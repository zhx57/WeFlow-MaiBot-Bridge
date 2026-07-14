from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from maim_message import MessageBase

from .buffer import DebounceBuffer
from .caption import CaptionProvider
from .config import AppConfig
from .media import SafeImageDownloader, decode_base64_image, read_local_image, write_unique_image
from .messages import build_message, resolve_reply_target
from .models import InboundMessage, event_message_id, normalize_event, policy_accept
from .outbound import OutboundPart, parse_outbound_segments
from .process_lock import ProcessLock
from .router import RouterService
from .storage import Storage
from .uia import UIASender
from .weflow import WeFlowClient


log = logging.getLogger(__name__)


class BridgeApp:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.storage = Storage(config.storage.database, config.bridge.queue_size)
        self.weflow = WeFlowClient(config.weflow, config.media)
        self.caption = CaptionProvider(config.caption)
        self.uia = UIASender(config.uia)
        self.router = RouterService(config.maibot, self._handle_maibot)
        self.buffer = DebounceBuffer(config.bridge.debounce_seconds, self._flush_batch)
        self.media_limit = asyncio.Semaphore(config.bridge.media_concurrency)
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = asyncio.Event()
        self._sequence = 0
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._process_lock = ProcessLock(config.storage.database.with_suffix(".lock"))

    async def run(self) -> None:
        self._process_lock.acquire()
        try:
            log.info("WeFlow-MaiBot-Bridge 正在启动")
            self.storage.initialize()
            self.config.media.directory.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self.weflow.check_api)
            self._tasks = [
                asyncio.create_task(self.router.run(), name="router-supervisor"),
                asyncio.create_task(self.weflow.run(self._receive_weflow), name="weflow-sse"),
                asyncio.create_task(self._outbox_worker(), name="inbound-outbox"),
                asyncio.create_task(self._outbound_worker(), name="wechat-outbound"),
                asyncio.create_task(self._maintenance_worker(), name="maintenance"),
            ]
            self._tasks.extend(
                asyncio.create_task(self._inbound_worker(), name=f"inbound-{index}")
                for index in range(self.config.bridge.media_concurrency)
            )
            try:
                await asyncio.to_thread(self.uia.start)
                log.info("微信 UIA 发送器已就绪")
            except Exception as exc:
                # Receiving from WeFlow must continue even when the WeChat window
                # is temporarily unavailable. Outbound sends will retry later.
                log.warning("微信 UIA 暂未就绪，不影响接收消息: %s", exc)
            done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                error = task.exception()
                if error:
                    raise error
        finally:
            try:
                await self.stop()
            finally:
                self._process_lock.release()

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.weflow.stop()
        for task in self._tasks:
            if task.get_name() == "weflow-sse" and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in self._tasks if task.get_name() == "weflow-sse"), return_exceptions=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and await asyncio.to_thread(self.storage.pending_event_count):
            await asyncio.sleep(0.1)
        await self.router.stop()
        try:
            await self.buffer.close(drain=True)
        except Exception:
            log.exception("停止时消息缓冲排空失败，事件将在下次启动恢复")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and await asyncio.to_thread(self.storage.pending_outbound_count):
            await asyncio.sleep(0.1)
        await self.uia.stop()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in self._tasks if task is not current), return_exceptions=True)

    async def _receive_weflow(self, event: dict[str, Any]) -> None:
        message_id = event_message_id(self.config.maibot.platform, event)
        inserted = await asyncio.to_thread(self.storage.enqueue_event, message_id, event)
        if inserted:
            log.info("微信消息已进入处理队列 id=%s", message_id[:12])
        else:
            log.debug("跳过已处理的重复微信消息 id=%s", message_id[:12])

    async def _inbound_worker(self) -> None:
        while True:
            item = await asyncio.to_thread(self.storage.claim_event)
            if item is None:
                if self._stopping.is_set():
                    return
                await asyncio.sleep(0.1)
                continue
            try:
                session_key = str(
                    item.payload.get("sessionId") or item.payload.get("talkerId")
                    or item.payload.get("sourceName") or item.message_id
                )
                lock = self._session_locks.setdefault(session_key, asyncio.Lock())
                async with lock:
                    buffered = await self._process_weflow(item.payload, item.message_id)
                if buffered:
                    await asyncio.to_thread(self.storage.event_buffered, item.message_id)
                else:
                    await asyncio.to_thread(self.storage.event_done, item.message_id)
            except Exception as exc:
                await asyncio.to_thread(
                    self.storage.event_failed, item, exc, self.config.bridge.max_attempts
                )
                log.exception("处理 WeFlow 消息失败 rawid=%s", item.payload.get("rawid"))

    async def _process_weflow(self, event: dict[str, Any], event_id: str) -> bool:
        self._sequence += 1
        message = normalize_event(
            event,
            self.config.maibot.platform,
            self._sequence,
            self.config.bridge.bot_nicknames,
            self.config.bridge.bot_wxid,
        )
        if message is None:
            log.debug("消息被过滤：自己发送、语音或空消息")
            return False
        if message.chat_type == "group" and self.config.bridge.group_mode == "mention":
            if message.part.type in {"image", "emoji"} and not message.mentioned:
                await asyncio.to_thread(
                    self.storage.save_pending_mention_media,
                    event_id,
                    message.session_key,
                    event,
                )
                return True
            if message.mentioned:
                cached = await asyncio.to_thread(
                    self.storage.get_pending_mention_media, message.session_key, 15.0
                )
                consumed: list[str] = []
                for cached_id, cached_event in cached:
                    self._sequence += 1
                    cached_message = normalize_event(
                        cached_event, self.config.maibot.platform, self._sequence,
                        self.config.bridge.bot_nicknames, self.config.bridge.bot_wxid,
                    )
                    if cached_message and cached_message.sender_key == message.sender_key:
                        await self._prepare_media(cached_message)
                        await self.buffer.add(self._buffer_key(cached_message), cached_message)
                        consumed.append(cached_id)
                await asyncio.to_thread(self.storage.remove_pending_mention_media, consumed)
        if not policy_accept(message, self.config.bridge.group_mode):
            log.info("群消息未 @ 机器人，已按 mention 模式忽略")
            return False
        if message.part.type in {"image", "emoji"}:
            await self._prepare_media(message)
        await self.buffer.add(self._buffer_key(message), message)
        log.info(
            "消息已进入 %.1f 秒合并缓冲 [%s]",
            self.config.bridge.debounce_seconds,
            message.session_name,
        )
        return True

    async def _prepare_media(self, message: InboundMessage) -> None:
        async with self.media_limit:
            image = await asyncio.to_thread(self.weflow.fetch_media, message.raw)
            message.part.data = image.base64
            message.part.caption = await asyncio.to_thread(self.caption.caption, image)

    def _buffer_key(self, message: InboundMessage) -> str:
        if message.chat_type == "group" and self.config.bridge.group_mode != "batch":
            return f"{message.session_key}\x1f{message.sender_key}"
        return message.session_key

    async def _flush_batch(self, batch: list[InboundMessage]) -> None:
        bot_name = self.config.bridge.bot_nicknames[0] if self.config.bridge.bot_nicknames else "WeFlow Bridge"
        message = build_message(
            batch,
            self.config.maibot.platform,
            bot_name,
            self.storage,
            group_batch=self.config.bridge.group_mode == "batch",
        )
        self.storage.enqueue_batch(
            message.message_info.message_id,
            message.to_dict(),
            [item.message_id for item in batch],
        )

    async def _outbox_worker(self) -> None:
        while not self._stopping.is_set():
            item = await asyncio.to_thread(self.storage.claim_next)
            if item is None:
                await asyncio.sleep(0.25)
                continue
            try:
                await self.router.send(MessageBase.from_dict(item.payload))
                await asyncio.to_thread(self.storage.sent, item.message_id)
                log.info("消息已交付 MaiBot id=%s", item.message_id)
            except Exception as exc:
                await asyncio.to_thread(self.storage.failed, item, exc, self.config.bridge.max_attempts)
                await asyncio.sleep(0.25)

    async def _maintenance_worker(self) -> None:
        while not self._stopping.is_set():
            expired = await asyncio.to_thread(self.storage.expire_pending_mention_media, 15.0)
            if expired:
                log.info("已清理 %d 条超时未关联的群媒体", expired)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def _handle_maibot(self, raw: MessageBase | dict[str, Any]) -> None:
        message = MessageBase.from_dict(raw) if isinstance(raw, dict) else raw
        target = resolve_reply_target(message, self.config.maibot.platform, self.storage)
        message_id = getattr(message.message_info, "message_id", None)
        try:
            if target is None:
                raise ValueError("MaiBot 回复无法映射到持久化微信会话")
            parts = parse_outbound_segments(message.message_segment)
            if not parts:
                return
            payload = {
                "contact": target[0],
                "chat_type": target[1],
                "parts": [{"kind": part.kind, "data": part.data} for part in parts],
                "next_part": 0,
            }
            if not message_id:
                message_id = f"generated-{uuid.uuid4().hex}"
            await asyncio.to_thread(self.storage.enqueue_outbound_message, str(message_id), payload)
        except Exception as exc:
            self.storage.store_dead_letter("outbound", message_id, message.to_dict(), exc)
            log.exception("MaiBot 出站消息进入死信 id=%s", message_id)

    async def _outbound_worker(self) -> None:
        while True:
            item = await asyncio.to_thread(self.storage.claim_outbound_message)
            if item is None:
                if self._stopping.is_set():
                    return
                await asyncio.sleep(0.1)
                continue
            try:
                payload = item.payload
                contact = str(payload["contact"])
                parts = payload["parts"]
                for index in range(int(payload.get("next_part", 0)), len(parts)):
                    part = OutboundPart(str(parts[index]["kind"]), parts[index].get("data"))
                    temporary: Path | None = None
                    try:
                        if part.kind == "text":
                            await self.uia.send(contact, "text", str(part.data))
                        else:
                            image = await asyncio.to_thread(self._prepare_outbound_image, part.data)
                            temporary = write_unique_image(image, self.config.media.directory, "maibot")
                            await self.uia.send(contact, "image", str(temporary))
                    finally:
                        if temporary:
                            try:
                                os.unlink(temporary)
                            except OSError:
                                log.warning("临时出站图片清理失败 path=%s", temporary)
                    payload["next_part"] = index + 1
                    await asyncio.to_thread(
                        self.storage.advance_outbound_message, item.message_id, payload
                    )
                await asyncio.to_thread(self.storage.finish_outbound_message, item.message_id)
            except Exception as exc:
                retry_safe = getattr(exc, "retry_safe", True)
                if retry_safe:
                    retrying = await asyncio.to_thread(
                        self.storage.retry_outbound_message,
                        item,
                        exc,
                        self.config.bridge.max_attempts,
                    )
                    if retrying:
                        log.warning("微信出站尚未开始，稍后重试 message_id=%s: %s", item.message_id, exc)
                        continue
                else:
                    await asyncio.to_thread(self.storage.fail_outbound_message, item, exc)
                log.exception("微信出站失败并进入死信 message_id=%s", item.message_id)

    def _prepare_outbound_image(self, data: Any):
        if isinstance(data, dict):
            values = [data[key] for key in ("base64", "data", "url", "path") if data.get(key)]
            if len(values) != 1:
                raise ValueError("image 段必须且只能指定一种来源")
            data = values[0]
        if not isinstance(data, str):
            raise ValueError("image 段数据必须是字符串")
        value = data.strip()
        if value.lower().startswith(("http://", "https://")):
            return SafeImageDownloader(
                self.config.media.max_bytes,
                self.config.media.download_timeout,
                self.config.media.max_redirects,
            ).download(value)
        if Path(value).expanduser().is_file():
            return read_local_image(value, self.config.media.max_bytes, self.config.media.local_roots)
        return decode_base64_image(value, self.config.media.max_bytes)

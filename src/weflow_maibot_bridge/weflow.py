from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin

import requests

from .config import MediaConfig, WeFlowConfig
from .media import ValidatedImage, validate_image
from .sse import SSEParser


log = logging.getLogger(__name__)


class WeFlowClient:
    def __init__(self, config: WeFlowConfig, media: MediaConfig) -> None:
        self.config = config
        self.media = media
        self._stop = threading.Event()
        self._response: requests.Response | None = None
        self._lock = threading.Lock()

    async def run(self, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue(maxsize=256)
        worker = asyncio.create_task(asyncio.to_thread(self._listen_thread, loop, queue))
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                await handler(item)
        finally:
            self.stop()
            await asyncio.gather(worker, return_exceptions=True)

    def check_api(self) -> None:
        log.info("正在检查 WeFlow API: %s", self.config.base_url)
        try:
            response = requests.get(
                self.config.base_url.rstrip("/") + "/health",
                timeout=(self.config.connect_timeout, self.config.connect_timeout),
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("status") != "ok":
                raise RuntimeError("WeFlow /health 返回内容异常")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "未知"
            raise RuntimeError(f"WeFlow /health 返回 HTTP {status}") from None
        except requests.RequestException:
            raise RuntimeError("无法连接 WeFlow API，请确认 WeFlow 已启动并开启 API") from None
        except ValueError:
            raise RuntimeError("WeFlow /health 没有返回有效 JSON") from None
        log.info("WeFlow API 检查通过")

    def _listen_thread(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        delay = self.config.retry_min_seconds
        last_event_id: str | None = None
        while not self._stop.is_set():
            try:
                log.info("正在连接 WeFlow SSE: %s/api/v1/push/messages", self.config.base_url.rstrip("/"))
                headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
                if last_event_id:
                    headers["Last-Event-ID"] = last_event_id
                response = requests.get(
                    self.config.base_url.rstrip("/") + "/api/v1/push/messages",
                    params={"access_token": self.config.access_token},
                    headers=headers,
                    stream=True,
                    timeout=(self.config.connect_timeout, self.config.read_timeout),
                )
                response.raise_for_status()
                with self._lock:
                    self._response = response
                log.info("WeFlow SSE 已连接，等待微信消息")
                parser = SSEParser()
                delay = self.config.retry_min_seconds
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if self._stop.is_set():
                        break
                    # WeFlow historically emits one complete JSON object per data line.
                    # Handle that immediately instead of waiting for a blank SSE delimiter.
                    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                    if text and text.startswith("data:"):
                        data_text = text[5:].strip()
                        try:
                            json.loads(data_text)
                        except json.JSONDecodeError:
                            pass
                        else:
                            # Finish a complete one-line WeFlow event immediately,
                            # while preserving any preceding SSE id/event fields.
                            parser.feed(line)
                            event = parser.feed("")
                            parser = SSEParser()
                            if event is not None:
                                if event.id is not None:
                                    last_event_id = event.id
                                payload = json.loads(event.data)
                                if isinstance(payload, dict):
                                    if event.id is not None:
                                        payload["_sse_event_id"] = event.id
                                    payload["_received_at_ns"] = time.time_ns()
                                    self._put_payload(loop, queue, payload)
                                continue
                    event = parser.feed(line)
                    if event is None:
                        continue
                    if event.id is not None:
                        last_event_id = event.id
                    try:
                        payload = json.loads(event.data)
                    except json.JSONDecodeError:
                        log.warning("忽略无法解析的 WeFlow SSE JSON event_id=%s", event.id)
                        continue
                    if isinstance(payload, dict):
                        if event.id is not None:
                            payload["_sse_event_id"] = event.id
                        payload["_received_at_ns"] = time.time_ns()
                        self._put_payload(loop, queue, payload)
                if self._stop.is_set():
                    break
                event = parser.feed("")
                if event is not None:
                    try:
                        payload = json.loads(event.data)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(payload, dict):
                            payload["_received_at_ns"] = time.time_ns()
                            self._put_payload(loop, queue, payload)
                raise ConnectionError("WeFlow SSE 流已结束")
            except BaseException as exc:
                if self._stop.is_set():
                    break
                log.warning(
                    "WeFlow SSE 断开，%.1f 秒后重连: %s",
                    delay,
                    self._safe_error(exc),
                )
                if self._stop.wait(delay + random.uniform(0, delay * 0.2)):
                    break
                delay = min(self.config.retry_max_seconds, delay * 2)
            finally:
                with self._lock:
                    if self._response is not None:
                        self._response.close()
                    self._response = None
        loop.call_soon_threadsafe(queue.put_nowait, None)

    def _put_payload(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        payload: dict[str, Any],
    ) -> None:
        log.info(
            "收到微信消息 [%s] %s",
            payload.get("sourceName") or payload.get("talkerName") or "未知会话",
            str(payload.get("content") or "[媒体]")[:80],
        )
        future = asyncio.run_coroutine_threadsafe(queue.put(payload), loop)
        future.result(timeout=self.config.read_timeout)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._response is not None:
                self._response.close()

    def fetch_media(self, event: dict[str, Any]) -> ValidatedImage:
        try:
            direct = event.get("mediaUrl")
            if direct:
                return self._download_weflow_media(str(direct))
            session = str(event.get("sessionId") or event.get("talkerId") or "")
            response = requests.get(
                self.config.base_url.rstrip("/") + "/api/v1/messages",
                params={"access_token": self.config.access_token, "talker": session, "media": "true", "limit": 20},
                timeout=(self.config.connect_timeout, self.media.download_timeout),
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"WeFlow 媒体 API 请求失败: {self._safe_error(exc)}") from None
        messages = body if isinstance(body, list) else body.get("messages", body.get("data", []))
        if not isinstance(messages, list):
            raise ValueError("WeFlow 消息 API 未返回列表")
        candidates = [item for item in messages if isinstance(item, dict) and item.get("mediaUrl")]
        rawid = str(event.get("rawid") or event.get("rawId") or "")
        match = next(
            (item for item in candidates if rawid and str(item.get("rawid") or item.get("rawId") or "") == rawid),
            None,
        )
        if match is None:
            match = next(
                (item for item in candidates if str(item.get("mediaType") or "").lower() in {"image", "sticker", "emoji"}),
                None,
            )
            if match:
                log.warning("未按 rawid 精确匹配媒体，退化为会话最近图片 rawid=%s", rawid or "missing")
        if match is None:
            raise ValueError("WeFlow 会话中没有可下载媒体")
        return self._download_weflow_media(str(match["mediaUrl"]))

    def _download_weflow_media(self, media_url: str) -> ValidatedImage:
        try:
            url = urljoin(self.config.base_url.rstrip("/") + "/", media_url)
            response = requests.get(
                url,
                params={"access_token": self.config.access_token},
                stream=True,
                timeout=(self.config.connect_timeout, self.media.download_timeout),
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"WeFlow 媒体下载失败: {self._safe_error(exc)}") from None
        length = response.headers.get("Content-Length")
        if length and int(length) > self.media.max_bytes:
            raise ValueError("WeFlow 图片超过尺寸上限")
        raw = bytearray()
        for chunk in response.iter_content(64 * 1024):
            raw.extend(chunk)
            if len(raw) > self.media.max_bytes:
                raise ValueError("WeFlow 图片超过尺寸上限")
        return validate_image(bytes(raw), self.media.max_bytes, response.headers.get("Content-Type"))

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        if isinstance(error, requests.HTTPError) and error.response is not None:
            return f"HTTP {error.response.status_code}"
        if isinstance(error, requests.Timeout):
            return "timeout"
        if isinstance(error, requests.ConnectionError):
            return "connection error"
        return type(error).__name__

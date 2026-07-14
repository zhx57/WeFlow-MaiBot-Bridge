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

    def _listen_thread(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        delay = self.config.retry_min_seconds
        last_event_id: str | None = None
        while not self._stop.is_set():
            try:
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
                parser = SSEParser()
                delay = self.config.retry_min_seconds
                for line in response.iter_lines(decode_unicode=True):
                    if self._stop.is_set():
                        return
                    event = parser.feed(line)
                    if event is None:
                        continue
                    if event.id is not None:
                        last_event_id = event.id
                    if event.event not in {"message", "wechat-message"}:
                        continue
                    try:
                        payload = json.loads(event.data)
                    except json.JSONDecodeError:
                        log.warning("忽略无法解析的 WeFlow SSE JSON event_id=%s", event.id)
                        continue
                    if isinstance(payload, dict):
                        if event.id is not None:
                            payload["_sse_event_id"] = event.id
                        payload["_received_at_ns"] = time.time_ns()
                        future = asyncio.run_coroutine_threadsafe(queue.put(payload), loop)
                        future.result(timeout=self.config.read_timeout)
                raise ConnectionError("WeFlow SSE 流已结束")
            except BaseException as exc:
                if self._stop.is_set():
                    return
                log.warning(
                    "WeFlow SSE 断开，%.1f 秒后重连: %s",
                    delay,
                    self._safe_error(exc),
                )
                if self._stop.wait(delay + random.uniform(0, delay * 0.2)):
                    return
                delay = min(self.config.retry_max_seconds, delay * 2)
            finally:
                with self._lock:
                    if self._response is not None:
                        self._response.close()
                    self._response = None
        loop.call_soon_threadsafe(queue.put_nowait, None)

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

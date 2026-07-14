from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from maim_message import MessageBase, RouteConfig, Router, TargetConfig

from .config import MaiBotConfig


log = logging.getLogger(__name__)


class RouterService:
    def __init__(self, config: MaiBotConfig, handler: Callable[[MessageBase], Awaitable[None]]) -> None:
        self.config = config
        self.handler = handler
        self.router = self._new_router()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def _new_router(self) -> Router:
        router = Router(RouteConfig(route_config={
            self.config.platform: TargetConfig(
                url=self.config.url,
                token=self.config.token or None,
                ssl_verify=None,
            )
        }), custom_logger=log)
        router.register_class_handler(self.handler)
        return router

    async def run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            self._task = asyncio.create_task(self.router.run(), name="maim-router")
            try:
                await self._task
                if not self._stop.is_set():
                    raise RuntimeError("maim Router 意外退出")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    return
                log.warning("maim Router 退出，%.1f 秒后重建: %s", delay, exc)
                try:
                    await self.router.stop()
                except Exception:
                    log.debug("停止故障 Router 失败", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                if not self._stop.is_set():
                    self.router = self._new_router()
                    delay = min(self.config.reconnect_max_seconds, delay * 2)

    def connected(self) -> bool:
        try:
            return self.router.check_connection(self.config.platform)
        except Exception:
            return False

    async def send(self, message: MessageBase) -> None:
        if not self.connected():
            raise ConnectionError("MaiBot Router 尚未连接")
        delivered = await self.router.send_message(message)
        if delivered is not True:
            raise ConnectionError("maim Router 未接受消息")

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self.router.stop()
        finally:
            if self._task and not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .models import InboundMessage


@dataclass(slots=True)
class _Bucket:
    items: list[InboundMessage] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class DebounceBuffer:
    """Per-conversation trailing debounce that keeps arrivals during processing."""

    def __init__(self, delay: float, handler: Callable[[list[InboundMessage]], Awaitable[None]]) -> None:
        self.delay = delay
        self.handler = handler
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def add(self, key: str, item: InboundMessage) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("消息缓冲已关闭")
            bucket = self._buckets.setdefault(key, _Bucket())
            bucket.items.append(item)
            bucket.changed.set()
            if bucket.task is None:
                bucket.task = asyncio.create_task(self._run(key, bucket), name=f"debounce:{key}")

    async def _run(self, key: str, bucket: _Bucket) -> None:
        try:
            while True:
                bucket.changed.clear()
                try:
                    await asyncio.wait_for(bucket.changed.wait(), timeout=self.delay)
                    continue
                except TimeoutError:
                    pass
                async with self._lock:
                    batch = sorted(bucket.items, key=lambda item: item.sequence)
                    bucket.items = []
                if batch:
                    try:
                        await self.handler(batch)
                    except BaseException:
                        async with self._lock:
                            bucket.items = batch + bucket.items
                        raise
                async with self._lock:
                    if bucket.items:
                        bucket.changed.set()
                        continue
                    self._buckets.pop(key, None)
                    bucket.task = None
                    return
        except asyncio.CancelledError:
            raise
        finally:
            async with self._lock:
                if bucket.task is asyncio.current_task():
                    bucket.task = None
                if not self._closed and bucket.items:
                    bucket.task = asyncio.create_task(
                        self._run(key, bucket), name=f"debounce:{key}"
                    )
                elif not bucket.items:
                    self._buckets.pop(key, None)

    async def close(self, drain: bool = True) -> None:
        async with self._lock:
            self._closed = True
            tasks = [bucket.task for bucket in self._buckets.values() if bucket.task]
        if drain:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        else:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

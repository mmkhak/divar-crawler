"""In-process fan-out for server-sent events."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, payload))
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait((event, payload))
                except Exception:
                    pass

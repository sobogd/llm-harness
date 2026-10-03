"""SSE event bus: global seq, ring buffer for reconnection, fan-out to subscribers."""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque


class EventBus:
    def __init__(self, maxlen: int = 10_000):
        self._ring: deque[dict] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue] = set()
        self._seq = 0
        self._lock = asyncio.Lock()

    # -- publishing ---------------------------------------------------------
    async def publish(self, etype: str, **data) -> dict:
        async with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "type": etype,
                     "ts": int(time.time() * 1000), **data}
            self._ring.append(event)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # slow consumer — it can catch up via ?since=<seq>
                    pass
            return event

    # -- subscribing ---------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1_000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def events_since(self, seq: int) -> list[dict]:
        return [e for e in self._ring if e["seq"] > seq]

    @property
    def last_seq(self) -> int:
        return self._seq

    # -- SSE wire format -----------------------------------------------------
    @staticmethod
    def sse_format(event: dict) -> str:
        return (f"id: {event['seq']}\n"
                f"event: {event['type']}\n"
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
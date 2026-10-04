"""Tiny in-process pub/sub so the live dashboard can watch calls as they happen."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 800):
        self._subs: set[asyncio.Queue] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=history)

    def publish(self, call_id: str, type_: str, **data: Any) -> None:
        event = {"call_id": call_id, "type": type_, "ts": time.time(), **data}
        self._recent.append(event)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow dashboard; drop rather than block the call

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def recent(self) -> list[dict[str, Any]]:
        # Token-level deltas are noise on replay; the final agent_turn carries the text.
        return [e for e in self._recent if e["type"] not in {"agent_delta", "caption"}]


bus = EventBus()

"""In-process publish/subscribe used to push Server-Sent Events to the browser."""
from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any

log = logging.getLogger("vc.events")


class Subscriber:
    def __init__(self) -> None:
        self.queue: queue.Queue[str] = queue.Queue(maxsize=2000)
        self.overflowed = False


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    def publish(self, type_: str, data: Any = None) -> None:
        msg = json.dumps({"type": type_, "data": data}, ensure_ascii=False)
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.queue.put_nowait(msg)
            except queue.Full:
                if not sub.overflowed:
                    log.warning("SSE subscriber too slow; it will be told to resync")
                sub.overflowed = True

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

"""In-process event bus: the reactive core of the Maestro daemon.

Every task state change, output chunk, question, usage report, and verification
result is published as a :class:`TaskEvent`. Subscribers (MCP blocking waits,
dashboards, SSE streams) consume events from per-subscriber queues — nothing in
the system polls for completion. A ring buffer of recent events lets late
subscribers catch up.
"""

from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskEvent:
    task_id: str
    type: str  # "state" | "output" | "question" | "usage" | "verify" | "artifact"
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utcnow_iso)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "type": self.type, "data": dict(self.data), "ts": self.ts, "seq": self.seq}


class Subscription:
    """A consumer view of the bus with blocking waits (no polling)."""

    def __init__(self, bus: "EventBus", q: "queue.Queue[TaskEvent]", types: tuple[str, ...]) -> None:
        self._bus = bus
        self._q = q
        self.types = types

    @property
    def overflowed(self) -> bool:
        """True when this subscriber fell ``maxsize`` events behind and events were dropped."""
        return bool(getattr(self._q, "overflowed", False))

    def get(self, timeout: float | None = None) -> TaskEvent | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait(self, predicate: Callable[[TaskEvent], bool] | None = None, timeout: float | None = None) -> TaskEvent | None:
        """Block until a matching event arrives (or the timeout expires)."""
        import time

        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + timeout
        while True:
            if deadline is None:
                event = self._q.get()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    event = self._q.get(timeout=remaining)
                except queue.Empty:
                    return None
            if predicate is None or predicate(event):
                return event

    def close(self) -> None:
        self._bus._unsubscribe(self)


class EventBus:
    """Thread-safe pub/sub with a catch-up ring buffer."""

    def __init__(self, ring_size: int = 1000) -> None:
        self._ring: list[TaskEvent] = []
        self._subs: list[tuple["queue.Queue[TaskEvent]", tuple[str, ...]]] = []
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._ring_size = ring_size

    def publish(self, event: TaskEvent) -> TaskEvent:
        with self._lock:
            event.seq = next(self._counter)
            self._ring.append(event)
            if len(self._ring) > self._ring_size:
                del self._ring[: len(self._ring) - self._ring_size]
            targets = [q for q, types in self._subs if not types or event.type in types]
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                q.overflowed = True  # type: ignore[attr-defined]  # read by Subscription.overflowed
        return event

    def subscribe(self, *types: str, maxsize: int = 0) -> Subscription:
        """Subscribe to events of ``types`` (all types when none are given).

        ``maxsize`` bounds the subscriber's queue; 0, the default, means no
        bound. A bounded subscriber catches up on at most ``maxsize`` of the
        newest events, and once its queue is full further events are dropped
        and :attr:`Subscription.overflowed` becomes True.
        """
        q: "queue.Queue[TaskEvent]" = queue.Queue(maxsize=maxsize)
        with self._lock:
            # Catch up on recent history first (newest last).
            backlog = [event for event in self._ring if not types or event.type in types]
            if maxsize:
                backlog = backlog[-maxsize:]
            for event in backlog:
                q.put(event)
            self._subs.append((q, types))
        return Subscription(self, q, types)

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            for i, (q, _) in enumerate(self._subs):
                if q is sub._q:
                    del self._subs[i]
                    break

    def history(self, task_id: str | None = None, types: tuple[str, ...] = ()) -> list[TaskEvent]:
        with self._lock:
            return [e for e in self._ring if (task_id is None or e.task_id == task_id) and (not types or e.type in types)]

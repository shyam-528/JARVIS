"""Async pub/sub event bus with SQLite persistence.

Failure policy (Wave 7):
- ``publish`` never raises. Persistence and queueing are best-effort so a
  broken DB or loop can never block a publisher, but such failures are ALWAYS
  announced loudly via ``print("[EventBus] ...")`` naming the event type —
  a dropped replay-log entry or undispatched event must never be invisible.
- One bad handler never breaks the others; handler exceptions are logged.
- History reads are fail-soft: a single corrupt payload row decodes to {}
  rather than breaking the whole read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .db import connect as _db_connect


@dataclass
class Event:
    type: str  # "command", "tool_result", "file_created", ...
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Async pub/sub event bus with SQLite persistence.

    All automation reacts to events through this. Events are persisted so
    triggers can replay missed events after restart.
    """

    def __init__(self, db_path: Path, max_log: int = 5000):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "type TEXT NOT NULL,"
            "payload TEXT NOT NULL)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (type, ts DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)")
        self.conn.commit()
        self._max_log = max_log
        self._handlers: dict[str, list[Handler]] = {}
        self._catchall: list[Handler] = []
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._dispatch_task: asyncio.Task | None = None
        self._running = False
        # stats
        self.published = 0
        self.dispatched = 0

    # ---------- lifecycle ----------

    def start(self):
        if self._running:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self):
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            # Expected shutdown path: the task was just cancelled by us.
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task
        self.conn.close()

    async def _dispatch_loop(self):
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # fail-soft: 1s poll timeout, just re-check _running
            except asyncio.CancelledError:
                break  # expected shutdown via stop()
            await self._dispatch(event)

    async def _dispatch(self, event: Event):
        handlers = list(self._handlers.get(event.type, [])) + list(self._catchall)
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                # One bad handler must not break others
                print(f"[EventBus] handler error on {event.type}: {exc}")
        self.dispatched += 1

    # ---------- publishing ----------

    def publish(self, event_type: str, payload: dict | None = None):
        """Publish an event (sync-safe; can be called from anywhere)."""
        event = Event(type=event_type, payload=payload or {})
        self.published += 1
        # Persist (best-effort, never blocks the publisher on failure —
        # but a dropped replay-log entry must never be invisible)
        try:
            self.conn.execute(
                "INSERT INTO events (ts, type, payload) VALUES (?, ?, ?)",
                (event.ts, event.type, json.dumps(event.payload, default=str)),
            )
            self.conn.commit()
            # Trim old events periodically
            if self.published % 500 == 0:
                self._trim()
        except Exception as exc:
            print(f"[EventBus] failed to persist event {event.type}: {exc}")
        # Queue for async dispatch
        try:
            self._queue.put_nowait(event)
        except Exception as exc:
            print(f"[EventBus] failed to queue event {event.type} for dispatch: {exc}")

    async def publish_async(self, event_type: str, payload: dict | None = None):
        self.publish(event_type, payload)

    def _trim(self):
        try:
            self.conn.execute(
                "DELETE FROM events WHERE id NOT IN ("
                "SELECT id FROM events ORDER BY ts DESC LIMIT ?)",
                (self._max_log,),
            )
            self.conn.commit()
        except Exception as exc:
            # fail-soft: trim is maintenance; log grows until next attempt.
            # Fires at most once per 500 publishes, so not spammy.
            print(f"[EventBus] event-log trim failed: {exc}")

    # ---------- subscribing ----------

    def subscribe(self, event_type: str, handler: Handler):
        """Subscribe a handler to a specific event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: Handler):
        """Subscribe a handler to ALL events."""
        self._catchall.append(handler)

    def unsubscribe(self, event_type: str, handler: Handler):
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    # ---------- history ----------

    def recent(self, limit: int = 50, event_type: str = "") -> list[dict]:
        if event_type:
            rows = self.conn.execute(
                "SELECT ts, type, payload FROM events WHERE type = ? ORDER BY id DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ts, type, payload FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for ts, etype, payload in rows:
            try:
                p = json.loads(payload)
            except Exception as exc:
                # fail-soft: one corrupt row must not break the whole read,
                # but corrupt replay-log data must not be invisible either
                print(f"[EventBus] corrupt payload for event '{etype}' in replay log: {exc}")
                p = {}
            out.append({"ts": ts, "type": etype, "payload": p})
        return out

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        by_type = self.conn.execute(
            "SELECT type, COUNT(*) FROM events GROUP BY type ORDER BY 2 DESC LIMIT 15"
        ).fetchall()
        return {
            "total_events": total,
            "published_this_session": self.published,
            "dispatched_this_session": self.dispatched,
            "by_type": dict(by_type),
            "running": self._running,
        }

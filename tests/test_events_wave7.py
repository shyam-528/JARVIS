"""Wave 7 failure-path tests for core.events (EventBus).

Contract: publish() never raises — persistence and queueing are best-effort
so a broken DB/loop can never block a publisher, but every such failure is
announced loudly via print("[EventBus] ...") naming the event type.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from core.events import Event, EventBus


@pytest.fixture
def bus(tmp_path):
    b = EventBus(tmp_path / "events.db", max_log=10)
    yield b
    with contextlib.suppress(Exception):
        b.conn.close()  # already closed by some tests; fail-soft cleanup


def test_publish_with_broken_db_is_loud_but_still_queues(bus, capsys):
    bus.conn.close()  # break persistence

    bus.publish("file_created", {"path": "x"})

    out = capsys.readouterr().out
    assert "[EventBus] failed to persist event file_created" in out
    assert bus._queue.qsize() == 1, "event must still be queued for dispatch"
    assert bus.published == 1


def test_publish_with_broken_queue_is_loud_but_still_persists(bus, capsys):
    class ExplodingQueue:
        def put_nowait(self, event):
            raise RuntimeError("loop is gone")

    bus._queue = ExplodingQueue()
    bus.publish("command", {"cmd": "ls"})

    out = capsys.readouterr().out
    assert "[EventBus] failed to queue event command" in out
    # persistence still worked
    assert bus.recent(1)[0]["type"] == "command"


async def test_handler_error_is_loud_and_other_handlers_still_run(bus, capsys):
    seen = []

    async def bad(event):
        raise ValueError("handler exploded")

    async def good(event):
        seen.append(event.type)

    bus.subscribe("command", bad)
    bus.subscribe("command", good)
    bus.subscribe_all(good)

    await bus._dispatch(Event(type="command", payload={}))

    out = capsys.readouterr().out
    assert "[EventBus] handler error on command" in out
    assert seen == ["command", "command"], "good handlers must still run after a bad one"
    assert bus.dispatched == 1


def test_corrupt_replay_row_is_loud_and_returns_empty_payload(bus, capsys):
    bus.publish("good", {"a": 1})
    # inject a corrupt row directly
    bus.conn.execute(
        "INSERT INTO events (ts, type, payload) VALUES (?, ?, ?)",
        (1.0, "corrupt", "{not json"),
    )
    bus.conn.commit()

    rows = bus.recent(10)
    out = capsys.readouterr().out
    assert "[EventBus] corrupt payload for event 'corrupt'" in out
    corrupt = [r for r in rows if r["type"] == "corrupt"][0]
    assert corrupt["payload"] == {}
    assert any(r["type"] == "good" and r["payload"] == {"a": 1} for r in rows)


def test_trim_failure_is_loud(bus, capsys):
    bus.conn.close()
    bus._trim()  # must not raise
    assert "[EventBus] event-log trim failed" in capsys.readouterr().out


async def test_dispatch_loop_survives_and_stops_cleanly(bus):
    got = []

    async def handler(event):
        got.append(event.type)

    bus.subscribe("ping", handler)
    bus.start()
    bus.publish("ping", {})
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.05)
    assert got == ["ping"]
    assert bus.dispatched == 1
    await bus.stop()

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from core.config import Config
from core.notify import (
    NotificationConfig,
    NotificationManager,
    Priority,
)
from core.scheduler import TaskScheduler

# ---------- NotificationManager ----------


class DummyOrch:
    """Bare-bones stand-in for the orchestrator (toolbox.execute recorder)."""

    class toolbox:
        calls = []

        @staticmethod
        async def execute(action, args_json):
            DummyOrch.toolbox.calls.append((action, args_json))
            return "ok"


def make_sched(tmp_path: Path) -> TaskScheduler:
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    sched = TaskScheduler(cfg)
    sched.set_orchestrator(DummyOrch())
    return sched


# ---------- templates ----------


def test_default_templates_loaded():
    m = NotificationManager(NotificationConfig())
    assert m.templates, "DEFAULT_TEMPLATES should load at init"
    assert "reminder" in m.templates
    rendered = m.render_template("reminder", message="stand up")
    assert "stand up" in rendered


def test_render_template_missing_key_is_graceful():
    m = NotificationManager(NotificationConfig())
    out = m.render_template("briefing", name="Sir")  # date/weather/schedule missing
    assert out.startswith("Template error:")


# ---------- telegram attachment framing (no network) ----------


def test_telegram_send_uses_photo_when_attachment(monkeypatch):
    sent = []

    class FakeResp:
        @staticmethod
        def json():
            return {"ok": True}

    class FakeClient:
        async def post(self, url, **kwargs):
            sent.append((url, kwargs))
            return FakeResp()

        async def aclose(self):
            pass

    from core.notify import NotificationSender

    sender = NotificationSender(NotificationConfig(telegram_bot_token="T", telegram_chat_id="C"))
    sender._client = FakeClient()

    result = asyncio.run(
        sender._send_telegram(
            "hello", "Title", Priority.DEFAULT, attachments=[("shot.png", b"pngbytes")]
        )
    )

    assert result["success"] is True
    assert "sendPhoto" in sent[0][0]
    assert sent[0][1]["files"]["photo"][0] == "shot.png"


def test_telegram_send_plain_message(monkeypatch):
    sent = []

    class FakeResp:
        @staticmethod
        def json():
            return {"ok": True}

    class FakeClient:
        async def post(self, url, **kwargs):
            sent.append((url, kwargs))
            return FakeResp()

        async def aclose(self):
            pass

    from core.notify import NotificationSender

    sender = NotificationSender(NotificationConfig(telegram_bot_token="T", telegram_chat_id="C"))
    sender._client = FakeClient()

    result = asyncio.run(sender._send_telegram("hello", "Title", Priority.DEFAULT))

    assert result["success"] is True
    assert "sendMessage" in sent[0][0]
    assert sent[0][1]["json"]["text"].startswith("<b>Title</b>")


# ---------- scheduler catch-up ----------


def test_catch_up_fires_missed_once_task(tmp_path):
    sched = make_sched(tmp_path)
    # A 'once' task that came due 1 hour ago (while "offline")
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    _tid = sched.add_task(
        name="missed reminder",
        trigger_type="once",
        trigger_spec={"run_at": past},
        action="notify_reminder",
        action_args={"message": "you missed me"},
    )

    async def run():
        await sched.start()  # fires catch-up
        await asyncio.sleep(0.2)  # let create_task run
        await sched.stop()

    asyncio.run(run())

    assert DummyOrch.toolbox.calls, "catch-up should execute the missed task"
    action, args_json = DummyOrch.toolbox.calls[-1]
    assert action == "notify_reminder"
    assert "you missed me" in args_json
    DummyOrch.toolbox.calls.clear()


def test_catch_up_skips_very_old_tasks(tmp_path):
    sched = make_sched(tmp_path)
    old = (datetime.now() - timedelta(days=3)).isoformat()
    _tid = sched.add_task(
        name="ancient reminder",
        trigger_type="once",
        trigger_spec={"run_at": old},
        action="notify_reminder",
        action_args={"message": "too old"},
    )

    async def run():
        await sched.start()
        await asyncio.sleep(0.2)
        await sched.stop()

    asyncio.run(run())

    assert not DummyOrch.toolbox.calls, "tasks older than 24h must NOT fire"
    DummyOrch.toolbox.calls.clear()


def test_add_task_same_name_replaces(tmp_path):
    sched = make_sched(tmp_path)
    sched.add_task(
        name="dup",
        trigger_type="interval",
        trigger_spec={"seconds": 60},
        action="notify_reminder",
        action_args={},
    )
    sched.add_task(
        name="dup",
        trigger_type="interval",
        trigger_spec={"seconds": 60},
        action="notify_reminder",
        action_args={},
    )
    names = [t.name for t in sched.list_tasks()]
    assert names.count("dup") == 1, "same-name tasks must dedupe"


# ---------- missed recurring jobs (e.g. db-maintenance at 04:10 while off) ----------


def _force_next_run_into_past(sched, task_id, *, hours=0, days=0):
    """Simulate 'the server was off when the slot arrived': push next_run back."""
    past = (datetime.now() - timedelta(hours=hours, days=days)).isoformat()
    sched._conn.execute("UPDATE tasks SET next_run=? WHERE id=?", (past, task_id))
    sched._conn.commit()
    sched._tasks[task_id].next_run = past


def _boot_and_settle(sched):
    async def run():
        await sched.start()
        await asyncio.sleep(0.5)  # comfortably past the first run-loop tick
        await sched.stop()

    asyncio.run(run())


def test_missed_housekeeping_job_fires_once_at_boot(tmp_path):
    """catch_up_always jobs (db-maintenance) missed by <24h fire once at boot."""
    DummyOrch.toolbox.calls.clear()
    sched = make_sched(tmp_path)
    tid = sched.add_task(
        name="db-maintenance",
        trigger_type="daily",
        trigger_spec={"hour": 4, "minute": 10, "catch_up_always": True},
        action="db_maintain",
        action_args={},
    )
    _force_next_run_into_past(sched, tid, hours=2)
    _boot_and_settle(sched)

    assert len(DummyOrch.toolbox.calls) == 1, (
        f"missed slot must fire exactly once, got {len(DummyOrch.toolbox.calls)}"
    )
    assert DummyOrch.toolbox.calls[0][0] == "db_maintain"
    assert sched._tasks[tid].next_run > datetime.now().isoformat(), (
        "next_run must be pushed to the next future slot after catch-up"
    )
    DummyOrch.toolbox.calls.clear()


def test_missed_housekeeping_job_fires_even_when_ancient(tmp_path):
    """A missed VACUUM is still useful days late — catch_up_always ignores the
    24h anti-spam window that applies to user-facing reminders."""
    DummyOrch.toolbox.calls.clear()
    sched = make_sched(tmp_path)
    tid = sched.add_task(
        name="db-maintenance",
        trigger_type="daily",
        trigger_spec={"hour": 4, "minute": 10, "catch_up_always": True},
        action="db_maintain",
        action_args={},
    )
    _force_next_run_into_past(sched, tid, days=3)
    _boot_and_settle(sched)

    assert len(DummyOrch.toolbox.calls) == 1, (
        f"catch_up_always job should fire once even when >24h stale, got {len(DummyOrch.toolbox.calls)}"
    )
    assert sched._tasks[tid].next_run > datetime.now().isoformat()
    DummyOrch.toolbox.calls.clear()


def test_missed_user_facing_job_skips_stale_slot(tmp_path):
    """Default recurring behavior unchanged: a 7:30 briefing at 9pm boot is
    skipped, not spoken — but next_run MUST move to the next future slot."""
    DummyOrch.toolbox.calls.clear()
    sched = make_sched(tmp_path)
    tid = sched.add_task(
        name="morning-briefing",
        trigger_type="daily",
        trigger_spec={"hour": 7, "minute": 30},
        action="notify_briefing",
        action_args={},
    )
    _force_next_run_into_past(sched, tid, hours=2)
    _boot_and_settle(sched)

    assert not DummyOrch.toolbox.calls, "user-facing stale slot must be skipped"
    assert sched._tasks[tid].next_run > datetime.now().isoformat()
    DummyOrch.toolbox.calls.clear()


def test_missed_recurring_ancient_without_flag_never_fires(tmp_path):
    """Regression: >24h-stale recurring tasks fell through catch-up untouched,
    so the run loop fired the ancient slot on its first tick. A stale next_run
    must never reach the run loop."""
    DummyOrch.toolbox.calls.clear()
    sched = make_sched(tmp_path)
    tid = sched.add_task(
        name="morning-briefing",
        trigger_type="daily",
        trigger_spec={"hour": 7, "minute": 30},
        action="notify_briefing",
        action_args={},
    )
    _force_next_run_into_past(sched, tid, days=3)
    _boot_and_settle(sched)

    assert not DummyOrch.toolbox.calls, (
        f"ancient unflagged recurring task must not fire, got {DummyOrch.toolbox.calls}"
    )
    assert sched._tasks[tid].next_run > datetime.now().isoformat()
    DummyOrch.toolbox.calls.clear()


def test_missed_once_task_fires_exactly_once(tmp_path):
    """Missed one-shot (<24h): catch-up claims it (next_run=None) then fires,
    so the run loop cannot fire it a second time."""
    DummyOrch.toolbox.calls.clear()
    sched = make_sched(tmp_path)
    past = (datetime.now() - timedelta(hours=1)).isoformat()
    sched.add_task(
        name="missed reminder",
        trigger_type="once",
        trigger_spec={"run_at": past},
        action="notify_reminder",
        action_args={"message": "hi"},
    )
    _boot_and_settle(sched)

    assert len(DummyOrch.toolbox.calls) == 1, (
        f"missed one-shot must fire exactly once, got {len(DummyOrch.toolbox.calls)}"
    )
    DummyOrch.toolbox.calls.clear()


def test_missed_once_task_ancient_never_fires(tmp_path):
    """One-shot added with a future run_at, then missed by >24h of downtime:
    the anti-spam guard must claim it (next_run=None) without firing — a stale
    next_run must never leak through catch-up into the run loop."""
    DummyOrch.toolbox.calls.clear()
    sched = make_sched(tmp_path)
    future = (datetime.now() + timedelta(hours=1)).isoformat()
    tid = sched.add_task(
        name="ancient reminder",
        trigger_type="once",
        trigger_spec={"run_at": future},
        action="notify_reminder",
        action_args={"message": "too old"},
    )
    _force_next_run_into_past(sched, tid, days=3)
    _boot_and_settle(sched)

    assert not DummyOrch.toolbox.calls, (
        f"ancient one-shot must not fire, got {DummyOrch.toolbox.calls}"
    )
    assert sched._tasks[tid].next_run is None, "ancient one-shot must be claimed"
    DummyOrch.toolbox.calls.clear()

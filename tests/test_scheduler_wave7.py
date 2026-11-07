"""Wave 7 failure-path tests for core.scheduler.

Contract: action failures are loud + retried once + recorded as last_error;
storage corruption (bad cron expr, corrupt rows/timestamps) is loud and the
affected task is skipped/disabled — never silently dropped.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from core.config import Config
from core.scheduler import ScheduledTask, TaskScheduler


def make_cfg(tmp_path) -> Config:
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return cfg


def make_task(**kw) -> ScheduledTask:
    base = {
        "id": "t1",
        "name": "demo",
        "trigger_type": "daily",
        "trigger_spec": {"hour": 9, "minute": 0},
        "action": "notify_reminder",
        "action_args": {},
    }
    base.update(kw)
    return ScheduledTask(**base)


def test_bad_cron_expression_is_loud_and_recorded(tmp_path, capsys):
    sched = TaskScheduler(make_cfg(tmp_path))
    task = make_task(trigger_type="cron", trigger_spec={"expression": "not a cron"})
    sched._compute_next_run(task)

    assert task.next_run is None
    assert "bad cron expression" in (task.last_error or "")
    out = capsys.readouterr().out
    assert "demo" in out and "not a cron" in out
    sched._conn.close()


def test_corrupt_stored_row_skipped_loudly_others_load(tmp_path, capsys):
    sched = TaskScheduler(make_cfg(tmp_path))
    good = make_task(id="good", name="healthy")
    sched._save_task(good)
    # inject a row whose trigger_spec is not JSON
    sched._conn.execute(
        "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("bad", "corrupt", "daily", "{not json", "act", "{}", 1, "now", None, None, 0, None, None),
    )
    sched._conn.commit()

    sched2 = TaskScheduler(make_cfg(tmp_path))  # reloads from same db
    out = capsys.readouterr().out
    assert "corrupt" in out and "bad" in out
    assert "good" in sched2._tasks
    assert "bad" not in sched2._tasks
    sched._conn.close()
    sched2._conn.close()


def test_corrupt_next_run_catchup_is_loud(tmp_path, capsys):
    sched = TaskScheduler(make_cfg(tmp_path))
    task = make_task(next_run="not-a-timestamp")
    result = sched._missed_due_time(task, datetime.now())
    assert result is None
    assert "corrupt next_run" in capsys.readouterr().out
    sched._conn.close()


def test_malformed_once_spec_catchup_is_loud(tmp_path, capsys):
    sched = TaskScheduler(make_cfg(tmp_path))
    task = make_task(trigger_type="once", trigger_spec={}, next_run=None, last_run=None)
    result = sched._missed_due_time(task, datetime.now())
    assert result is None
    assert "malformed once-spec" in capsys.readouterr().out
    sched._conn.close()


async def test_task_failure_retries_once_then_records_error(tmp_path, capsys, monkeypatch):
    sched = TaskScheduler(make_cfg(tmp_path))

    calls = []

    class FailingToolbox:
        async def execute(self, action, args):
            calls.append(action)
            raise RuntimeError("tool is down")

    class FakeOrch:
        toolbox = FailingToolbox()

    sched._orchestrator = FakeOrch()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))  # no real 60s wait

    task = make_task(name="flaky")
    await sched._execute_task(task)

    assert len(calls) == 2, "exactly one retry"
    assert "tool is down" in (task.last_error or "")
    out = capsys.readouterr().out
    assert "flaky failed" in out and "retry also failed" in out
    assert task.next_run is not None, "recurring task must be rescheduled, not wedged"
    sched._conn.close()


async def test_task_success_clears_last_error(tmp_path):
    sched = TaskScheduler(make_cfg(tmp_path))

    class OkToolbox:
        async def execute(self, action, args):
            return "ok"

    class FakeOrch:
        toolbox = OkToolbox()

    sched._orchestrator = FakeOrch()
    task = make_task(name="healthy", last_error="old failure")
    await sched._execute_task(task)

    assert task.last_error is None
    assert task.run_count == 1
    sched._conn.close()


def test_past_once_task_catches_up(tmp_path):
    sched = TaskScheduler(make_cfg(tmp_path))
    past = (datetime.now() - timedelta(hours=2)).isoformat()
    task = make_task(
        trigger_type="once", trigger_spec={"run_at": past}, next_run=None, last_run=None
    )
    due = sched._missed_due_time(task, datetime.now())
    assert due is not None
    sched._conn.close()


def test_garbage_db_quarantines_and_boots_fresh(tmp_path, capsys):
    """CHAOS FINDING: a garbage scheduler.db raised DatabaseError out of the
    constructor — the orchestrator builds this in __init__ with no fallback,
    so one corrupt file killed the whole brain at boot. Now: quarantine aside
    (loud) and boot fresh."""
    db = tmp_path / "scheduler.db"
    db.write_bytes(b"\x00\xffgarbage-not-sqlite" * 64)
    sched = TaskScheduler(make_cfg(tmp_path))
    out = capsys.readouterr().out
    assert "database unusable" in out
    assert "quarantined" in out
    assert list(tmp_path.glob("scheduler.db.corrupt-*"))
    tid = sched.add_task(
        name="post-crash",
        trigger_type="once",
        trigger_spec={"run_at": "2099-01-01T00:00:00"},
        action="notify_reminder",
        action_args={"message": "hi"},
    )
    assert sched.get_task(tid) is not None
    sched._conn.close()

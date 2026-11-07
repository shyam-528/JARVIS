"""Regression tests for two known bugs from changes.txt:

1. "Jarvis can't delete remainders"  -> remove_scheduled_task must be a CORE
   tool (always sent to the LLM), support delete-by-name, and the scheduler
   must have find-by-name + name-based removal.
2. "background jobs output is not showing in website" -> _register_bg_result
   must publish an event the web layer can broadcast, and /api/chat must not
   silently swallow the bg_dispatch note.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from core.scheduler import TaskScheduler
from core.tools import CORE_TOOLS


def _cfg(tmp_path):
    return SimpleNamespace(db_path=tmp_path / "jarvis.db")


def test_remove_task_is_core_tool():
    # Delete must be in the always-sent core set, not a keyword-gated pack.
    assert "remove_scheduled_task" in CORE_TOOLS
    assert "list_scheduled_tasks" in CORE_TOOLS
    assert "schedule_task" in CORE_TOOLS


def test_scheduler_find_by_name(tmp_path):
    sched = TaskScheduler(_cfg(tmp_path))
    run_at = (datetime.now() + timedelta(hours=1)).isoformat()
    tid = sched.add_task(
        "gym reminder", "once", {"run_at": run_at}, "notify_reminder", {"message": "gym"}
    )
    found = sched.find_tasks_by_name("gym")
    assert any(t.id == tid for t in found)
    # partial, case-insensitive
    found2 = sched.find_tasks_by_name("GYM")
    assert any(t.id == tid for t in found2)


def test_scheduler_remove_by_name(tmp_path):
    sched = TaskScheduler(_cfg(tmp_path))
    run_at = (datetime.now() + timedelta(hours=1)).isoformat()
    sched.add_task(
        "water plants", "once", {"run_at": run_at}, "notify_reminder", {"message": "plants"}
    )
    removed = sched.remove_task_by_name("water plants")
    assert removed >= 1
    assert sched.find_tasks_by_name("water plants") == []


def test_register_bg_result_publishes_event():
    from core.orchestrator import Orchestrator

    published = []

    class FakeBus:
        def publish(self, t, payload):
            published.append((t, payload))

    class FakeMem:
        def add(self, *a, **k):
            pass

    orch = Orchestrator.__new__(Orchestrator)  # bypass heavy __init__
    orch._bg_notes = []
    orch.event_bus = FakeBus()
    orch.memory = FakeMem()

    task = SimpleNamespace(
        status="done",
        agent="coder",
        task_id="abc123",
        prompt="write tests",
        result="done: 5 tests",
    )
    Orchestrator._register_bg_result(orch, task)

    # result queued for next turn (existing behavior preserved)
    assert orch._bg_notes and orch._bg_notes[0]["task_id"] == "abc123"
    # AND an event published so the website can show it live
    assert any(t == "bg_job_done" for t, _ in published)

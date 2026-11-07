"""Phase 1: briefing content, dedup, scheduler catch-up, boot jobs."""

import asyncio
from pathlib import Path

from core.config import Config
from core.scheduler import TaskScheduler


def make_sched(tmp_path: Path) -> TaskScheduler:
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    sched = TaskScheduler(cfg)

    class _O:
        class toolbox:
            calls = []

            @staticmethod
            async def execute(action, args_json):
                _O.toolbox.calls.append((action, args_json))
                return "ok"

    sched.set_orchestrator(_O())
    return sched


def test_briefing_contains_sections():
    from skills.briefing import generate_briefing

    out = asyncio.run(generate_briefing("morning"))
    assert "sir" in out.lower()
    assert len(out) > 40


def test_notify_briefing_dedup(tmp_path: Path, monkeypatch):
    import skills.notify_skills as ns

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    from core.notify import NotificationConfig, NotificationManager

    calls = []
    mgr = NotificationManager(NotificationConfig())

    async def fake_notify(**kw):
        calls.append(kw)
        return {"success": True}

    mgr.notify = fake_notify
    from core.tools import ToolBox

    box = ToolBox()
    ns.register_notify(box, mgr)
    tool = box._tools["notify_briefing"]
    r1 = asyncio.run(tool.handler("morning"))
    r2 = asyncio.run(tool.handler("morning"))
    assert "sent" in r1.lower()
    assert "dedup" in r2.lower()
    assert len(calls) == 1


def test_boot_jobs_idempotent(tmp_path: Path):
    sched = make_sched(tmp_path)
    sched.add_task(
        "morning-briefing",
        "daily",
        {"hour": 8, "minute": 0},
        "notify_briefing",
        {"kind": "morning"},
    )
    sched.add_task(
        "morning-briefing",
        "daily",
        {"hour": 8, "minute": 0},
        "notify_briefing",
        {"kind": "morning"},
    )
    names = [t.name for t in sched.list_tasks()]
    assert names.count("morning-briefing") == 1


def test_catchup_fires_missed_once(tmp_path: Path):
    from datetime import datetime, timedelta

    sched = make_sched(tmp_path)
    past = (datetime.now() - timedelta(hours=2)).isoformat()
    sched.add_task("missed-once", "once", {"run_at": past}, "notify_reminder", {"message": "hi"})
    # start triggers catch-up
    asyncio.run(sched.start())
    try:
        t = [x for x in sched.list_tasks() if x.name == "missed-once"][0]
        assert t.run_count >= 1
    finally:
        asyncio.run(sched.stop())

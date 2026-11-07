"""One-tap routines: starting-work + winding-down bundles, on-demand."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import ToolBox


def _task(tid="t1", name="take pills", run_at=None, ttype="once", enabled=True, ran=False):
    now = datetime.now()
    return SimpleNamespace(
        id=tid,
        name=name,
        enabled=enabled,
        trigger_type=ttype,
        trigger_spec={"run_at": (run_at or (now - timedelta(hours=2))).isoformat()},
        run_count=1 if ran else 0,
        last_run="x" if ran else None,
        next_run=None,
    )


def _orch(tasks=None, triage="triage summary"):
    async def mail_triage(limit=20):
        return triage

    box = ToolBox()
    from core.tools import Tool

    box.register(
        Tool(
            name="mail_triage",
            description="x",
            parameters={"type": "object", "properties": {}},
            handler=mail_triage,
        )
    )
    return SimpleNamespace(scheduler=SimpleNamespace(list_tasks=lambda: tasks or []), toolbox=box)


async def test_morning_sections(monkeypatch):
    import core.calendar_feed as _cal
    import skills.briefing as _b
    from skills.routine_skills import register_routines

    now = datetime.now()
    orch = _orch([_task("a", "take pills", run_at=now - timedelta(hours=1))])

    async def _fake_brief(kind="morning"):
        return "BRIEF-" + kind

    monkeypatch.setattr(_b, "generate_briefing", _fake_brief)
    monkeypatch.setattr(
        _cal, "fetch_today_events", lambda *a, **k: [{"start": now, "summary": "Sync"}]
    )
    box = ToolBox()
    register_routines(box, orch)
    out = await box.execute("morning_start", "{}")
    assert "Starting work" in out
    assert "BRIEF-morning" in out and "triage summary" in out
    assert "take pills" in out and "Sync" in out


async def test_evening_tomorrow_preview(monkeypatch):
    import core.calendar_feed as _cal
    import skills.briefing as _b
    from skills.routine_skills import register_routines

    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    orch = _orch([_task("t", "pay bill", run_at=tomorrow)])

    async def _fake_review(kind="morning"):
        return "REVIEW"

    monkeypatch.setattr(_b, "generate_briefing", _fake_review)
    monkeypatch.setattr(
        _cal,
        "fetch_upcoming_events",
        lambda *a, **k: [
            {"start": tomorrow, "summary": "Dentist"},
            {"start": now + timedelta(days=9), "summary": "Far"},
        ],
    )
    box = ToolBox()
    register_routines(box, orch)
    out = await box.execute("evening_wrap", "{}")
    assert "Winding down" in out and "REVIEW" in out
    assert "Dentist" in out and "Far" not in out
    assert "pay bill" in out


async def test_degrades_gracefully_when_everything_down(monkeypatch):
    import core.calendar_feed as _cal
    import skills.briefing as _b
    from skills.routine_skills import register_routines

    def boom(*a, **k):
        raise RuntimeError("down")

    async def _boom(*a, **k):
        raise RuntimeError("down")

    orch = SimpleNamespace(scheduler=SimpleNamespace(list_tasks=boom))
    orch.toolbox = SimpleNamespace(execute=boom)
    monkeypatch.setattr(_b, "generate_briefing", _boom)
    monkeypatch.setattr(_cal, "fetch_today_events", boom)
    monkeypatch.setattr(_cal, "fetch_upcoming_events", boom)
    box = ToolBox()
    register_routines(box, orch)
    for tool in ("morning_start", "evening_wrap"):
        out = await box.execute(tool, "{}")
        assert "unavailable" in out  # never raises, still structured


async def test_routine_pack_detected():
    from core.tools import detect_packs

    assert "routine" in detect_packs("starting work now")
    assert "routine" in detect_packs("winding down for today")

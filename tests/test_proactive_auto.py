"""Phase C safe auto-act: dry-run, allowlist, schedule fence, caps, audit."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ProactiveConfig
from core.orchestrator import Orchestrator
from core.tools import Tool, ToolBox


def _orch(tmp_path, auto=False, dry=True):
    box = ToolBox()
    executed = []

    async def _remember(text="", kind="fact", **kw):
        executed.append(("remember", text))
        return f"remembered: {text[:40]}"

    async def _notify_reminder(message="", title="Reminder", **kw):
        executed.append(("notify_reminder", message))
        return "Reminder sent"

    async def _notify_alert(message="", title="ALERT", **kw):
        executed.append(("notify_alert", message))
        return "Alert sent"

    async def _schedule_task(
        name="",
        trigger_type="once",
        trigger_spec=None,
        action="",
        action_args=None,
        max_runs=None,
        **kw,
    ):
        executed.append(("schedule_task", name))
        return f"scheduled '{name}'"

    for name, fn in (
        ("remember", _remember),
        ("notify_reminder", _notify_reminder),
        ("notify_alert", _notify_alert),
        ("schedule_task", _schedule_task),
    ):
        box.register(
            Tool(
                name=name,
                description="x",
                parameters={"type": "object", "properties": {}},
                handler=fn,
            )
        )
    cfg = SimpleNamespace(
        proactive=ProactiveConfig(auto_act=False),
        db_path=tmp_path / "usage.db",
    )
    orch = SimpleNamespace(
        cfg=cfg,
        toolbox=box,
        scheduler=SimpleNamespace(list_tasks=lambda: []),
        proactive=SimpleNamespace(dismiss=lambda sid: True),
        history=[],
    )
    # bind real methods + class guards (SimpleNamespace lacks them)
    for m in (
        "_auto_state",
        "set_auto_state",
        "recent_acts",
        "_audit_act",
        "_auto_schedule_count",
        "_validate_auto_schedule",
        "_maybe_act_on_suggestion",
    ):
        setattr(orch, m, getattr(Orchestrator, m).__get__(orch))
    orch.AUTO_ACT_ALLOW = Orchestrator.AUTO_ACT_ALLOW
    orch.AUTO_SCHEDULE_MAX_PENDING = Orchestrator.AUTO_SCHEDULE_MAX_PENDING
    orch.AUTO_SCHEDULE_MAX_DAYS = Orchestrator.AUTO_SCHEDULE_MAX_DAYS
    orch.set_auto_state(auto=auto, dry=dry)
    orch._exec = executed
    return orch


def _sug(sid="s1", conf=0.95, action="notify_alert", args=None):
    return SimpleNamespace(
        id=sid,
        title="T",
        description="D",
        confidence=conf,
        action=action,
        action_args=args or {"message": "m"},
    )


def _sched_args(tmp_days=1, name="[auto] Follow up: x result"):
    run_at = (datetime.now() + timedelta(days=tmp_days)).replace(microsecond=0)
    return {
        "name": name,
        "trigger_type": "once",
        "trigger_spec": {"run_at": run_at.isoformat()},
        "action": "notify_reminder",
        "action_args": {"message": "Follow up on x result"},
    }


async def test_default_off_no_act(tmp_path):
    orch = _orch(tmp_path, auto=False, dry=True)
    assert orch._auto_state() == (False, True)
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug()) is None
    assert orch._exec == []


async def test_dry_announces_once_and_audits(tmp_path):
    orch = _orch(tmp_path, auto=True, dry=True)
    res1 = await Orchestrator._maybe_act_on_suggestion(orch, _sug())
    assert res1 and "would act" in res1
    assert orch._exec == []  # dry never executes
    res2 = await Orchestrator._maybe_act_on_suggestion(orch, _sug())
    assert res2 is None  # same id announced once
    rows = orch.recent_acts(10)
    assert rows and all(r["mode"] == "dry" for r in rows)


async def test_live_remember_executes_and_audits(tmp_path):
    orch = _orch(tmp_path, auto=True, dry=False)
    res = await Orchestrator._maybe_act_on_suggestion(
        orch, _sug(action="remember", args={"text": "likes chai", "kind": "habit"})
    )
    assert "remembered" in res
    assert orch._exec == [("remember", "likes chai")]
    rows = orch.recent_acts(5)
    assert rows and rows[-1]["mode"] == "live"


async def test_live_schedule_task_guarded(tmp_path):
    orch = _orch(tmp_path, auto=True, dry=False)
    res = await Orchestrator._maybe_act_on_suggestion(
        orch, _sug(sid="f1", action="schedule_task", args=_sched_args())
    )
    assert "scheduled" in res
    # missing [auto] title blocked
    bad = _sched_args(name="Follow up without prefix")
    assert orch._validate_auto_schedule(bad) and "auto" in orch._validate_auto_schedule(bad).lower()
    assert (
        await Orchestrator._maybe_act_on_suggestion(
            orch, _sug(sid="f2", action="schedule_task", args=bad)
        )
        is None
    )
    # non-once blocked
    bad2 = _sched_args()
    bad2["trigger_type"] = "daily"
    assert "once" in (orch._validate_auto_schedule(bad2) or "")
    # past + far-future blocked
    past = _sched_args()
    past["trigger_spec"] = {"run_at": (datetime.now() - timedelta(hours=1)).isoformat()}
    assert "past" in (orch._validate_auto_schedule(past) or "")
    far = _sched_args(tmp_days=9)
    assert "horizon" in (orch._validate_auto_schedule(far) or "")
    # wrong inner action blocked
    wrong = _sched_args()
    wrong["action"] = "notify_alert"
    assert "notify_reminder" in (orch._validate_auto_schedule(wrong) or "")
    # empty message blocked
    nomsg = _sched_args()
    nomsg["action_args"] = {}
    assert "message" in (orch._validate_auto_schedule(nomsg) or "")


async def test_cap_three_pending_blocks_fourth(tmp_path):
    orch = _orch(tmp_path, auto=True, dry=False)
    tasks = [SimpleNamespace(name=f"[auto] r{i}", run_count=0, last_run=None) for i in range(3)]
    orch.scheduler = SimpleNamespace(list_tasks=lambda: tasks)
    assert orch._auto_schedule_count() == 3
    assert "3 unrun" in (orch._validate_auto_schedule(_sched_args()) or "")
    assert (
        await Orchestrator._maybe_act_on_suggestion(
            orch, _sug(sid="cap1", action="schedule_task", args=_sched_args())
        )
        is None
    )
    # run one -> slot frees
    tasks[0].run_count = 1
    assert orch._auto_schedule_count() == 2
    assert orch._validate_auto_schedule(_sched_args()) is None


async def test_allowlist_and_confidence(tmp_path):
    orch = _orch(tmp_path, auto=True, dry=False)
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug(conf=0.5)) is None
    assert (
        await Orchestrator._maybe_act_on_suggestion(orch, _sug(action="run_command", args={}))
        is None
    )
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug(action=None)) is None
    # empty remember text blocked, no crash
    assert (
        await Orchestrator._maybe_act_on_suggestion(
            orch, _sug(action="remember", args={"text": "  "})
        )
        is None
    )


async def test_blocked_audited_and_set_persists(tmp_path):
    orch = _orch(tmp_path, auto=True, dry=False)
    bad = _sched_args(name="no prefix")
    await Orchestrator._maybe_act_on_suggestion(
        orch, _sug(sid="b1", action="schedule_task", args=bad)
    )
    rows = orch.recent_acts(10)
    assert any(r["mode"] == "blocked" for r in rows)
    orch.set_auto_state(auto=False)
    assert orch._auto_state()[0] is False
    data = json.loads((tmp_path / "proactive_auto.json").read_text(encoding="utf-8"))
    assert data["auto"] is False and data["dry"] is False


def test_followup_detector_emits_guarded_schedule(tmp_path):
    from core.proactive import ProactiveEngine

    cfg = SimpleNamespace(db_path=tmp_path / "usage.db")
    eng = ProactiveEngine(cfg, SimpleNamespace())
    hist = [{"role": "assistant", "tool_calls": [{"function": {"name": "web_search"}}]}]
    sugs = eng._detect_forgotten_followups(hist)
    assert sugs and sugs[0].action == "schedule_task"
    args = sugs[0].action_args
    assert args["name"].startswith("[auto]")
    assert args["trigger_type"] == "once"
    assert args["action"] == "notify_reminder"

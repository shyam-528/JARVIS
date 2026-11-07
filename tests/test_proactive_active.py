"""Proactive-active gate + failure producer + mail watcher baseline."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ProactiveConfig
from core.orchestrator import Orchestrator
from core.tools import Tool, ToolBox


def _stub(auto_act=False, executed=None):
    box = ToolBox()

    async def notify_alert(message="", title="", **kw):
        (executed if executed is not None else []).append(message)
        return "Alert sent"

    box.register(
        Tool(
            name="notify_alert",
            description="x",
            parameters={"type": "object", "properties": {}},
            handler=notify_alert,
        )
    )
    cfg = SimpleNamespace(proactive=ProactiveConfig(auto_act=auto_act))
    orch = SimpleNamespace(cfg=cfg, toolbox=box)
    # Phase C guards live on the class; stubs need them too.
    orch.AUTO_ACT_ALLOW = Orchestrator.AUTO_ACT_ALLOW
    orch.AUTO_SCHEDULE_MAX_PENDING = Orchestrator.AUTO_SCHEDULE_MAX_PENDING
    orch.AUTO_SCHEDULE_MAX_DAYS = Orchestrator.AUTO_SCHEDULE_MAX_DAYS
    for _m in ("_auto_state", "_audit_act", "_auto_schedule_count", "_validate_auto_schedule"):
        setattr(orch, _m, getattr(Orchestrator, _m).__get__(orch))
    return orch


def _sug(conf=0.95, action="notify_alert", args=None):
    return SimpleNamespace(
        title="T",
        description="D",
        confidence=conf,
        action=action,
        action_args=args or {"message": "m"},
    )


async def test_gate_off_by_default():
    orch = _stub(auto_act=False)
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug()) is None


async def test_gate_blocks_low_confidence_and_unsafe():
    orch = _stub(auto_act=True)
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug(conf=0.5)) is None
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug(action="run_command")) is None
    assert await Orchestrator._maybe_act_on_suggestion(orch, _sug(action=None)) is None


async def test_gate_executes_and_skips_history(monkeypatch):
    # Phase C: dry-run defaults ON, so force live for this execution test.
    monkeypatch.setenv("PROACTIVE_DRY_RUN", "0")
    executed = []
    orch = _stub(auto_act=True, executed=executed)
    orch.history = []
    res = await Orchestrator._maybe_act_on_suggestion(orch, _sug())
    assert res == "Alert sent" and executed == ["m"]
    assert orch.history == []


async def test_failed_tasks_once_each():
    orch = _stub()
    t = SimpleNamespace(id="t1", name="job", last_error="boom")
    orch.scheduler = SimpleNamespace(list_tasks=lambda: [t])
    first = Orchestrator._failed_task_suggestions(orch)
    second = Orchestrator._failed_task_suggestions(orch)
    assert len(first) == 1 and second == []
    assert first[0].action == "notify_alert" and first[0].confidence == 0.95


async def test_mailwatch_baseline_then_new(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    import core.mail as mailmod
    from skills.mail_skills import register_mailwatch

    box = ToolBox()
    sent = []

    class Manager:
        async def notify(self, **kw):
            sent.append(kw)
            return {"success": True}

    register_mailwatch(box, Manager())
    mails = {"emails": [{"id": "1", "from": "a", "subject": "s", "date": "d"}], "total": 1}
    monkeypatch.setattr(mailmod, "unread", lambda limit=25: dict(mails))
    monkeypatch.setattr(mailmod, "configured", lambda: True)
    r1 = await box.execute("mail_check_new", "{}")
    assert "baseline" in r1 and not sent
    r2 = await box.execute("mail_check_new", "{}")
    assert "no new mail" in r2 and not sent
    mails["emails"].append({"id": "2", "from": "b", "subject": "s2", "date": "d"})
    mails["total"] = 2
    r3 = await box.execute("mail_check_new", "{}")
    assert "triaged 1 new mail" in r3 and len(sent) == 1

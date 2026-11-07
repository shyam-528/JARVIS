"""Chaining: scheduler->workflow, rule->workflow (single tools compose)."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config, ProviderConfig, WatchdogConfig
from core.orchestrator import Orchestrator
from core.scheduler import TaskScheduler
from core.tools import Tool, ToolBox
from core.workflow import WorkflowEngine, WorkflowSpec, WorkflowStep


async def _echo(**kw):
    return f"echo:{kw.get('msg', '')}"


def _box():
    box = ToolBox()
    box.register(
        Tool(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {}},
            handler=_echo,
        )
    )
    return box


async def test_workflow_runs_tool_chain(tmp_path):
    from core.events import EventBus

    box = _box()
    orch = SimpleNamespace(toolbox=box)
    eng = WorkflowEngine(orch, EventBus(tmp_path / "events.db"), tmp_path / "wf.db")
    spec = WorkflowSpec(
        id="w1", name="demo", description="d", steps=[WorkflowStep(tool="echo", args={"msg": "hi"})]
    )
    eng._workflows[spec.id] = spec
    run_id = await eng.run("w1", background=False)
    runs = eng.conn.execute("SELECT status FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    assert runs[0] == "done"


async def test_scheduler_triggers_workflow_tool(tmp_path):
    ran = []

    async def run_workflow(workflow_id=None, **kw):
        ran.append(workflow_id)
        return f"ran {workflow_id}"

    box = _box()
    box.register(
        Tool(
            name="run_workflow",
            description="run a workflow",
            parameters={"type": "object", "properties": {}},
            handler=run_workflow,
        )
    )
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    sched = TaskScheduler(cfg)
    sched._orchestrator = SimpleNamespace(toolbox=box)
    tid = sched.add_task(
        "chain-demo", "interval", {"seconds": 3600}, "run_workflow", {"workflow_id": "w1"}
    )
    task = sched.get_task(tid)
    await sched._execute_task(task)
    assert ran == ["w1"]


async def test_rule_triggers_workflow_tool(tmp_path):
    p = ProviderConfig(
        name="t",
        base_url="https://t.test/v1",
        model="m",
        api_key="k",
        priority=0,
        daily_token_budget=10**9,
    )
    cfg = Config(
        jarvis_name="T",
        system_prompt="test",
        watchdog=WatchdogConfig(
            first_token_timeout_s=0.5, connect_timeout_s=2.0, read_timeout_s=5.0
        ),
        providers=[p],
        db_path=tmp_path / "usage.db",
    )
    orch = Orchestrator(cfg)

    async def gen(messages, tools):
        yield "ok"

    class C:
        def stream_chat(self, base_url, api_key, model, messages, tools=None):
            return gen(messages, tools)

    orch.router.client = C()

    async def run_workflow(workflow_id=None, **kw):
        return f"ran {workflow_id}"

    orch.toolbox.register(
        Tool(
            name="run_workflow",
            description="run a workflow",
            parameters={"type": "object", "properties": {}},
            handler=run_workflow,
        )
    )
    from core.automation import AutomationEngine

    eng = AutomationEngine(cfg, orch)
    eng.add_rule(
        "chain rule",
        {"type": "command", "pattern": "start chain"},
        [{"tool": "run_workflow", "args": {"workflow_id": "w1"}}],
    )
    orch._automation = eng
    out = []
    async for provider, chunk in orch.process("please start chain now"):
        out.append((provider, chunk))
    auto = [c for pr, c in out if pr == "auto"]
    assert auto and "ran w1" in auto[0], auto

"""Wave 7 failure-path tests for core.agent_team and core.agent_discovery.

Contract: pipeline step failures are recorded on the run + surfaced via the
pipeline_finished event; every best-effort side effect (auto-review,
on_complete, bg-note filing, discovery scans/announces) must degrade
gracefully but NEVER silently — a loud [AgentTeam]/[A2A Discovery] print.
"""

from __future__ import annotations

from core.agent_team import AgentTeam, PipelineRun, PipelineSpec, PipelineStep

# ---------- fakes ----------


class FakeEventBus:
    def __init__(self):
        self.published = []

    def publish(self, event_type, payload):
        self.published.append((event_type, payload))


class FakeOrch:
    def __init__(self):
        self.event_bus = FakeEventBus()
        self._bg_notes = []


class FakePool:
    """Stands in for AgentPool: agents dict + scripted run() async-gen."""

    def __init__(self, script=None):
        self.agents = {"coder": object(), "code_reviewer": object(), "fast": object()}
        # script: agent -> list of (provider, chunk) tuples, or Exception to raise
        self.script = script or {}
        self.calls = []

    async def run(self, agent, prompt):
        self.calls.append((agent, prompt))
        outcome = self.script.get(agent, [("model", "some output chunk")])
        if isinstance(outcome, Exception):
            raise outcome
        for provider, chunk in outcome:
            yield provider, chunk


def make_team(pool=None):
    pool = pool or FakePool()
    return AgentTeam(pool, FakeOrch()), pool


# ---------- agent_team: pipeline failure contract ----------


async def test_step_error_recorded_on_run_and_event_published():
    team, pool = make_team(FakePool({"coder": [("error", "model exploded")]}))
    spec = PipelineSpec(
        name="p", description="d", steps=[PipelineStep(agent="coder", instruction="do $input")]
    )
    run = PipelineRun(run_id="r1", pipeline="p", input_text="task")
    await team._execute(spec, run)

    assert run.status == "failed"
    assert any("model exploded" in e for e in run.errors)
    assert team.orch.event_bus.published[-1][0] == "pipeline_finished"
    assert team.orch.event_bus.published[-1][1]["status"] == "failed"


async def test_unexpected_step_exception_marks_run_failed_not_silent():
    team, pool = make_team(FakePool({"coder": RuntimeError("pool combusted")}))
    spec = PipelineSpec(
        name="p", description="d", steps=[PipelineStep(agent="coder", instruction="x")]
    )
    run = PipelineRun(run_id="r2", pipeline="p", input_text="task")
    await team._execute(spec, run)

    assert run.status == "failed"
    assert any("pool combusted" in e for e in run.errors)
    assert team.orch.event_bus.published[-1][1]["status"] == "failed"


async def test_auto_review_error_chunk_is_loud_and_returns_empty(capsys):
    team, pool = make_team(FakePool({"code_reviewer": [("error", "reviewer 500")]}))
    review = await team._auto_review("def f():\n" + "x = 1\n" * 30)
    assert review == ""
    out = capsys.readouterr().out
    assert "code_reviewer" in out and "reviewer 500" in out


async def test_auto_review_exception_is_loud_and_returns_empty(capsys):
    team, pool = make_team(FakePool({"code_reviewer": ConnectionError("pool down")}))
    review = await team._auto_review("def f():\n" + "x = 1\n" * 30)
    assert review == ""
    out = capsys.readouterr().out
    assert "code_reviewer" in out and "pool down" in out


async def test_on_complete_callback_failure_is_loud_and_not_raised(capsys):
    team, _ = make_team()

    async def bad_callback(run):
        raise ValueError("callback blew up")

    team.on_complete = bad_callback
    run = PipelineRun(run_id="r3", pipeline="p", input_text="t", status="done")
    await team._notify(run)  # must not raise

    out = capsys.readouterr().out
    assert "r3" in out and "callback blew up" in out
    # event still published before the failing callback
    assert team.orch.event_bus.published[-1][0] == "pipeline_finished"


async def test_auto_review_travels_in_result_only():
    """Single-delivery: the review is appended to task.result; NO separate
    note is filed (that surfaced the same review twice next turn)."""
    team, pool = make_team()

    class Task:
        agent = "coder"
        status = "done"
        task_id = "t-99"
        result = "def foo():\n" + "    pass\n" * 20  # long code-looking output

    task = Task()
    team.orch._bg_notes = []
    await team.maybe_auto_review(task)
    assert "AUTO-REVIEW" in task.result
    assert team.orch._bg_notes == []

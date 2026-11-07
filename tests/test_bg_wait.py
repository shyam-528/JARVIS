"""Background-agent handoff: wait_for + web inline-answer path (wait_bg)."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent_pool import AgentPool, BackgroundTask
from core.config import Config, ProviderConfig, WatchdogConfig
from core.orchestrator import Orchestrator


def _pool():
    return AgentPool(object())


async def test_wait_for_returns_done_task():
    pool = _pool()
    t = BackgroundTask(task_id="cod-001-abcd", agent="coder", prompt="x", status="running")
    pool.tasks[t.task_id] = t

    async def flip():
        await asyncio.sleep(0.05)
        t.status = "done"
        t.result = "did it"

    asyncio.create_task(flip())
    got = await pool.wait_for(t.task_id, timeout=5.0)
    assert got.result == "did it"
    assert got.status == "done"


async def test_wait_for_timeout_leaves_job_running():
    pool = _pool()
    t = BackgroundTask(task_id="cod-002-abcd", agent="coder", prompt="x", status="running")
    pool.tasks[t.task_id] = t
    with pytest.raises(TimeoutError):
        await pool.wait_for(t.task_id, timeout=0.05)
    assert pool.tasks[t.task_id].status == "running"


async def test_wait_for_unknown_id():
    with pytest.raises(KeyError):
        await _pool().wait_for("nope", timeout=0.05)


def _cfg(tmp_path):
    p = ProviderConfig(
        name="t",
        base_url="https://t.test/v1",
        model="m",
        api_key="k",
        priority=0,
        daily_token_budget=10**9,
    )
    return Config(
        jarvis_name="T",
        system_prompt="test",
        watchdog=WatchdogConfig(
            first_token_timeout_s=0.5, connect_timeout_s=2.0, read_timeout_s=5.0
        ),
        providers=[p],
        db_path=tmp_path / "usage.db",
    )


class _FakePool:
    """Pool stub: instant 4s-style job, configurable outcome."""

    def __init__(self, result="the note was added", status="done", provider="groq-test"):
        self.agents = {"coder": SimpleNamespace(icon="🤖")}
        self._result = result
        self._status = status
        self._provider = provider

    def submit(self, agent_name, prompt):
        return "cod-001-test"

    async def wait_for(self, task_id, timeout=120.0):
        return BackgroundTask(
            task_id=task_id,
            agent="coder",
            prompt="x",
            status=self._status,
            result=self._result,
            provider=self._provider,
        )


def _bg_orch(tmp_path, pool):
    orch = Orchestrator(_cfg(tmp_path))
    orch.auto_background = True
    orch._bg_classify = lambda user_input, enabled=True: SimpleNamespace(
        background=True, agent="coder", reason="heavy work (score 3)"
    )
    orch.agent_pool = pool
    return orch


async def test_web_wait_bg_answers_inline(tmp_path):
    orch = _bg_orch(tmp_path, _FakePool())
    out = [
        (p, c)
        async for p, c in orch.process(
            "please implement the whatsapp automation like mail", wait_bg=True
        )
    ]
    labels = [p for p, _ in out]
    assert "bg_dispatch" in labels  # handoff chip still shown first
    text = "".join(str(c) for p, c in out if p != "bg_dispatch")
    assert "the note was added" in text  # job result delivered inline


async def test_cli_fire_and_forget_unchanged(tmp_path):
    orch = _bg_orch(tmp_path, _FakePool())
    out = [
        (p, c)
        async for p, c in orch.process(
            "please implement the whatsapp automation like mail", wait_bg=False
        )
    ]
    assert [p for p, _ in out] == ["bg_dispatch"]
    assert "cod-001-test" in out[0][1]


async def test_web_wait_bg_timeout_falls_back_to_handoff(tmp_path):
    class _SlowPool(_FakePool):
        async def wait_for(self, task_id, timeout=120.0):
            raise TimeoutError("still running")

    orch = _bg_orch(tmp_path, _SlowPool())
    out = [
        (p, c)
        async for p, c in orch.process(
            "please implement the whatsapp automation like mail", wait_bg=True
        )
    ]
    # slow job: chip only, result lands via bg_notes next turn (old behavior)
    assert [p for p, _ in out] == ["bg_dispatch"]


async def test_web_wait_bg_failure_reported(tmp_path):
    # patch error onto the returned task via subclass instead
    class _FailPool(_FakePool):
        async def wait_for(self, task_id, timeout=120.0):
            t = BackgroundTask(
                task_id=task_id, agent="coder", prompt="x", status="failed", error="boom"
            )
            return t

    orch2 = _bg_orch(tmp_path, _FailPool())
    out = [
        (p, c)
        async for p, c in orch2.process(
            "please implement the whatsapp automation like mail", wait_bg=True
        )
    ]
    text = "".join(str(c) for p, c in out if p != "bg_dispatch")
    assert "FAILED" in text and "boom" in text

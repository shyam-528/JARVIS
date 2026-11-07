"""Offline draft: labeled local answer when all brains are down."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ["JARVIS_VENV_RELAUNCHED"] = "1"  # don't relaunch pytest under venv
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config, ProviderConfig, WatchdogConfig
from core.orchestrator import Orchestrator
from core.router import AllProvidersDown


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


async def test_offline_draft_uses_memory(tmp_path):
    orch = Orchestrator(_cfg(tmp_path))
    orch.memory.add("the deploy password is hunter2-hopefully-not")
    out = orch.offline_draft("what is the deploy password?")
    assert "Offline draft" in out
    assert "hunter2" in out


async def test_offline_draft_empty_state(tmp_path):
    orch = Orchestrator(_cfg(tmp_path))
    out = orch.offline_draft("quantum zebras?")
    assert "Offline draft" in out
    assert "nothing relevant" in out.lower() or "try again" in out.lower()


async def test_offline_draft_never_raises(tmp_path):
    orch = Orchestrator(_cfg(tmp_path))
    orch.memory = SimpleNamespace(search=lambda *a, **k: (_ for _ in ()).throw(Exception("x")))
    orch.knowledge_store = SimpleNamespace(
        search=lambda *a, **k: (_ for _ in ()).throw(Exception("y"))
    )
    out = orch.offline_draft("hi")
    assert isinstance(out, str) and "Offline draft" in out


async def test_chat_turn_returns_draft_on_blackout(tmp_path, monkeypatch):
    import jarvis as J

    orch = Orchestrator(_cfg(tmp_path))

    async def dead(text, surface="cli"):
        raise AllProvidersDown("nope")
        yield

    monkeypatch.setattr(orch, "process", dead)
    out = await J.chat_turn(orch, "hi")
    assert "Offline draft" in out

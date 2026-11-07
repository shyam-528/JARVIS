"""Automation rules: match, EXECUTE, enable/disable, validation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.automation import AutomationEngine
from core.config import Config, ProviderConfig, WatchdogConfig
from core.orchestrator import Orchestrator
from core.tools import Tool


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


class _Client:
    def __init__(self, text="ok"):
        self.text = text

    def stream_chat(self, base_url, api_key, model, messages, tools=None):
        async def gen():
            yield self.text

        return gen()


def _engine(tmp_path):
    cfg = _cfg(tmp_path)
    orch = Orchestrator(cfg)
    orch.router.client = _Client()
    return AutomationEngine(cfg, orch)


async def test_rule_fires_and_executes(tmp_path):
    cfg = _cfg(tmp_path)
    orch = Orchestrator(cfg)
    orch.router.client = _Client("done")

    async def ping():
        return "pong"

    orch.toolbox.register(
        Tool(
            name="ping",
            description="ping",
            parameters={"type": "object", "properties": {}},
            handler=ping,
        )
    )
    from core.automation import AutomationEngine

    eng = AutomationEngine(cfg, orch)
    eng.add_rule(
        "ping me", {"type": "command", "pattern": "ping me"}, [{"tool": "ping", "args": {}}]
    )
    orch._automation = eng
    out = []

    async for provider, chunk in orch.process("please ping me now"):
        out.append((provider, chunk))
    auto = [c for p, c in out if p == "auto"]
    assert auto and "pong" in auto[0], auto


async def test_no_match_no_fire(tmp_path):
    eng = _engine(tmp_path)
    eng.add_rule(
        "ping me", {"type": "command", "pattern": "ping me"}, [{"tool": "ping", "args": {}}]
    )
    assert eng.evaluate("command", {"text": "hello world"}) == []


async def test_disabled_visible_and_reenable(tmp_path):
    eng = _engine(tmp_path)
    rid = eng.add_rule("R", {"type": "command", "pattern": "r"}, [{"tool": "ping", "args": {}}])
    assert eng.enable_rule(rid, False) is True
    assert any(r["id"] == rid and r["enabled"] is False for r in eng.list_rules_formatted())
    assert eng.evaluate("command", {"text": "r"}) == []
    assert eng.enable_rule(rid, True) is True
    assert eng.evaluate("command", {"text": "r"}) != []


async def test_bad_regex_rejected(tmp_path):
    eng = _engine(tmp_path)
    with pytest.raises(ValueError):
        eng.add_rule("bad", {"type": "command", "pattern": "([unclosed"}, [])


async def test_id_collision_suffix(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_rule("Same Name", {"type": "command", "pattern": "a"}, [])
    b = eng.add_rule("Same Name", {"type": "command", "pattern": "b"}, [])
    assert a != b and b.startswith(a)

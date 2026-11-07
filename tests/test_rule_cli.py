"""Rule CLI: add/list/off/del parsing + engine round-trip."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["JARVIS_VENV_RELAUNCHED"] = "1"  # don't relaunch pytest under venv
from core.commands import _do_rule
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


async def _orch(tmp_path):
    orch = Orchestrator(_cfg(tmp_path))

    async def noop(**kw):
        return "ok"

    orch.toolbox.register(
        Tool(
            name="mytool",
            description="t",
            parameters={"type": "object", "properties": {}},
            handler=noop,
        )
    )
    return orch


async def test_add_command_rule(tmp_path, capsys):
    orch = await _orch(tmp_path)
    _do_rule(orch, '/rule add deploy-guard | deploy | do mytool {"a": 1}')
    eng = orch._automation
    rules = eng.list_rules_formatted()
    assert len(rules) == 1 and rules[0]["id"] == "deploy-guard"
    assert rules[0]["trigger"] == {"type": "command", "pattern": "deploy"}
    assert eng.evaluate("command", {"text": "lets deploy now"}) != []


async def test_add_tool_trigger(tmp_path):
    orch = await _orch(tmp_path)
    _do_rule(orch, "/rule add after-mail | tool:mail_unread | do mytool")
    rules = orch._automation.list_rules_formatted()
    assert rules[0]["trigger"] == {"type": "tool_result", "tool": "mail_unread"}


async def test_unknown_tool_rejected(tmp_path, capsys):
    orch = await _orch(tmp_path)
    _do_rule(orch, "/rule add x | y | do nosuchtool")
    assert orch._automation.list_rules_formatted() == []
    assert "unknown tool" in capsys.readouterr().out


async def test_bad_json_rejected(tmp_path, capsys):
    orch = await _orch(tmp_path)
    _do_rule(orch, "/rule add x | y | do mytool {bad}")
    assert orch._automation.list_rules_formatted() == []
    assert "bad args" in capsys.readouterr().out


async def test_on_off_del(tmp_path, capsys):
    orch = await _orch(tmp_path)
    _do_rule(orch, "/rule add t | hi | do mytool")
    _do_rule(orch, "/rule off t")
    assert orch._automation.evaluate("command", {"text": "hi there"}) == []
    _do_rule(orch, "/rule on t")
    assert orch._automation.evaluate("command", {"text": "hi there"}) != []
    _do_rule(orch, "/rule del t")
    assert orch._automation.list_rules_formatted() == []

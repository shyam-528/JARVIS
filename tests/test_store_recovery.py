"""P0 batch-2 tests: every remaining JSON store follows the shared
corrupt-file contract (quarantine aside + loud + safe fallback).

Each test corrupts the store file, exercises the normal load path, and
asserts: (1) the fallback value, (2) console-loudness, (3) a timestamped
quarantine file holding the original bytes (nothing deleted).
"""

from __future__ import annotations

from types import SimpleNamespace

from core import single_owner
from core.budget_guard import due_alerts, record_alerts
from core.persona import PersonaManager
from core.plugin_registry import PluginRegistry
from core.router import FailoverRouter
from core.skill_acquisition import SkillAcquisition


def _corrupt(path, text="{not valid json"):
    path.write_text(text, encoding="utf-8")


def _quarantines(path):
    return list(path.parent.glob(path.name + ".corrupt-*"))


# ---------- single_owner lock ----------


def test_corrupt_lock_quarantines_and_acquire_still_works(tmp_path, capsys):
    _corrupt(tmp_path / "telegram_owner.lock")
    assert single_owner.read_owner(tmp_path) is None
    out = capsys.readouterr().out
    assert "corrupt store telegram_owner.lock" in out
    assert _quarantines(tmp_path / "telegram_owner.lock")
    # fail-open direction unchanged: a new owner can take the lock
    assert single_owner.acquire(tmp_path, "cli") is True
    assert single_owner.is_owner(tmp_path) is True
    single_owner.release(tmp_path)


# ---------- budget_guard alert log ----------


def _providers():
    return [
        SimpleNamespace(name="groq-a", enabled=True, daily_token_budget=1000),
    ]


def _ledger():
    return SimpleNamespace(today=lambda name: 950)  # 5% left -> below WARN_PCT


def test_corrupt_alert_log_quarantines_and_realerts_safe_side(tmp_path, capsys):
    p = tmp_path / "alerts.json"
    _corrupt(p)
    due = due_alerts(_providers(), _ledger(), p)
    assert due == ["groq"]  # safe side: repeat today's alert, never skip it
    out = capsys.readouterr().out
    assert "corrupt store alerts.json" in out
    assert _quarantines(p)
    record_alerts(p, due)
    assert due_alerts(_providers(), _ledger(), p) == []  # recorded, no repeat


# ---------- router snapshot ----------


def test_corrupt_router_state_quarantines_and_starts_fresh(tmp_path, capsys):
    p = tmp_path / "router_state.json"
    _corrupt(p)
    imported = {}

    orch = SimpleNamespace(
        _state_path=p,
        breakers=SimpleNamespace(
            export=lambda: {},
            import_state=lambda d: imported.update(from_state=True, data=d),
        ),
        ledger=SimpleNamespace(TPM_LIMIT={}),
    )
    FailoverRouter._load_router_state(orch)
    out = capsys.readouterr().out
    assert "corrupt store router_state.json" in out
    assert _quarantines(p)
    assert imported["data"] == {}  # breakers start fresh, self-heal by probing


# ---------- skill_acquisition patterns ----------


def test_corrupt_skill_patterns_quarantine_and_reset(tmp_path, capsys):
    p = tmp_path / "skill_patterns.json"
    _corrupt(p)
    sa = SkillAcquisition(p)
    assert len(sa.patterns) == 0
    out = capsys.readouterr().out
    assert "corrupt store skill_patterns.json" in out
    assert _quarantines(p)


# ---------- persona ----------


def test_corrupt_persona_quarantines_and_keeps_defaults(tmp_path, capsys):
    p = tmp_path / "persona.json"
    _corrupt(p)
    pm = PersonaManager(config_path=p)
    assert pm.active_persona == "professional"
    assert "professional" in pm.personas
    out = capsys.readouterr().out
    assert "corrupt store persona.json" in out
    assert _quarantines(p)
    assert _quarantines(p)[0].read_text(encoding="utf-8") == "{not valid json"


# ---------- agent_pool custom agents ----------


def test_corrupt_custom_agents_quarantine_and_keep_builtins(tmp_path, capsys):
    from core.agent_pool import DEFAULT_AGENTS, AgentPool

    p = tmp_path / "custom_agents.json"
    _corrupt(p)
    pool = SimpleNamespace(CUSTOM_AGENTS_FILE=p, agents=dict(DEFAULT_AGENTS))
    AgentPool._load_custom_agents(pool)
    out = capsys.readouterr().out
    assert "corrupt store custom_agents.json" in out
    assert _quarantines(p)
    assert set(pool.agents) == set(DEFAULT_AGENTS)


# ---------- orchestrator auto-act toggles ----------


def test_auto_state_without_db_path_falls_back_to_config_flag():
    """Regression: _auto_state must never raise for partial configs —
    the pre-P0 try/except covered missing db_path, the quarantine must not
    have narrowed that."""
    from types import SimpleNamespace as _SN

    from core.config import ProactiveConfig
    from core.orchestrator import Orchestrator

    orch = _SN(cfg=_SN(proactive=ProactiveConfig(auto_act=True)))
    auto, dry = Orchestrator._auto_state(orch)
    assert auto is True and dry is True


def test_auto_state_corrupt_file_quarantines_and_plays_safe(tmp_path, capsys):
    from types import SimpleNamespace as _SN

    from core.config import ProactiveConfig
    from core.orchestrator import Orchestrator

    p = tmp_path / "proactive_auto.json"
    p.write_text('{"auto": true', encoding="utf-8")  # truncated JSON
    cfg = _SN(db_path=tmp_path / "state.db", proactive=ProactiveConfig(auto_act=False))
    auto, dry = Orchestrator._auto_state(_SN(cfg=cfg))
    assert (auto, dry) == (False, True)  # safe side: off/dry-on
    out = capsys.readouterr().out
    assert "corrupt store proactive_auto.json" in out
    assert list(tmp_path.glob("proactive_auto.json.corrupt-*"))


# ---------- plugin_registry index ----------


def _registry(tmp_path):
    cfg = SimpleNamespace(
        plugins_dir=tmp_path / "plugins",
        security_enabled=None,
    )
    return PluginRegistry(cfg, plugins_dir=tmp_path / "plugins")


def test_corrupt_plugin_index_quarantines_and_forgets_loudly(tmp_path, capsys):
    reg = _registry(tmp_path)
    _corrupt(reg.index_path)
    reg._load_index()
    assert reg.list_installed() == []
    out = capsys.readouterr().out
    assert "corrupt store" in out and "index.json" in out
    assert _quarantines(reg.index_path)

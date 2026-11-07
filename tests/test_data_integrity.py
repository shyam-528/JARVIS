"""Wave 2 data-integrity tests — pins the contracts of core/persistence.py,
core/memory_v2.py and core/preferences.py:

  - persistence.add_message refuses unknown sessions (no orphan messages);
  - persistence.search survives FTS-hostile user input;
  - memory_v2.find_skills_for is DB-backed (skills survive a restart);
  - memory_v2.search_all never starves a category at small k;
  - preferences set/get/remove share one key normalization;
  - a corrupt preferences file is moved aside (never silently emptied);
  - malformed preference entries are skipped individually.
"""

from __future__ import annotations

import json

import pytest

from core.config import Config
from core.memory_v2 import MemoryV2
from core.persistence import ConversationStore
from core.preferences import PreferenceManager


def _v2(tmp_path) -> MemoryV2:
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return MemoryV2(cfg)


# ── core/persistence.py ─────────────────────────────────────────────────


def test_add_message_unknown_session_raises(tmp_path):
    store = ConversationStore(tmp_path / "conv.db")
    with pytest.raises(ValueError, match="unknown session"):
        store.add_message("no-such-session", "user", "hello")
    # And no orphan row was written.
    n = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 0


def test_search_fts_hostile_input(tmp_path):
    store = ConversationStore(tmp_path / "conv.db")
    sid = store.create_session()
    store.add_message(sid, "user", "user prefers tabs over spaces")
    # Quotes / AND / parens would break a raw FTS5 MATCH — sanitized instead.
    results = store.search('tabs "over" AND (spaces OR')
    assert any("tabs" in r["content"] for r in results)


def test_search_blank_or_punct_returns_empty(tmp_path):
    store = ConversationStore(tmp_path / "conv.db")
    sid = store.create_session()
    store.add_message(sid, "user", "hello world")
    assert store.search("") == []
    assert store.search('"""') == []


# ── core/memory_v2.py ───────────────────────────────────────────────────


def test_find_skills_survives_restart(tmp_path):
    v2 = _v2(tmp_path)
    v2.add_skill(
        name="deploy-app",
        description="deploy the app",
        steps=["build", "push", "restart"],
        triggers=["deploy"],
    )
    v2.close()

    # Fresh instance: procedural_cache starts empty — the skill must still
    # be found because find_skills_for reads the DB.
    v2b = _v2(tmp_path)
    assert v2b.procedural_cache == {}
    skills = v2b.find_skills_for("please deploy the app")
    assert [s.name for s in skills] == ["deploy-app"]
    v2b.close()


def test_search_all_small_k_not_starved(tmp_path):
    v2 = _v2(tmp_path)
    v2.add_episodic(
        event_type="conversation",
        content="we talked about deploys",
        context={},
        importance=0.9,
    )
    out = v2.search_all("deploys", k=1)
    assert out["episodic"], "k=1 must not starve episodic to zero"
    v2.close()


# ── core/preferences.py ─────────────────────────────────────────────────


def test_set_get_remove_share_key_normalization(tmp_path):
    pm = PreferenceManager(tmp_path / "prefs.json")
    assert pm.set_value("My Favorite Color", "blue")
    # Same raw key the Settings UI would send back for delete/get.
    assert pm.get("My Favorite Color") == "blue"
    assert pm.remove("My Favorite Color") is True
    assert pm.get("My Favorite Color") is None
    assert pm.remove("My Favorite Color") is False


def test_preferences_roundtrip_persists(tmp_path):
    path = tmp_path / "prefs.json"
    pm = PreferenceManager(path)
    pm.set_value("theme", "dark")
    pm2 = PreferenceManager(path)
    assert pm2.get("theme") == "dark"


def test_corrupt_preferences_file_moved_aside(tmp_path):
    path = tmp_path / "prefs.json"
    path.write_text("{not valid json!!!", encoding="utf-8")
    pm = PreferenceManager(path)
    assert pm.list_all() == {}
    backups = list(tmp_path.glob("prefs.json.corrupt-*"))
    assert len(backups) == 1, "corrupt file must be preserved, not deleted"
    assert backups[0].read_text(encoding="utf-8") == "{not valid json!!!"
    # And the manager is usable immediately afterwards.
    assert pm.set_value("theme", "dark")
    assert json.loads(path.read_text(encoding="utf-8"))["theme"]["value"] == "dark"


def test_malformed_entries_skipped_individually(tmp_path):
    path = tmp_path / "prefs.json"
    good = {
        "key": "theme",
        "value": "dark",
        "category": "format",
        "confidence": 1.0,
        "source": "explicit",
        "created_at": 1.0,
        "updated_at": 1.0,
        "usage_count": 0,
    }
    path.write_text(
        json.dumps({"theme": good, "broken": {"not": "a preference"}}),
        encoding="utf-8",
    )
    pm = PreferenceManager(path)
    assert pm.get("theme") == "dark"
    assert "broken" not in pm.list_all()

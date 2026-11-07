"""P0 tests for the shared JSON-store contract (utils/json_utils.py).

Every file-backed store must follow the preferences.py rule: a corrupt file
is quarantined aside (never deleted, never silently emptied) and the failure
is loud. These tests pin the shared helper every migrated store delegates to.
"""

from __future__ import annotations

import json

from utils.json_utils import load_json_store, save_json_atomic


def test_missing_file_returns_default_quietly(tmp_path, capsys):
    out = load_json_store(tmp_path / "nope.json", default={"a": 1}, owner="t")
    assert out == {"a": 1}
    assert capsys.readouterr().out == ""


def test_default_is_deep_copied(tmp_path):
    default = {"items": []}
    first = load_json_store(tmp_path / "nope.json", default=default, owner="t")
    first["items"].append("x")
    second = load_json_store(tmp_path / "nope.json", default=default, owner="t")
    assert second == {"items": []}


def test_corrupt_json_quarantined_and_loud(tmp_path, capsys):
    p = tmp_path / "storm_queue.json"
    p.write_text("{not valid json", encoding="utf-8")
    out = load_json_store(p, default={"items": []}, owner="storm")
    assert out == {"items": []}
    assert not p.exists()
    backups = list(tmp_path.glob("storm_queue.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"
    printed = capsys.readouterr().out
    assert "corrupt store storm_queue.json" in printed
    assert "quarantined" in printed


def test_wrong_shape_quarantined(tmp_path, capsys):
    p = tmp_path / "prefs.json"
    p.write_text('["not", "a", "dict"]', encoding="utf-8")
    out = load_json_store(p, default={}, owner="prefs", expect=dict)
    assert out == {}
    assert list(tmp_path.glob("prefs.json.corrupt-*"))
    assert "expected dict" in capsys.readouterr().out


def test_unreadable_file_left_alone(tmp_path, capsys, monkeypatch):
    from pathlib import Path

    p = tmp_path / "locked.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied"))
    )
    out = load_json_store(p, default={}, owner="t")
    assert out == {}
    assert p.exists()  # never quarantine a file we couldn't read
    assert not list(tmp_path.glob("locked.json.corrupt-*"))
    assert "denied" in capsys.readouterr().out


def test_quarantine_failure_still_returns_default(tmp_path, capsys, monkeypatch):
    import os

    p = tmp_path / "bad.json"
    p.write_text("{{{", encoding="utf-8")
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert load_json_store(p, default=[], owner="t") == []
    assert "could not quarantine" in capsys.readouterr().out


def test_save_round_trip(tmp_path):
    p = tmp_path / "sub" / "s.json"
    assert save_json_atomic(p, {"items": [1, 2]}) is True
    assert json.loads(p.read_text(encoding="utf-8")) == {"items": [1, 2]}
    assert not list(tmp_path.rglob("*.tmp"))


def test_save_failure_is_loud_false(tmp_path, capsys, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(
        Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert save_json_atomic(tmp_path / "s.json", {}) is False
    assert "failed to save s.json" in capsys.readouterr().out

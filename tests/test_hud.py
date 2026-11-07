"""Tests for the HUD data layer (roadmap step B)."""

from __future__ import annotations

import sqlite3
import time

import hud


def _seed(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(hud, "DATA", data)

    j = sqlite3.connect(data / "surface_journal.db")
    j.execute("CREATE TABLE surface_turns (id TEXT, surface TEXT, role TEXT, text TEXT, ts REAL)")
    j.execute(
        "INSERT INTO surface_turns VALUES ('t1', 'telegram', 'user', 'hello from phone', ?)",
        (time.time() - 120,),
    )
    j.commit()
    j.close()

    s = sqlite3.connect(data / "scheduler.db")
    s.execute("CREATE TABLE tasks (id TEXT, name TEXT, enabled INTEGER, next_run TEXT)")
    s.execute("INSERT INTO tasks VALUES ('a', 'water', 1, '2026-09-21T18:00:00')")
    s.execute("INSERT INTO tasks VALUES ('b', 'disabled one', 0, '2026-09-21T09:00:00')")
    s.commit()
    s.close()

    e = sqlite3.connect(data / "events.db")
    e.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts REAL, type TEXT, payload TEXT)")
    e.execute(
        "INSERT INTO events (ts, type, payload) VALUES (?, 'bg_job_done', ?)",
        (time.time() - 60, '{"agent": "writer", "task_id": "wri-1"}'),
    )
    e.commit()
    e.close()


def test_fetch_status_reads_all_sources(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    s = hud.fetch_status()
    assert "hello from phone" in s["activity"]
    assert "telegram" in s["activity"]
    assert "water" in s["reminder"]
    assert "disabled one" not in s["reminder"]  # disabled tasks excluded
    assert "writer" in s["job"] and "wri-1" in s["job"]


def test_fetch_status_survives_missing_dbs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(hud, "DATA", data)  # no db files at all
    s = hud.fetch_status()
    assert s["activity"] is None and s["reminder"] is None and s["job"] is None


def test_fetch_status_survives_wrong_schema(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(hud, "DATA", data)
    for name in ("surface_journal.db", "scheduler.db", "events.db"):
        db = sqlite3.connect(data / name)
        db.execute("CREATE TABLE junk (x TEXT)")
        db.commit()
        db.close()
    s = hud.fetch_status()  # must not raise
    assert s["activity"] is None


def test_ago_formatting():
    assert hud._ago(time.time() - 30).endswith("s ago")
    assert hud._ago(time.time() - 300).endswith("m ago")
    assert hud._ago(time.time() - 7200).endswith("h ago")
    assert hud._ago(time.time() - 3 * 86400).endswith("d ago")

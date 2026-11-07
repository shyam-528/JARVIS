"""Tests for core/db.py — the shared SQLite helper and its maintenance job.

Locks the quick-win hardening: daily WAL checkpoint + conditional VACUUM over
data/*.db, best-effort per DB, never raises.
"""

from __future__ import annotations

from core.db import connect, maintenance


def _make_db(path, rows: int = 0) -> None:
    conn = connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (data TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [("x" * 300,)] * rows)
    conn.commit()
    conn.close()


def test_maintenance_checkpoints_healthy_db(tmp_path):
    _make_db(tmp_path / "ok.db", rows=100)
    report = maintenance(tmp_path)
    info = report["ok.db"]
    assert info["ok"] and info["checkpointed"]
    assert info["error"] is None
    # Small DB with no churn must NOT be vacuumed (no pointless rewrites).
    assert not info["vacuumed"]


def test_maintenance_vacuums_bloated_db(tmp_path):
    db = tmp_path / "bloated.db"
    _make_db(db, rows=6000)  # ~2MB of rows
    size_before = db.stat().st_size
    conn = connect(db)
    conn.execute("DELETE FROM t")  # everything to the freelist
    conn.commit()
    conn.close()

    report = maintenance(tmp_path)
    info = report["bloated.db"]
    assert info["ok"]
    assert info["vacuumed"], "freelist >>10% and >1MB should trigger VACUUM"
    assert db.stat().st_size < size_before
    assert info["reclaimed_bytes"] > 0


def test_maintenance_truncates_wal_file(tmp_path):
    db = tmp_path / "wal.db"
    _make_db(db, rows=10)
    # Force a non-trivial WAL: write on one connection without closing it.
    conn = connect(db)
    conn.executemany("INSERT INTO t VALUES (?)", [("y" * 500,)] * 2000)
    conn.commit()  # committed but not checkpointed while conn stays open
    report = maintenance(tmp_path)
    conn.close()
    assert report["wal.db"]["checkpointed"]
    wal = tmp_path / "wal.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_maintenance_never_raises_on_garbage_db(tmp_path):
    (tmp_path / "broken.db").write_bytes(b"this is not sqlite")
    (tmp_path / "notes.txt").write_text("not a db")  # non-.db ignored
    report = maintenance(tmp_path)  # must not raise
    assert "notes.txt" not in report
    info = report["broken.db"]
    assert not info["ok"]
    assert info["error"], "garbage DB must record an error, not crash the job"


def test_maintenance_empty_dir(tmp_path):
    assert maintenance(tmp_path) == {}

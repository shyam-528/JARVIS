"""Memory consolidation: promote facts, never delete, report trivia."""

from pathlib import Path

from core.consolidation import consolidate, format_stats
from core.memory import MemoryStore
from core.preferences import PreferenceManager


def _setup(tmp_path: Path):
    mem = MemoryStore(tmp_path / "memory.db")
    prefs = PreferenceManager(tmp_path / "prefs.json")
    return mem, prefs


def test_promotes_preference_like_fact(tmp_path: Path):
    mem, prefs = _setup(tmp_path)
    mem.add("I prefer dark mode everywhere")
    stats = consolidate(mem, prefs)
    assert stats["scanned"] == 1
    assert stats["promoted"] == 1
    assert len(prefs.list_all()) >= 1
    # original memory still there, retagged
    assert mem.count() == 1
    kind = mem.conn.execute("SELECT kind FROM memories").fetchone()[0]
    assert kind == "promoted"


def test_never_deletes(tmp_path: Path):
    mem, prefs = _setup(tmp_path)
    mem.add("random trivia note xyz")
    mem.add("buy milk tomorrow")
    before = mem.count()
    stats = consolidate(mem, prefs)
    assert mem.count() == before
    assert stats["scanned"] == before


def test_review_flags_old_short(tmp_path: Path):
    mem, prefs = _setup(tmp_path)
    mem.add("ok")
    mem.conn.execute("UPDATE memories SET ts = '2020-01-01 00:00'")
    mem.conn.commit()
    stats = consolidate(mem, prefs, stale_days=90)
    assert stats["review_candidates"] == 1


def test_format_stats():
    out = format_stats(
        {
            "scanned": 5,
            "promoted": 1,
            "promoted_samples": ["x"],
            "review_candidates": 0,
            "review_samples": [],
            "preferences_total": 3,
        }
    )
    assert "scanned 5" in out and "clean" in out

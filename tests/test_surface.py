"""Tests for the cross-device surface journal (roadmap step 3)."""

from __future__ import annotations

import time

from core.surface import SurfaceJournal


def _journal(tmp_path):
    return SurfaceJournal(tmp_path / "journal.db")


def test_add_and_read_cross_surface(tmp_path):
    j = _journal(tmp_path)
    j.add("telegram", "user", "draft that mail")
    j.add("telegram", "assistant", "done, sir")
    j.add("cli", "user", "hello from the terminal")

    # cli sees only the telegram turns
    turns = j.recent_cross("cli")
    assert len(turns) == 2
    assert all(t["surface"] == "telegram" for t in turns)
    assert turns[0]["text"] == "draft that mail"  # oldest first

    # telegram sees only the cli turn
    turns = j.recent_cross("telegram")
    assert len(turns) == 1
    assert turns[0]["text"] == "hello from the terminal"


def test_context_block_format(tmp_path):
    j = _journal(tmp_path)
    j.add("web", "user", "what's the weather")
    j.add("web", "assistant", "sunny, sir")
    block = j.context_block("cli")
    assert "other devices" in block
    assert "[web] You: what's the weather" in block
    assert "[web] JARVIS: sunny, sir" in block


def test_context_block_empty_when_no_cross_turns(tmp_path):
    j = _journal(tmp_path)
    j.add("cli", "user", "same surface only")
    assert j.context_block("cli") == ""


def test_empty_text_not_stored(tmp_path):
    j = _journal(tmp_path)
    assert j.add("cli", "user", "   ") == ""
    assert j.recent_cross("web") == []


def test_old_turns_excluded(tmp_path):
    j = _journal(tmp_path)
    j.add("telegram", "user", "recent one")
    # backdate a turn to 3 days ago
    j.conn.execute(
        "INSERT INTO surface_turns (id, surface, role, text, ts) VALUES (?,?,?,?,?)",
        ("old1", "telegram", "user", "ancient", time.time() - 3 * 86400),
    )
    j.conn.commit()
    turns = j.recent_cross("cli")
    assert len(turns) == 1
    assert turns[0]["text"] == "recent one"


def test_broken_db_never_raises(tmp_path):
    j = _journal(tmp_path)
    j.conn.close()  # simulate a dead connection
    assert j.add("cli", "user", "hello") == ""
    assert j.recent_cross("cli") == []
    assert j.context_block("cli") == ""


def test_two_writers_share_one_file(tmp_path):
    a = _journal(tmp_path)
    b = _journal(tmp_path)  # second process on the same file (WAL)
    a.add("cli", "user", "from the PC")
    b.add("telegram", "user", "from the phone")
    assert len(a.recent_cross("web")) == 2
    assert any(t["text"] == "from the PC" for t in b.recent_cross("web"))

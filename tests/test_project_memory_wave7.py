"""Wave 7 failure-path tests for core.project_memory.

Contract: methods never raise — {"ok": bool, "error": str}. A corrupt memory
file is loud (it would silently wipe all project memory); a failed save is
reported as {"ok": False} — never claims success for data not on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.project_memory import ProjectMemory


@pytest.fixture
def mem(tmp_path):
    return ProjectMemory(tmp_path)


def test_corrupt_memory_file_is_loud_and_empty(mem, capsys):
    mem._path.write_text("{not json", encoding="utf-8")
    assert mem.list() == []
    out = capsys.readouterr().out
    assert "corrupt store project_memory.json" in out
    # P0: quarantined aside (recoverable), not silently wiped
    assert list(mem._path.parent.glob("project_memory.json.corrupt-*"))
    assert "quarantined" in out


def test_missing_memory_file_is_quiet(mem, capsys):
    assert mem.list() == []
    assert capsys.readouterr().out == ""


def test_remember_reports_save_failure(mem, capsys, monkeypatch):
    def boom(self, other):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    result = mem.remember("mark3", "convention", "use ruff")
    assert result["ok"] is False
    assert "persist" in result["error"]
    assert "failed to persist memory" in capsys.readouterr().out


def test_forget_reports_save_failure(mem, capsys, monkeypatch):
    assert mem.remember("mark3", "note", "remember this")["ok"] is True

    def boom(self, other):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    result = mem.forget("mark3", "remember this")
    assert result["ok"] is False
    assert "persist" in result["error"]


def test_remember_forget_roundtrip(mem):
    assert mem.remember("mark3", "convention", "always run ruff")["ok"] is True
    assert "always run ruff" in mem.recall("mark3")
    assert mem.forget("mark3", "always run ruff")["ok"] is True
    assert "always run ruff" not in mem.recall("mark3")


def test_remember_validates_input(mem):
    assert mem.remember("", "note", "x")["ok"] is False
    assert mem.remember("p", "note", "")["ok"] is False
    assert mem.remember("p", "bogus-kind", "x")["ok"] is False


def test_forget_unknown_project_is_error_not_crash(mem):
    assert mem.forget("ghost-project")["ok"] is False

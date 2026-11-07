"""Workspace map: routing, guardrail, and guidance presence.

Covers core/workspace.py (route/resolve/check_write_path) plus the
model-facing placement hints in tool descriptions and agent suffixes.
"""

from __future__ import annotations

from pathlib import Path

from core.workspace import BULK_ROOT, REPO_ROOT, check_write_path, kind_dir, resolve, route


def test_route_auto_sniffs_media_and_logs(tmp_path):
    assert route("clip.mp3", bulk_root=tmp_path) == tmp_path / "media" / "clip.mp3"
    assert route("shot.png", bulk_root=tmp_path) == tmp_path / "media" / "shot.png"
    assert route("run.log") == REPO_ROOT / "logs" / "run.log"


def test_route_explicit_kind_wins(tmp_path):
    assert route("clip.mp3", kind="log") == REPO_ROOT / "logs" / "clip.mp3"
    assert route("report.md", kind="plan") == REPO_ROOT / "docs" / "plans" / "report.md"
    assert route("deep.txt", kind="research") == REPO_ROOT / "data" / "research" / "deep.txt"
    assert route("helper.py", kind="script") == REPO_ROOT / "workspace" / "scripts" / "helper.py"


def test_route_default_lands_in_workspace():
    assert route("notes.txt") == REPO_ROOT / "workspace" / "notes.txt"
    assert route("") == REPO_ROOT / "workspace" / "untitled.txt"


def test_kind_dir_bulk_vs_repo(tmp_path):
    assert kind_dir("media", tmp_path) == tmp_path / "media"
    assert kind_dir("plan") == REPO_ROOT / "docs" / "plans"
    assert kind_dir("bogus") == REPO_ROOT / "workspace"


def test_resolve_keeps_source_edits_and_absolutes(tmp_path):
    assert resolve("core/code_agent.py") == REPO_ROOT / "core" / "code_agent.py"
    assert resolve("D:/jarvis/media/x.mp3") == Path("D:/jarvis/media/x.mp3")
    assert resolve("note.txt") == REPO_ROOT / "workspace" / "note.txt"


def test_guardrail_refuses_escape_and_system():
    ok, _ = check_write_path("../outside.txt")
    assert ok is False
    ok, reason = check_write_path("C:/Windows/evil.txt")
    assert ok is False and "system" in reason
    ok, _ = check_write_path("C:/")
    assert ok is False


def test_guardrail_allows_repo_bulk_home():
    assert check_write_path("core/code_agent.py")[0] is True
    assert check_write_path("docs/plans/new.md")[0] is True
    assert check_write_path("D:/jarvis/media/clip.mp3")[0] is True
    assert check_write_path("C:/Users/krith/notes/todo.txt")[0] is True
    assert check_write_path("")[0] is False


def test_guidance_strings_present():
    from core.agent_pool import DEFAULT_AGENTS

    assert "docs/plans" in DEFAULT_AGENTS["coder"].system_prompt_suffix
    assert "data/research" in DEFAULT_AGENTS["researcher"].system_prompt_suffix
    assert "workspace" in DEFAULT_AGENTS["writer"].system_prompt_suffix


def test_bulk_root_defaults_to_d_drive():
    assert str(BULK_ROOT).replace("\\", "/").upper().startswith("D:/")

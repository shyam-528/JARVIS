"""Phase 2 dev loop: init/review/test/pr-guard/standup."""

from pathlib import Path

from core.tools import ToolBox
from skills.dev_loop import register_dev_loop


def _box() -> ToolBox:
    box = ToolBox()
    register_dev_loop(box)
    return box


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_init_writes_config(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    box = _box()
    out = _run(box.execute("dev_init", '{"path": "."}'))
    assert ".jarvis.json" in out
    assert (tmp_path / ".jarvis.json").exists()


def test_review_clean_tree(tmp_path: Path, monkeypatch):
    import subprocess

    monkeypatch.chdir(tmp_path)
    subprocess.run("git init -q", shell=True, check=False)
    subprocess.run("git config user.email t@t.t", shell=True, check=False)
    subprocess.run("git config user.name t", shell=True, check=False)
    (tmp_path / "a.txt").write_text("hi")
    subprocess.run("git add . && git commit -qm init", shell=True, check=False)
    box = _box()
    out = _run(box.execute("dev_review", '{"path": "."}'))
    assert "clean" in out.lower() or "nothing" in out.lower()


def test_pr_refuses_main(tmp_path: Path, monkeypatch):
    import subprocess

    monkeypatch.chdir(tmp_path)
    subprocess.run("git init -q -b main", shell=True, check=False)
    box = _box()
    out = _run(box.execute("dev_pr", '{"title": "x"}'))
    assert "refused" in out.lower() or "main" in out.lower()


def test_blocked_command():
    from skills.dev_loop import _run

    r = _run("rm -rf / --no-preserve-root", cwd=".")
    assert "error" in r and "blocked" in r["error"].lower()

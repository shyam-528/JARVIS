"""Phase 2 dev loop: init/review/test/pr/standup. Read-only by default, guarded writes."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from core.tools import Tool, ToolBox

try:
    import re as _re

    from core.code_agent import BLOCKED_PATTERNS

    def _blocked(cmd: str) -> str | None:
        for pat in BLOCKED_PATTERNS:
            if _re.search(pat, cmd, _re.IGNORECASE):
                return pat
        return None
except Exception:

    def _blocked(cmd: str) -> str | None:  # type: ignore
        return None


def _run(cmd: str, cwd: str = ".", timeout: int = 120) -> dict:
    b = _blocked(cmd)
    if b:
        return {"error": f"blocked by policy ({b})"}
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return {"code": r.returncode, "output": out[-6000:]}
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s: {cmd[:120]}"}
    except Exception as exc:
        return {"error": str(exc)}


def _detect_type(root: Path) -> tuple[str, str]:
    if (
        (root / "pyproject.toml").exists()
        or (root / "setup.py").exists()
        or (root / "requirements.txt").exists()
    ):
        return "python", "pytest -q"
    if (root / "package.json").exists():
        return "node", "npm test --silent"
    if (root / "Cargo.toml").exists():
        return "rust", "cargo test --quiet"
    if (root / "go.mod").exists():
        return "go", "go test ./..."
    return "unknown", "pytest -q"


def register_dev_loop(box: ToolBox, knowledge_indexer=None, scheduler=None):
    async def dev_init(path: str = ".", index: bool = False) -> str:
        root = Path(path).resolve()
        if not root.exists():
            return f"error: {path} not found"
        ptype, test_cmd = _detect_type(root)
        cfg = {"root": str(root), "type": ptype, "test_cmd": test_cmd, "default_branch": "main"}
        try:
            (root / ".jarvis.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as exc:
            return f"error writing .jarvis.json: {exc}"
        if not index or knowledge_indexer is None:
            return (
                f"init {root.name} [{ptype}] test: `{test_cmd}`. Saved .jarvis.json. "
                f"Index skipped (fast) — run `dev_init path index=true` or `/index {root}` to index code for Q&A."
            )
        # Opt-in only: embedding up to 500 files blocks minutes on first run
        # (model download + CPU encode). Runs sync; prefer small repos.
        try:
            res = knowledge_indexer.index_directory(str(root))
            return (
                f"init {root.name} [{ptype}] test: `{test_cmd}`. Saved .jarvis.json. "
                f"Indexed {res.get('files_indexed', '?')}/{res.get('files_found', '?')} files."
            )
        except Exception as exc:
            return f"init {root.name} [{ptype}]. Saved .jarvis.json. Index failed: {exc}"

    async def dev_review(path: str = ".") -> str:
        inside = _run("git rev-parse --is-inside-work-tree", cwd=path)
        if inside.get("code", 1) != 0 or "true" not in (inside.get("output", "") or "").lower():
            return (
                "not a git repo (or no commits yet) — nothing to review.\n"
                "Run `git init && git add . && git commit -m init` first, or point /review at a git checkout."
            )
        r = _run("git diff --stat", cwd=path)
        if r.get("code", 1) != 0:
            return f"git diff failed:\n{(r.get('output') or r.get('error', ''))[-1500:]}"
        d = _run("git diff", cwd=path)
        if d.get("code", 1) != 0:
            return f"git diff failed:\n{(d.get('output') or d.get('error', ''))[-1500:]}"
        diff = (d.get("output", "") or "")[:8000]
        if not diff.strip():
            return "working tree clean — nothing to review."
        return (
            "Review this diff (read-only, do NOT edit). Report: bugs, edge cases, "
            "security (secrets/injection), failing paths.\n\n"
            f"STAT:\n{r.get('output', '')[:1500]}\n\nDIFF:\n{diff}"
        )

    async def dev_test(path: str = ".", cmd: str = "") -> str:
        if not cmd:
            try:
                j = json.loads((Path(path) / ".jarvis.json").read_text(encoding="utf-8"))
                cmd = j.get("test_cmd", "pytest -q")
            except Exception:
                _, cmd = _detect_type(Path(path))
        r = _run(cmd, cwd=path, timeout=300)
        if "error" in r:
            return f"error: {r['error']}"
        status = "PASS" if r["code"] == 0 else f"FAIL({r['code']})"
        return f"{status} `{cmd}`:\n{r['output'][-4000:]}"

    async def dev_pr(title: str, body: str = "", path: str = ".") -> str:
        branch = ""
        for bc in (
            "git branch --show-current",
            "git rev-parse --abbrev-ref HEAD",
            "git symbolic-ref --short HEAD",
        ):
            br = _run(bc, cwd=path)
            cand = (br.get("output", "") or "").strip().splitlines()
            cand = cand[-1].strip() if cand else ""
            if cand and cand not in ("HEAD", ""):
                branch = cand
                break
        if branch in ("main", "master"):
            return "refused: on main/master — create a feature branch first (`git checkout -b feat/x`)."
        st = _run("git status --porcelain", cwd=path).get("output", "")
        if not st.strip() and not title:
            return "nothing to PR: clean tree and no title."
        r = _run(
            f"gh pr create --title {shlex.quote(title)} --body {shlex.quote(body or title)}",
            cwd=path,
        )
        if "error" in r:
            return f"error: {r['error']}"
        if r["code"] != 0:
            return f"gh failed ({r['code']}):\n{r['output'][-2000:]}\nHint: `gh auth login` first."
        return f"PR created:\n{r['output'][-2000:]}"

    async def dev_standup(path: str = ".") -> str:
        log = _run("git log --since='24 hours ago' --oneline -15", cwd=path).get("output", "")
        stat = _run("git status --porcelain", cwd=path).get("output", "")
        lines = ["Standup (24h):"]
        lines.append(f"commits:\n{log[:1500]}" if log.strip() else "commits: none")
        lines.append(f"dirty:\n{stat[:1000]}" if stat.strip() else "tree: clean")
        if scheduler is not None:
            try:
                ts = scheduler.list_tasks()
                lines.append(f"scheduled: {len(ts)} tasks")
            except Exception:
                pass
        return "\n".join(lines)[:3000]

    box.register(
        Tool(
            name="dev_init",
            description="Init repo for Jarvis: detect type, write .jarvis.json. Set index=true to also embed code (SLOW, minutes on big repos).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "index": {"type": "boolean", "default": False},
                },
            },
            handler=dev_init,
        )
    )
    box.register(
        Tool(
            name="dev_review",
            description="Read-only review input: git diff stat + diff excerpt for LLM review.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
            handler=dev_review,
        )
    )
    box.register(
        Tool(
            name="dev_test",
            description="Run project tests (auto from .jarvis.json or detect).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "cmd": {"type": "string", "default": ""},
                },
            },
            handler=dev_test,
        )
    )
    box.register(
        Tool(
            name="dev_pr",
            description="Create GitHub PR via gh. Refuses on main/master.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string", "default": ""},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["title"],
            },
            handler=dev_pr,
        )
    )
    box.register(
        Tool(
            name="dev_standup",
            description="24h commits + dirty tree + scheduled count.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
            handler=dev_standup,
        )
    )

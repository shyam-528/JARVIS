"""Workspace map: where Jarvis-created files belong.

Root cause of the repo-root litter (changes.txt line 22): file tools resolve
relative paths against the process working directory and the model gets zero
placement guidance, so every ad-hoc artifact lands in the repo root.

The map: code and plans stay in the repo (visible, versioned); bulk output
goes to D:\\jarvis (C: is for the system, D: has the free space).

    plans    -> docs/plans/            (upgrade plans, guides, reports)
    research -> data/research/         (research dumps, deep-dives)
    logs     -> logs/                  (transcripts, run logs, audits)
    media    -> D:/jarvis/media/       (audio, images, video — bulk)
    scripts  -> workspace/scripts/     (agent-written helper scripts)
    default  -> workspace/             (everything else ad-hoc)

Two layers (approved plan):
  guidance — route()/resolve() + tool-description hints steer the model.
  guardrail — check_write_path() refuses only the obviously-never-correct:
    `..` escaping above the repo root, and Windows system dirs. Explicit
    absolute paths elsewhere (D:, home, other projects) still work.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

try:
    from core.config import ROOT as _REPO_ROOT
except Exception:
    _REPO_ROOT = Path(__file__).resolve().parent.parent

REPO_ROOT = Path(_REPO_ROOT)

# Bulk output lives on D: (space), not C: (system). Overridable per install.
BULK_ROOT = Path("D:/jarvis")

MEDIA_EXTS = frozenset(
    {
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".m4a",
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
    }
)
LOG_EXTS = frozenset({".log"})

KIND_DIRS = {
    "plan": "docs/plans",
    "research": "data/research",
    "log": "logs",
    "media": "media",  # joined to BULK_ROOT, not REPO_ROOT
    "script": "workspace/scripts",
}

BULK_KINDS = frozenset({"media"})

# Never write here, no matter who asks via a tool path.
SYSTEM_DIRS = (
    PureWindowsPath("C:/Windows"),
    PureWindowsPath("C:/Program Files"),
    PureWindowsPath("C:/Program Files (x86)"),
    PureWindowsPath("C:/ProgramData"),
)


def kind_dir(kind: str, bulk_root: str | Path = BULK_ROOT) -> Path:
    """Absolute directory for a content kind. Never raises."""
    try:
        key = (kind or "default").lower()
        if key in BULK_KINDS:
            return Path(bulk_root) / KIND_DIRS[key]
        return REPO_ROOT / KIND_DIRS.get(key, "workspace")
    except Exception:
        return REPO_ROOT / "workspace"


def route(filename: str, kind: str = "auto", bulk_root: str | Path = BULK_ROOT) -> Path:
    """Route a bare filename to its mapped home. Never raises.

    kind="auto" sniffs the extension (media/log); anything else lands in
    workspace/. Pass an explicit kind (plan/research/script/...) for reports.
    """
    try:
        name = str(filename or "").strip().lstrip("/\\") or "untitled.txt"
        key = (kind or "auto").lower()
        if key == "auto":
            ext = Path(name).suffix.lower()
            if ext in MEDIA_EXTS:
                key = "media"
            elif ext in LOG_EXTS:
                key = "log"
            else:
                return REPO_ROOT / "workspace" / Path(name).name
        return kind_dir(key, bulk_root) / Path(name).name
    except Exception:
        return REPO_ROOT / "workspace" / "untitled.txt"


def resolve(path: str | Path, kind: str = "auto", bulk_root: str | Path = BULK_ROOT) -> Path:
    """Resolve a model-given path the way tools should.

    - Absolute paths pass through (D:, home, other projects keep working).
    - Paths with directories stay repo-relative (source edits unaffected).
    - Bare filenames route via route() (the litter fix).
    Never raises.
    """
    try:
        raw = str(path or "").strip()
        if not raw:
            return route("untitled.txt", kind, bulk_root)
        p = Path(raw)
        if p.is_absolute():
            return p
        if len(p.parts) > 1:
            return REPO_ROOT / p
        return route(p.name, kind, bulk_root)
    except Exception:
        return REPO_ROOT / "workspace" / "untitled.txt"


def check_write_path(path: str | Path) -> tuple[bool, str]:
    """Guardrail: (allowed, reason). Allows almost everything; refuses only:

    - relative paths escaping above the repo root via `..`
    - anything inside Windows system dirs
    Never raises; absolute paths elsewhere (D:, home) are allowed.
    """
    try:
        raw = str(path or "").strip()
        if not raw:
            return False, "empty path"
        p = Path(raw)
        resolved = (REPO_ROOT / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            resolved.relative_to(REPO_ROOT.resolve())
            inside_repo = True
        except Exception:
            inside_repo = False
        if not p.is_absolute() and not inside_repo:
            return False, f"escapes the workspace root: {raw}"
        win = PureWindowsPath(str(resolved))
        for sysdir in SYSTEM_DIRS:
            try:
                if win == sysdir or sysdir in win.parents:
                    return False, f"system directory is off-limits: {raw}"
            except Exception:
                continue
        if len(resolved.parts) <= 1:
            return False, f"drive root is off-limits: {raw}"
        return True, "ok"
    except Exception as exc:
        return False, f"unresolvable path: {type(exc).__name__}"


PLACEMENT_HINT = (
    "File placement: bare filenames are routed automatically — plans/reports to docs/plans/, "
    "research to data/research/, logs to logs/, audio/images/video to D:/jarvis/media/, helper "
    "scripts to workspace/scripts/, anything else to workspace/. Never drop files in the repo "
    "root. Repo source edits use their real repo-relative paths as always."
)

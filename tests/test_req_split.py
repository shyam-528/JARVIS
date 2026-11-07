"""P2 req-split gate: core-path modules must not hard-require voice/ML packages.

The tier split (requirements.core/voice/ml.txt) only holds if core, api,
non-voice/non-ml skills, utils, and entry points import heavy packages
lazily (function-level try/except). This test parses top-level imports via
AST and fails on any hard voice/ML dependency — so a future top-level
`import sklearn` breaks the gate instead of breaking core-only installs.

numpy is the documented exception: it stays in core (knowledge.py needs it
at import time; see requirements.core.txt header).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Top-level module names that live ONLY in voice/ml tiers.
VOICE_ML_ONLY = {
    "sounddevice",
    "faster_whisper",
    "edge_tts",
    "kokoro_onnx",
    "miniaudio",
    "openwakeword",
    "webrtcvad",
    "fastembed",
    "sentence_transformers",
    "sklearn",
}

# Modules allowed to hard-require voice/ML tiers.
EXEMPT = {
    "voice/stt.py",
    "voice/tts.py",
    "voice/mic.py",
    "voice/audio.py",
    "voice/wake.py",
    "voice/expressive.py",
    "voice/session.py",
    "core/ml_proactive.py",
    "core/knowledge.py",
    "core/memory.py",
    "core/rag_v2.py",
    "skills/ml_proactive_skills.py",
    "skills/voice_doctor.py",
    "api/routes/voice.py",
}

# Directories whose top-level imports must be tier-clean (minus EXEMPT files).
SCOPES = ("core", "api", "skills", "utils")


def _top_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            out.add(str(node.module).split(".")[0])
    return out


def _all_py():
    for scope in SCOPES:
        for path in (ROOT / scope).rglob("*.py"):
            if "__pycache__" in path.parts:
                yield path
    for name in ("jarvis.py", "hud.py", "hud_orb.py", "run_telegram_bot.py"):
        p = ROOT / name
        if p.exists():
            yield p


def test_no_hard_voice_ml_imports():
    offenders = []
    for path in _all_py():
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXEMPT:
            continue
        bad = _top_imports(path) & VOICE_ML_ONLY
        if bad:
            offenders.append(f"{rel}: {sorted(bad)}")
    assert not offenders, (
        "hard voice/ML imports outside EXEMPT files (import lazily instead):\n"
        + "\n".join(offenders)
    )


def test_exempt_list_is_current():
    """EXEMPT must not rot: every listed file must still exist.

    EXEMPT means "a hard voice/ML import here is acceptable" (voice domain,
    ML engines, and their skill/route surfaces) — not "currently has one".
    Lazy-first still applies to these files; the gate just doesn't fail them.
    """
    for rel in EXEMPT:
        assert (ROOT / rel).exists(), f"EXEMPT entry vanished: {rel}"


def test_tier_union_matches_full_requirements():
    """core+voice+ml package sets must equal requirements.txt (minus pytest)."""
    import re

    def pkgs(p, seen=None):
        seen = seen or set()
        out = set()
        for line in (ROOT / p).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r"):
                ref = line.split(maxsplit=1)[1]
                if ref not in seen:
                    seen.add(ref)
                    out |= pkgs(ref, seen)
                continue
            out.add(re.split(r"[>=<;\s]", line, maxsplit=1)[0].lower())
        return out

    union = (
        pkgs("requirements.core.txt") | pkgs("requirements.voice.txt") | pkgs("requirements.ml.txt")
    )
    full = pkgs("requirements.txt") - {"pytest", "pytest-asyncio"}
    assert union == full, f"split drift: missing={full - union} extra={union - full}"

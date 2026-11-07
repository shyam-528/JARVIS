from __future__ import annotations

import re
from dataclasses import dataclass

# Heuristics: fast, deterministic, zero API calls. Keep foreground chat snappy
# and only send genuinely heavy work to background agents.

HEAVY_VERBS = [
    "write",
    "build",
    "create",
    "make",
    "implement",
    "refactor",
    "fix",
    "debug",
    "develop",
    "generate",
    "design",
    "analyze",
    "research",
    "investigate",
    "optimize",
    "review",
    "migrate",
    "convert",
    "draft",
    "prepare",
    "compose",
    "set up",
    "setup",
]

CODING_HINTS = [
    "code",
    "script",
    "function",
    "class",
    "module",
    "api",
    "endpoint",
    "app",
    "program",
    "bug",
    "error",
    "crash",
    "test",
    "unit test",
    "repo",
    "repository",
    "python",
    "javascript",
    "typescript",
    "java",
    "rust",
    "golang",
    "c++",
    "html",
    "css",
    "react",
    "vue",
    "node",
    "sql",
    "database",
    "regex",
    "algorithm",
    "library",
    "framework",
    "compile",
    "deploy",
    "docker",
    "pipeline",
    "cli",
    "server",
    "backend",
    "frontend",
    "fullstack",
    "project",
    "file",
    "directory",
    "folder",
]

PROJECT_HINTS = [
    "project",
    "plan",
    "roadmap",
    "architecture",
    "documentation",
    "readme",
    "report",
    "summary of",
    "comparison",
    "compare",
    "study",
    "spec",
    "proposal",
    "presentation",
    "slides",
    "spreadsheet",
    "invoice",
    "budget",
    "proposal",
]

RESEARCH_HINTS = [
    "research",
    "find out",
    "look up",
    "search the web",
    "latest",
    "news about",
    "best practices",
    "state of",
    "survey",
    "market",
    "competitor",
]

QUICK_PATTERNS = [
    r"^(hi|hello|hey|yo|sup)\b",
    r"^(thanks|thank you|ty|ok|okay|cool|nice|great|good)\b",
    r"^(yes|no|yeah|nope|sure|fine)\b",
    r"\b(what is|what's|who is|where is|when is|how do i|how does|can you|do you|"
    r"are you|will you|could you|should i|is it)\b.*\?$",  # short question ending in ?
    r"^(remind me|remember|recall|forget)\b",
    r"^\s*$",
]

MIN_HEAVY_WORDS = 3  # requests shorter than this never background


@dataclass
class BGDecision:
    background: bool
    agent: str
    reason: str


def _matches_any(text: str, phrases: list[str]) -> bool:
    """Whole-phrase match with word boundaries (defect #2).

    The old `p in text` substring test fired on "taste"→test, "whatsapp"→app,
    "makeup"→make. Boundaries keep hints meaning what they say.
    """
    return _boundary_re(phrases).search(text) is not None


def _boundary_re(phrases: list[str]):
    pattern = "|".join(sorted((re.escape(p) for p in phrases), key=len, reverse=True))
    return re.compile(rf"\b(?:{pattern})\b")


def classify_background(user_input: str, enabled: bool = True) -> BGDecision:
    """Decide whether a user message should run in a background agent.

    Instant (no API calls): heuristics only. Quick chats and questions stay
    in the foreground; heavy work (coding/project/research verbs) goes to agents.
    """
    if not enabled:
        return BGDecision(False, "general", "bgmode off")

    text = user_input.strip()
    low = " ".join(text.lower().split())
    words = len(low.split())

    # 1) Never background: greetings, acks, short confirmations, memory ops, tiny inputs
    for pat in QUICK_PATTERNS:
        if re.match(pat, low):
            return BGDecision(False, "general", "quick/chat")
    if words < MIN_HEAVY_WORDS:
        return BGDecision(False, "general", "too short")

    # 2) Explicit background markers: user took control
    if re.search(r"\b(in background|background it|async|don'?t wait)\b", low):
        verb = next((v for v in HEAVY_VERBS if _boundary_re([v]).search(low)), "")
        return BGDecision(True, _pick_agent(low, verb), "explicit bg marker")

    # 3) Heavy work detection. A verb ALONE is never enough (defect #2:
    # "fix my phone" / "make dinner" / "write it down" are chats, not jobs) —
    # it must pair with a domain hint. Threshold 3 = verb(2) + hint(1).
    has_verb = _matches_any(low, HEAVY_VERBS)
    coding = _matches_any(low, CODING_HINTS)
    project = _matches_any(low, PROJECT_HINTS)
    research = _matches_any(low, RESEARCH_HINTS)
    score = (2 if has_verb else 0) + coding + project + research
    if score >= 3 and words >= MIN_HEAVY_WORDS:
        agent = _pick_agent(
            low,
            ""
            if not has_verb
            else next((v for v in HEAVY_VERBS if _boundary_re([v]).search(low)), ""),
        )
        return BGDecision(True, agent, f"heavy work (score {score})")

    # 4) Everything else: foreground chat
    return BGDecision(False, "general", "chat/question")


def _pick_agent(low: str, verb: str) -> str:
    if _matches_any(low, RESEARCH_HINTS) and not _matches_any(low, CODING_HINTS):
        return "researcher"
    if _matches_any(low, CODING_HINTS):
        return "coder"
    if _matches_any(low, PROJECT_HINTS):
        return "analyst"
    if verb in ("write", "draft", "compose"):
        return "writer"
    return "general"

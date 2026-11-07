"""Per-question answer sizing (defect #2 companion).

Simple questions get short answers, complex ones get thorough ones — the
model previously got one static "keep answers short" line for everything.
score_question() is instant heuristics (no API calls), mirroring the style
of core/bg_router.py:

  explicit override  >  learned default  >  heuristic

- Explicit: "briefly", "in detail", "tldr", "walk me through", ... always win.
- Learned: communication preferences containing brief/short/concise (or
  verbose/detailed/long) shift a "normal" verdict to short/detailed. Trivial
  short and genuinely complex verdicts are NOT overridden — a standing
  preference for brevity must not starve a complex question.
- Heuristic: word count + question marks + complexity hints (code, project,
  research, multi-part markers).

Levels: "short" (1-3 sentences), "normal" (no instruction — today's
behavior, byte-identical), "detailed" (structured, thorough).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SHORT_MARKS = [
    "briefly",
    "brief",
    "short",
    "tldr",
    "tl;dr",
    "just the answer",
    "one line",
    "one-liner",
    "in a sentence",
    "in one sentence",
    "yes or no",
    "quick answer",
    "keep it short",
]

DETAILED_MARKS = [
    "in detail",
    "detailed",
    "elaborate",
    "thorough",
    "thoroughly",
    "in-depth",
    "in depth",
    "step by step",
    "explain like",
    "full guide",
    "comprehensive",
    "break it down",
    "break down",
    "walk me through",
    "explain in detail",
    "go deep",
]

COMPLEX_HINTS = [
    "code",
    "script",
    "function",
    "project",
    "architecture",
    "design",
    "research",
    "compare",
    "comparison",
    "tradeoff",
    "tradeoffs",
    "trade-off",
    "pros and cons",
    "recommendation",
    "roadmap",
    "plan",
    "strategy",
    "explain",
    "why does",
    "how does",
    "debug",
    "refactor",
    "algorithm",
    "database",
    "deploy",
    "production",
]

SHORT_INSTRUCTION = (
    "Answer in 1-3 sentences. No preamble, no headings, no bullet lists "
    "unless asked. Just the answer, sir."
)

DETAILED_INSTRUCTION = (
    "Give a structured, thorough answer with headings and examples where "
    "they help. Cover the important angles; don't pad."
)


@dataclass
class Verbosity:
    level: str  # "short" | "normal" | "detailed"
    reason: str
    explicit: bool = False

    @property
    def instruction(self) -> str:
        if self.level == "short":
            return SHORT_INSTRUCTION
        if self.level == "detailed":
            return DETAILED_INSTRUCTION
        return ""


def _has(text: str, phrases: list[str]) -> bool:
    pattern = "|".join(sorted((re.escape(p) for p in phrases), key=len, reverse=True))
    return re.compile(rf"\b(?:{pattern})\b").search(text) is not None


def _learned_default(preferences) -> str | None:
    """Standing verbosity from learned communication prefs (or None)."""
    try:
        prefs = preferences.get_for_category("communication")
    except Exception:
        return None
    short = detailed = 0
    for p in prefs or []:
        try:
            blob = f"{getattr(p, 'key', '')} {getattr(p, 'value', '')}".lower()
        except Exception:
            continue
        if any(w in blob for w in ("brief", "short", "concise", "terse")):
            short += 1
        if any(w in blob for w in ("verbose", "detailed", "long", "thorough", "elaborate")):
            detailed += 1
    if short and short >= detailed:
        return "short"
    if detailed and detailed > short:
        return "detailed"
    return None


def score_question(text: str | None, preferences=None) -> Verbosity:
    """Size one question. Never raises; falls back to normal."""
    try:
        low = " ".join(str(text or "").lower().split())
        if not low:
            return Verbosity("short", "empty")
        if _has(low, SHORT_MARKS):
            return Verbosity("short", "explicit marker", explicit=True)
        if _has(low, DETAILED_MARKS):
            return Verbosity("detailed", "explicit marker", explicit=True)

        words = len(low.split())
        questions = low.count("?")
        hints = sum(1 for h in COMPLEX_HINTS if re.search(rf"\b{re.escape(h)}\b", low))

        heuristic = "normal"
        if words <= 12 and questions <= 1 and hints == 0:
            heuristic = "short"
        elif (
            words >= 40
            or questions >= 2
            or hints >= 3
            or (words >= 15 and hints >= 2)
            or (words >= 20 and hints >= 1)
        ):
            heuristic = "detailed"

        if heuristic == "normal" and preferences is not None:
            learned = _learned_default(preferences)
            if learned == "short":
                return Verbosity("short", "learned default")
            if learned == "detailed":
                return Verbosity("detailed", "learned default")
        return Verbosity(heuristic, "heuristic")
    except Exception:
        return Verbosity("normal", "fallback")

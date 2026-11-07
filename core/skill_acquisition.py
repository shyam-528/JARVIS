from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic


@dataclass
class ToolSequence:
    """A candidate automation: a tool sequence the user performs repeatedly."""

    tools: list[str]
    count: int
    first_seen: float
    last_seen: float
    proposed: bool = False


class SkillAcquisition:
    """Learns repeatable behaviors: watches tool-usage sequences and proposes
    workflows when the same 2-4 tool pattern repeats 3+ times.

    The brain then drafts the automation via the NL flow composer and saves it
    DISABLED — the user explicitly enables what they like. Zero risk, pure win.
    """

    MIN_PATTERN_LEN = 2
    MAX_PATTERN_LEN = 4
    PROPOSE_THRESHOLD = 3
    SESSION_GAP_S = 15 * 60  # sequences longer than this gap start fresh

    def __init__(self, state_path: Path):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = state_path
        self._current_seq: list[str] = []
        self._last_tool_ts: float = 0.0
        self.patterns: Counter = Counter()
        self.pattern_times: dict[tuple, list[float]] = {}
        self._proposed: set[tuple] = set()
        self._load()

    # ---------- persistence ----------

    def _load(self):
        # P0: shared corrupt-file contract — learned patterns quarantine
        # aside (loud) instead of silently resetting to empty.
        data = load_json_store(self.state_path, default={}, owner="skill_acquisition", expect=dict)
        try:
            self.patterns = Counter({tuple(k): v for k, v in data.get("patterns", {}).items()})
            self.pattern_times = {tuple(k): v for k, v in data.get("times", {}).items()}
            self._proposed = {tuple(k) for k in data.get("proposed", [])}
        except Exception:
            pass

    def _save(self):
        save_json_atomic(
            self.state_path,
            {
                "patterns": {list(k): v for k, v in self.patterns.items()},
                "times": {list(k): v for k, v in self.pattern_times.items()},
                "proposed": [list(k) for k in self._proposed],
            },
        )

    # ---------- observation ----------

    def record_tool_use(self, tool_name: str):
        """Feed one tool invocation; updates sequence patterns."""
        now = time.time()
        if now - self._last_tool_ts > self.SESSION_GAP_S:
            self._current_seq = []
        self._last_tool_ts = now
        self._current_seq.append(tool_name)
        if len(self._current_seq) > self.MAX_PATTERN_LEN:
            self._current_seq = self._current_seq[-self.MAX_PATTERN_LEN :]

        # record all contiguous sub-sequences of length 2..MAX
        for size in range(self.MIN_PATTERN_LEN, self.MAX_PATTERN_LEN + 1):
            if len(self._current_seq) >= size:
                pattern = tuple(self._current_seq[-size:])
                self.patterns[pattern] += 1
                self.pattern_times.setdefault(pattern, []).append(now)

    # ---------- proposals ----------

    def find_candidates(self, max_age_days: float = 14.0) -> list[ToolSequence]:
        """Patterns that hit the proposal threshold and haven't been proposed yet."""
        cutoff = time.time() - max_age_days * 86400
        out = []
        for pattern, count in self.patterns.items():
            if count < self.PROPOSE_THRESHOLD or pattern in self._proposed:
                continue
            times = [t for t in self.pattern_times.get(pattern, []) if t >= cutoff]
            if len(times) < self.PROPOSE_THRESHOLD:
                continue
            out.append(
                ToolSequence(
                    tools=list(pattern),
                    count=count,
                    first_seen=times[0],
                    last_seen=times[-1],
                )
            )
        out.sort(key=lambda s: s.count, reverse=True)
        return out

    def mark_proposed(self, pattern: tuple):
        self._proposed.add(tuple(pattern))
        self._save()

    def describe(self, seq: ToolSequence) -> str:
        """Human-readable proposal text the brain can present to the user."""
        return (
            f"You've used {', '.join(seq.tools)} together {seq.count} times recently. "
            f"Want me to automate this into a workflow?"
        )

    def close(self):
        self._save()

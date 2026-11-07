"""Heuristic proactive engine: habit + forgotten-follow-up suggestions.

Contract: suggestions.json is a state file — a corrupt load quarantines the
bad file aside with a timestamp (never silently emptied, never overwriting a
previous quarantine) and is LOUD on the console; a failed save is loud and
swallowed so a disk hiccup can't crash the calling turn.
Detection is pure heuristics over orchestrator history (no LLM calls).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from utils.json_utils import load_json_store, save_json_atomic

from .config import Config
from .memory import MemoryStore

log = logging.getLogger(__name__)


@dataclass
class Suggestion:
    id: str
    type: str  # "habit", "reminder", "automation", "insight"
    title: str
    description: str
    confidence: float  # 0-1
    action: str | None = None  # tool to call
    action_args: dict | None = None
    created_at: str = ""
    dismissed: bool = False


class ProactiveEngine:
    def __init__(self, config: Config, memory: MemoryStore):
        self.config = config
        self.memory = memory
        self.suggestions_file = config.db_path.parent / "suggestions.json"
        self._suggestions: list[Suggestion] = []
        self._load()

    def _load(self):
        # P0: shared corrupt-file contract (timestamped quarantine + console-loud).
        # Malformed entries are skipped individually so one bad suggestion
        # can't sink the rest.
        data = load_json_store(self.suggestions_file, default=[], owner="proactive", expect=list)
        kept = []
        for s in data:
            try:
                kept.append(Suggestion(**s))
            except Exception as exc:
                log.warning("skipping malformed suggestion entry (%s)", exc)
        if len(kept) != len(data):
            log.warning("skipped %d malformed suggestion entries", len(data) - len(kept))
        self._suggestions = kept

    def _save(self):
        """Persist suggestions; a failed write is logged, never raised."""
        if not save_json_atomic(self.suggestions_file, [s.__dict__ for s in self._suggestions]):
            log.warning("suggestions save failed")

    def analyze_and_suggest(
        self, user_input: str, orchestrator_history: list[dict]
    ) -> list[Suggestion]:
        new_suggestions = []
        new_suggestions.extend(self._detect_habits(orchestrator_history))
        new_suggestions.extend(self._detect_forgotten_followups(orchestrator_history))
        for s in new_suggestions:
            dupe = next(
                (
                    existing
                    for existing in self._suggestions
                    if existing.id == s.id and not existing.dismissed
                ),
                None,
            )
            if dupe is None:
                self._suggestions.append(s)
            elif s.id == "followup_pending":
                # Still pending on a later turn: escalate to auto-actable.
                # (Acting dismisses it, so this fires at most once per issue.)
                s.confidence = 0.9
        self._save()
        return new_suggestions

    def _detect_habits(self, history: list[dict]) -> list[Suggestion]:
        suggestions = []
        tool_calls = []
        for msg in history:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_calls.append(tc["function"]["name"])
        if not tool_calls:
            return suggestions
        counts = Counter(tool_calls)
        most_common = counts.most_common(3)
        for tool_name, count in most_common:
            if count >= 3:
                suggestions.append(
                    Suggestion(
                        id=f"habit_{tool_name}",
                        type="habit",
                        title=f"You use {tool_name} frequently",
                        description=f"You've used {tool_name} {count} times recently. Want me to create a shortcut or scheduled task?",
                        confidence=min(0.9, count / 10),
                        action="remember",
                        action_args={
                            "text": f"User frequently uses {tool_name} ({count}x)",
                            "kind": "habit",
                        },
                    )
                )
        return suggestions

    def _detect_forgotten_followups(self, history: list[dict]) -> list[Suggestion]:
        suggestions = []
        pending = []
        for i, msg in enumerate(history):
            if (
                msg.get("role") == "assistant"
                and msg.get("tool_calls")
                and (i + 1 >= len(history) or history[i + 1].get("role") != "tool")
            ):
                for tc in msg["tool_calls"]:
                    pending.append(tc["function"]["name"])
        if pending:
            names = sorted(set(pending))
            # Default follow-up: tomorrow 09:00 local. Escalation below may
            # raise this to an auto-acted reminder (see analyze_and_suggest).
            run_at = (datetime.now() + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            suggestions.append(
                Suggestion(
                    id="followup_pending",
                    type="reminder",
                    title="Pending tool results",
                    description=f"Some tools were called but results may not have been processed: {', '.join(names)}",
                    confidence=0.7,
                    action="schedule_task",
                    action_args={
                        "name": f"[auto] Follow up: {names[0]} result",
                        "trigger_type": "once",
                        "trigger_spec": {"run_at": run_at.isoformat()},
                        "action": "notify_reminder",
                        "action_args": {"message": f"Follow up on {names[0]} result"},
                    },
                )
            )
        return suggestions

    def get_active(self, limit: int = 5) -> list[Suggestion]:
        active = [s for s in self._suggestions if not s.dismissed]
        active.sort(key=lambda s: s.confidence, reverse=True)
        return active[:limit]

    def dismiss(self, suggestion_id: str) -> bool:
        for s in self._suggestions:
            if s.id == suggestion_id:
                s.dismissed = True
                self._save()
                return True
        return False

    def clear_dismissed(self):
        self._suggestions = [s for s in self._suggestions if not s.dismissed]
        self._save()

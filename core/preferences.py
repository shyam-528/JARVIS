"""Learned user preferences (``~/.jarvis_preferences.json`` by default).

Contract
--------
- Single JSON object on disk, keyed by a normalized storage key.
  ``set_value``/``get``/``remove`` all normalize the same way
  (``_storage_key``), so the Settings UI can delete exactly what it set.
- ``_save`` is atomic (tmp file + ``os.replace``): a crash mid-write can
  never leave a truncated store. Write failures are fail-soft — the last
  good file stays on disk.
- ``_load`` is fail-soft but NOT fail-invisible: an unparseable file is
  moved aside to ``<name>.corrupt-<ts>`` (nothing is deleted) and the
  manager starts empty; malformed entries are skipped individually.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UserPreference:
    """A single learned user preference."""

    key: str
    value: str
    category: str  # coding, communication, workflow, tool, format, etc.
    confidence: float
    source: str  # explicit, implicit, correction
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    usage_count: int = 0


class PreferenceManager:
    """Learns and manages user preferences from interactions."""

    # Patterns for detecting explicit preferences
    EXPLICIT_PATTERNS = [
        (r"I prefer (\w+(?:\s+\w+)*)", "explicit"),
        (r"I like (\w+(?:\s+\w+)*) better", "explicit"),
        (r"use (\w+(?:\s+\w+)*) instead", "explicit"),
        (r"always (\w+(?:\s+\w+)*)", "explicit"),
        (r"never (\w+(?:\s+\w+)*)", "explicit"),
        (r"my preference is (\w+(?:\s+\w+)*)", "explicit"),
    ]

    # Patterns for detecting corrections
    CORRECTION_PATTERNS = [
        (r"no,? (?:use|do|make) (\w+(?:\s+\w+)*)", "correction"),
        (r"not (\w+(?:\s+\w+)*)", "correction"),
        (r"instead of (\w+(?:\s+\w+)*),? (?:use|do) (\w+(?:\s+\w+)*)", "correction"),
        (r"wrong,? (?:use|do) (\w+(?:\s+\w+)*)", "correction"),
        (r"actually,? (?:use|do) (\w+(?:\s+\w+)*)", "correction"),
    ]

    # Category keywords
    CATEGORY_KEYWORDS = {
        "coding": [
            "indent",
            "tab",
            "space",
            "brace",
            "naming",
            "style",
            "lint",
            "format",
            "python",
            "javascript",
            "type",
        ],
        "communication": [
            "brief",
            "detailed",
            "verbose",
            "concise",
            "explain",
            "summary",
            "honorific",
            "sir",
        ],
        "workflow": ["automate", "schedule", "remind", "notify", "pipeline", "deploy", "test"],
        "tool": ["editor", "ide", "terminal", "shell", "browser", "search", "git"],
        "format": ["json", "yaml", "markdown", "table", "list", "bullet", "code block"],
    }

    def __init__(self, storage_path: Path = None):
        self.storage_path = storage_path or Path.home() / ".jarvis_preferences.json"
        self.preferences: dict[str, UserPreference] = {}
        self._load()

    def _load(self):
        """Load preferences from disk (see module docstring for the
        corrupt-file backup contract)."""
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("preferences root must be a JSON object")
        except Exception:
            # Move the unparseable file aside instead of silently starting
            # empty — nothing is deleted, the user can inspect/recover it.
            try:
                backup = self.storage_path.with_name(
                    f"{self.storage_path.name}.corrupt-{int(time.time())}"
                )
                self.storage_path.replace(backup)
            except Exception:
                pass
            return
        for key, pdata in data.items():
            try:
                self.preferences[key] = UserPreference(**pdata)
            except Exception:
                continue  # skip the malformed entry, keep the rest

    def _save(self):
        """Persist preferences atomically (tmp + os.replace) so a crash
        mid-write can never truncate the store. Fail-soft: a write error
        leaves the previous good file untouched."""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: v.__dict__ for k, v in self.preferences.items()}
            tmp = self.storage_path.with_name(self.storage_path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.storage_path)
        except Exception:
            pass

    def _categorize(self, text: str) -> str:
        """Categorize a preference based on keywords."""
        lower = text.lower()
        for category, keywords in self.CATEGORY_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                return category
        return "general"

    def extract_from_message(
        self, user_message: str, assistant_message: str = ""
    ) -> list[UserPreference]:
        """Extract preferences from a conversation turn."""
        found = []

        # Check explicit preferences
        for pattern, source in self.EXPLICIT_PATTERNS:
            matches = re.finditer(pattern, user_message, re.IGNORECASE)
            for match in matches:
                pref_text = match.group(1).strip()
                if len(pref_text) > 2:
                    key = self._normalize_key(pref_text)
                    pref = UserPreference(
                        key=key,
                        value=pref_text,
                        category=self._categorize(pref_text),
                        confidence=0.9,
                        source=source,
                    )
                    found.append(pref)

        # Check corrections (often in response to assistant)
        for pattern, source in self.CORRECTION_PATTERNS:
            matches = re.finditer(pattern, user_message, re.IGNORECASE)
            for match in matches:
                groups = match.groups()
                # "instead of X, use Y" pattern
                pref_text = groups[1].strip() if len(groups) == 2 else groups[0].strip()
                if len(pref_text) > 2:
                    key = self._normalize_key(pref_text)
                    pref = UserPreference(
                        key=key,
                        value=pref_text,
                        category=self._categorize(pref_text),
                        confidence=0.85,
                        source=source,
                    )
                    found.append(pref)

        return found

    def _normalize_key(self, text: str) -> str:
        """Normalize preference text to a key."""
        # Remove common words, lowercase, replace spaces
        stopwords = {"the", "a", "an", "my", "to", "for", "with", "using", "use"}
        words = [w for w in text.lower().split() if w not in stopwords]
        return "_".join(words[:5])  # Max 5 words

    def learn(self, preference: UserPreference) -> bool:
        """Add or update a preference."""
        existing = self.preferences.get(preference.key)
        if existing:
            # Update with higher confidence
            if preference.confidence > existing.confidence:
                existing.value = preference.value
                existing.confidence = preference.confidence
                existing.source = preference.source
            existing.usage_count += 1
            existing.updated_at = time.time()
        else:
            self.preferences[preference.key] = preference
        self._save()
        return True

    def learn_from_turn(self, user_message: str, assistant_message: str = "") -> int:
        """Process a conversation turn for preferences."""
        prefs = self.extract_from_message(user_message, assistant_message)
        for pref in prefs:
            self.learn(pref)
        return len(prefs)

    @staticmethod
    def _storage_key(key: str) -> str:
        """Canonical storage key — set_value/get/remove all normalize the
        same way, so e.g. API 'delete Theme' matches 'set Theme'."""
        return " ".join(str(key or "").split()).lower().replace(" ", "_")[:80]

    def get(self, key: str, default: str = None) -> str | None:
        """Get a preference value."""
        pref = self.preferences.get(self._storage_key(key))
        return pref.value if pref else default

    def get_for_category(self, category: str) -> list[UserPreference]:
        """Get all preferences for a category."""
        return [p for p in self.preferences.values() if p.category == category]

    def get_relevant_for_prompt(self, task_type: str, max_prefs: int = 5) -> list[str]:
        """Get preferences relevant to a task type, formatted for prompt injection."""
        # Map task types to categories
        category_map = {
            "coding": ["coding", "format", "tool"],
            "reasoning": ["workflow"],
            "analysis": ["workflow", "format"],
            "creative": ["communication", "format"],
            "planning": ["workflow", "communication"],
            "explanation": ["communication"],
            "research": ["workflow", "format"],
            "fast": ["communication"],
        }
        relevant_categories = category_map.get(task_type, ["general"])

        relevant = []
        for pref in self.preferences.values():
            if pref.category in relevant_categories and pref.confidence > 0.7:
                relevant.append(pref)

        # Sort by confidence and usage
        relevant.sort(key=lambda p: (p.confidence, p.usage_count), reverse=True)

        return [f"- {p.key}: {p.value}" for p in relevant[:max_prefs]]

    def format_for_prompt(self, task_type: str = "general") -> str:
        """Format preferences as a system prompt addition."""
        prefs = self.get_relevant_for_prompt(task_type)
        if not prefs:
            return ""
        return "User Preferences:\n" + "\n".join(prefs)

    def list_all(self) -> dict[str, UserPreference]:
        return dict(self.preferences)

    def set_value(
        self,
        key: str,
        value: str,
        category: str = "general",
        confidence: float = 1.0,
        source: str = "explicit",
    ) -> bool:
        """Create or overwrite a preference (Settings UI / API / chat)."""
        key = self._storage_key(key)
        value = " ".join(str(value or "").split())[:500]
        if not key or not value:
            return False
        now = time.time()
        existing = self.preferences.get(key)
        if existing:
            existing.value = value
            existing.category = category or existing.category
            existing.confidence = float(confidence)
            existing.source = source
            existing.usage_count += 1
            existing.updated_at = now
        else:
            self.preferences[key] = UserPreference(
                key=key,
                value=value,
                category=category or "general",
                confidence=float(confidence),
                source=source,
                created_at=now,
                updated_at=now,
            )
        self._save()
        return True

    def remove(self, key: str) -> bool:
        key = self._storage_key(key)
        if key in self.preferences:
            del self.preferences[key]
            self._save()
            return True
        return False

    def clear_category(self, category: str) -> int:
        keys = [k for k, v in self.preferences.items() if v.category == category]
        for k in keys:
            del self.preferences[k]
        self._save()
        return len(keys)

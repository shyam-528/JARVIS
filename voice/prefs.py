"""Voice taste memory: how JARVIS should speak to THIS user.

Small JSON store (data/voice_prefs.json) holding what the user taught the
speech layer through natural corrections ("don't call me sir", "too long",
"read everything"). Same corrupt-file contract as every other store
(utils/json_utils.load_json_store): a corrupt file is quarantined, never
silently emptied.

note_correction() only fires on short messages (<=10 words) so a real
question that happens to contain "too long" is never hijacked.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic

DEFAULTS = {"address": "sir", "brevity": "auto", "last_spoken": ""}

# brevity: "auto" (follow the question), "brief" (always headline),
# "full" (always speak the longer line).

_ADDRESS_NONE = [
    "don't call me sir",
    "dont call me sir",
    "stop calling me sir",
    "no more sir",
    "drop the sir",
    "don't say sir",
    "dont say sir",
    "without the sir",
    "without sir",
    "no sir",
]
_ADDRESS_NAME = re.compile(r"\bcall me ([a-z][a-z\-']{1,20})\b")

_BREVITY_BRIEF = [
    "too long",
    "keep it short",
    "shorter",
    "be brief",
    "briefly please",
    "less talking",
    "cut it short",
    "just the headline",
    "headlines only",
    "just headlines",
]
_BREVITY_FULL = [
    "read everything",
    "read it all",
    "read the whole",
    "read it fully",
    "speak fully",
    "tell me everything",
    "tell me all of it",
    "full detail",
    "more detail",
    "don't summarize",
    "dont summarize",
    "don't condense",
    "dont condense",
    "don't shorten",
    "dont shorten",
]
_BREVITY_AUTO = [
    "back to normal",
    "speak normally",
    "reset voice",
    "forget that",
    "as before",
]

_MAX_CORRECTION_WORDS = 10


def _has(text: str, phrases: list[str]) -> bool:
    pattern = "|".join(sorted((re.escape(p) for p in phrases), key=len, reverse=True))
    return re.compile(rf"\b(?:{pattern})\b").search(text) is not None


@dataclass
class VoicePrefs:
    """How the user likes spoken answers. Never raises on load/save."""

    path: Path = field(default_factory=lambda: Path("data/voice_prefs.json"))
    address: str = "sir"  # "sir" | "none" | a name
    brevity: str = "auto"  # "auto" | "brief" | "full"
    last_spoken: str = ""  # last distilled line, for the no-repeat guard

    @classmethod
    def load(cls, path: str | Path, address_seed: str = "sir") -> VoicePrefs:
        p = Path(path)
        if not p.exists():
            # Fresh file: the config seed wins (no corrections learned yet).
            return cls(path=p, address=address_seed or "sir", brevity="auto", last_spoken="")
        try:
            raw = load_json_store(p, default=dict(DEFAULTS), owner="voice-prefs", expect=dict)
        except Exception:
            raw = dict(DEFAULTS)
        try:
            address = str(raw.get("address", "") or "")
            if not address:
                address = address_seed or "sir"
            brevity = str(raw.get("brevity", "auto") or "auto")
            if brevity not in ("auto", "brief", "full"):
                brevity = "auto"
            last = str(raw.get("last_spoken", "") or "")
        except Exception:
            address, brevity, last = address_seed or "sir", "auto", ""
        return cls(path=p, address=address, brevity=brevity, last_spoken=last)

    def save(self) -> None:
        # Taste memory must never break speech.
        with contextlib.suppress(Exception):
            save_json_atomic(
                self.path,
                {"address": self.address, "brevity": self.brevity, "last_spoken": self.last_spoken},
            )

    def note_correction(self, text: str | None) -> str | None:
        """Apply a natural-language voice correction. Returns the spoken
        acknowledgement, or None when the message is not a correction."""
        try:
            low = " ".join(str(text or "").lower().split())
            if not low or len(low.split()) > _MAX_CORRECTION_WORDS:
                return None
            acks: list[str] = []

            if _has(low, _ADDRESS_NONE):
                if self.address != "none":
                    self.address = "none"
                    acks.append("Understood — no more \u2018sir\u2019.")
            else:
                name = _ADDRESS_NAME.search(low)
                if name:
                    called = name.group(1).strip("-' ")
                    if called == "sir":
                        if self.address != "sir":
                            self.address = "sir"
                            acks.append("Understood — \u2018sir\u2019 it is.")
                    else:
                        self.address = called
                        acks.append(f"Understood, {self.address}.")

            if _has(low, _BREVITY_BRIEF):
                if self.brevity != "brief":
                    self.brevity = "brief"
                    acks.append("Understood — headlines from here on.")
            elif _has(low, _BREVITY_FULL):
                if self.brevity != "full":
                    self.brevity = "full"
                    acks.append("Understood — full detail aloud from here on.")
            elif _has(low, _BREVITY_AUTO) and self.brevity != "auto":
                self.brevity = "auto"
                acks.append("Voice back to following the question.")

            if not acks:
                return None
            self.save()
            return " ".join(acks)
        except Exception:
            return None

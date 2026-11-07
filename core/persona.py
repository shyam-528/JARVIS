from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic


@dataclass
class PersonaProfile:
    name: str
    description: str
    system_prompt_suffix: str = ""
    honorific: str = "Sir"
    verbosity: str = "concise"  # concise, balanced, detailed
    traits: list[str] = field(default_factory=list)


DEFAULT_PERSONAS = {
    "professional": PersonaProfile(
        name="Professional",
        description="Concise, precise, uses 'Sir' occasionally. Technical depth on demand.",
        system_prompt_suffix=(
            "You are professional and efficient. Address the user as 'Sir' occasionally. "
            "Keep answers short unless asked for detail. Be precise and honest about uncertainty."
        ),
        honorific="Sir",
        verbosity="concise",
        traits=["precise", "efficient", "respectful"],
    ),
    "creative": PersonaProfile(
        name="Creative",
        description="Expressive, exploratory, suggests alternatives. Good for design/brainstorming.",
        system_prompt_suffix=(
            "You are creative and exploratory. Offer alternatives and suggestions freely. "
            "Use metaphors and analogies. Encourage divergent thinking. "
            "Address the user as 'Sir' occasionally."
        ),
        honorific="Sir",
        verbosity="detailed",
        traits=["expressive", "exploratory", "suggestive"],
    ),
    "technical": PersonaProfile(
        name="Technical",
        description="Deep detail, shows reasoning, includes tradeoffs. For coding/analysis.",
        system_prompt_suffix=(
            "You are technically rigorous. Show your reasoning step by step. "
            "Include tradeoffs, alternatives, and edge cases. "
            "Use precise terminology. Address the user as 'Sir' occasionally."
        ),
        honorific="Sir",
        verbosity="detailed",
        traits=["rigorous", "detailed", "analytical"],
    ),
    "casual": PersonaProfile(
        name="Casual",
        description="Friendly, brief, minimal honorifics. For quick chats.",
        system_prompt_suffix=(
            "You are friendly and conversational. Keep it brief and natural. "
            "Minimal honorifics. Match the user's tone."
        ),
        honorific="",
        verbosity="concise",
        traits=["friendly", "brief", "natural"],
    ),
}


class PersonaManager:
    """Manages persona profiles and active persona state."""

    def __init__(self, config_path: Path = None):
        self.config_path = config_path or Path.home() / ".jarvis_persona.json"
        self.personas: dict[str, PersonaProfile] = dict(DEFAULT_PERSONAS)
        self.active_persona: str = "professional"
        self._load()

    def _load(self):
        """Load custom personas and active selection from disk."""
        # P0: shared corrupt-file contract — a corrupt persona file
        # quarantines aside (loud) instead of silently dropping to defaults.
        data = load_json_store(self.config_path, default={}, owner="persona", expect=dict)
        try:
            # Load custom personas
            for name, pdata in data.get("personas", {}).items():
                self.personas[name] = PersonaProfile(**pdata)
            # Load active persona
            self.active_persona = data.get("active", "professional")
        except Exception:
            pass

    def _save(self):
        """Persist custom personas and active selection."""
        # Only save non-default personas
        custom = {
            name: p.__dict__ for name, p in self.personas.items() if name not in DEFAULT_PERSONAS
        }
        data = {
            "active": self.active_persona,
            "personas": custom,
        }
        save_json_atomic(self.config_path, data)

    def get_active(self) -> PersonaProfile:
        return self.personas.get(self.active_persona, DEFAULT_PERSONAS["professional"])

    def set_active(self, name: str) -> bool:
        if name in self.personas:
            self.active_persona = name
            self._save()
            return True
        return False

    def list_personas(self) -> dict[str, PersonaProfile]:
        return dict(self.personas)

    def add_persona(self, profile: PersonaProfile) -> bool:
        if profile.name in self.personas:
            return False
        self.personas[profile.name] = profile
        self._save()
        return True

    def remove_persona(self, name: str) -> bool:
        if name in DEFAULT_PERSONAS:
            return False  # Cannot remove defaults
        if name in self.personas:
            del self.personas[name]
            if self.active_persona == name:
                self.active_persona = "professional"
            self._save()
            return True
        return False

    def build_system_prompt(self, base_prompt: str) -> str:
        """Combine base prompt with active persona suffix."""
        persona = self.get_active()
        if persona.system_prompt_suffix:
            return f"{base_prompt}\n\n{persona.system_prompt_suffix}"
        return base_prompt

    def get_honorific(self) -> str:
        return self.get_active().honorific

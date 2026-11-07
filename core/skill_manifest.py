from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class SkillManifest:
    name: str
    version: str
    description: str
    author: str
    license: str = "MIT"
    entry_point: str = "register"
    dependencies: list[str] = field(default_factory=list)
    jarvis_min_version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    homepage: str = ""
    repository: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def from_file(cls, path: Path) -> SkillManifest:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def to_file(self, path: Path):
        path.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")

    def to_dict(self) -> dict:
        return self.__dict__.copy()


MANIFEST_FILENAME = "skill.json"


def validate_manifest(data: dict) -> tuple[bool, list[str]]:
    errors = []
    required = ["name", "version", "description", "author"]
    for req in required:
        if req not in data or not data[req]:
            errors.append(f"Missing required field: {req}")
    if "version" in data and not isinstance(data["version"], str):
        errors.append("Version must be a string (semver)")
    return len(errors) == 0, errors

"""Per-project memory: conventions, open threads, last standup per repo.

One JSON file in data_dir (project_memory.json) keyed by normalized repo
identity, so it works from any surface (CLI, Telegram, web) without a cwd.
Kinds: convention | thread | standup | note. Standup replaces (one live
standup per project); lists are capped so the file can't grow forever.

Failure policy (Wave 7 + P0): all methods never raise — they return
{"ok": bool, "error": str} / data. A corrupt memory file quarantines aside
(timestamped, never silently wiped) and is announced loudly, and a failed
save is reported as {"ok": False} — remember()/forget() never claim success
for data that didn't hit disk.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from utils.json_utils import load_json_store

KINDS = ("convention", "thread", "standup", "note")
_LIST_CAP = 50
_THREAD_CAP = 20


def _key(name_or_path: str) -> str:
    clean = " ".join(str(name_or_path or "").split()).strip()
    if not clean:
        return ""
    try:
        p = Path(clean).expanduser()
        if p.is_absolute() or (len(clean) > 2 and clean[1] == ":"):
            return str(p.resolve()).lower() if p.exists() else clean.lower()
    except Exception:
        pass  # fail-soft: odd path strings just fall back to plain lowercase key
    return clean.lower()


class ProjectMemory:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / "project_memory.json"

    # ---------- persistence ----------

    def _load(self) -> dict:
        # P0: shared corrupt-file contract — quarantine aside instead of
        # returning empty over a file that still sits there corrupt.
        data = load_json_store(self._path, default={}, owner="project_memory", expect=dict)
        projs = data.get("projects", {})
        return {k: v for k, v in projs.items() if isinstance(v, dict)}

    def _save(self, projs: dict) -> bool:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"projects": projs}, indent=2), encoding="utf-8")
            tmp.replace(self._path)
            return True
        except Exception as exc:
            print(f"[project_memory] failed to persist memory to {self._path}: {exc}")
            return False

    def _blank(self, name: str) -> dict:
        return {
            "name": name,
            "conventions": [],
            "threads": [],
            "notes": [],
            "last_standup": {},
            "updated": "",
        }

    # ---------- write ----------

    def remember(self, project: str, kind: str, text: str) -> dict:
        """Store one item. Returns {"ok": bool, "key"/"error"}."""
        key = _key(project)
        clean = " ".join(str(text or "").split())
        if not key:
            return {"ok": False, "error": "project name required"}
        if not clean:
            return {"ok": False, "error": "empty text"}
        kind = str(kind or "note").strip().lower()
        if kind not in KINDS:
            return {"ok": False, "error": f"kind must be one of {', '.join(KINDS)}"}
        projs = self._load()
        entry = projs.get(key) or self._blank(str(project).strip()[:80])
        now = datetime.now().isoformat(timespec="seconds")
        if kind == "standup":
            entry["last_standup"] = {"text": clean[:1000], "ts": now}
        else:
            bucket = {"convention": "conventions", "thread": "threads"}.get(kind, "notes")
            cap = _THREAD_CAP if bucket == "threads" else _LIST_CAP
            if clean not in entry[bucket]:
                entry[bucket].append(clean[:500])
                entry[bucket] = entry[bucket][-cap:]
        entry["updated"] = now
        projs[key] = entry
        if not self._save(projs):
            return {"ok": False, "error": "failed to persist project memory (see console)"}
        return {"ok": True, "key": key}

    def forget(self, project: str, text: str = "") -> dict:
        """Remove one matching item (or the whole project when text is empty)."""
        key = _key(project)
        projs = self._load()
        target = projs.get(key)
        if target is None:
            match = self.resolve(project)
            target = projs.get(match, None) if match else None
            key = match or key
        if target is None:
            return {"ok": False, "error": f"unknown project '{project}'"}
        clean = " ".join(str(text or "").split()).lower()
        if not clean:
            del projs[key]
            if not self._save(projs):
                return {"ok": False, "error": "failed to persist project memory (see console)"}
            return {"ok": True, "key": key}
        removed = 0
        for bucket in ("conventions", "threads", "notes"):
            before = len(target.get(bucket, []))
            target[bucket] = [t for t in target.get(bucket, []) if clean not in t.lower()]
            removed += before - len(target[bucket])
        if clean in str(target.get("last_standup", {}).get("text", "")).lower():
            target["last_standup"] = {}
            removed += 1
        if removed:
            if not self._save(projs):
                return {"ok": False, "error": "failed to persist project memory (see console)"}
            return {"ok": True, "key": key}
        return {"ok": False, "error": "no match"}

    # ---------- read ----------

    def resolve(self, project: str) -> str:
        """Best key match: exact, basename, or substring. '' when unknown."""
        key = _key(project)
        if not key:
            return ""
        projs = self._load()
        if key in projs:
            return key
        base = key.replace("\\", "/").rstrip("/").split("/")[-1]
        for k, v in projs.items():
            kb = k.replace("\\", "/").rstrip("/").split("/")[-1]
            if base and (kb == base or str(v.get("name", "")).lower() == key):
                return k
        hits = [k for k in projs if key in k]
        return hits[0] if len(hits) == 1 else ""

    def recall(self, project: str) -> str:
        """Formatted project card, or '' when unknown."""
        projs = self._load()
        key = self.resolve(project) or _key(project)
        entry = projs.get(key)
        if not entry:
            return ""
        lines = [f"📁 {entry.get('name', key)}"]
        if entry.get("conventions"):
            lines.append("Conventions:")
            lines += [f"  - {c}" for c in entry["conventions"][-10:]]
        if entry.get("threads"):
            lines.append("Open threads:")
            lines += [f"  - {t}" for t in entry["threads"][-10:]]
        standup = entry.get("last_standup", {}) or {}
        if standup.get("text"):
            lines.append(f"Last standup ({standup.get('ts', '?')}): {standup['text'][:400]}")
        if entry.get("notes"):
            lines.append("Notes:")
            lines += [f"  - {n}" for n in entry["notes"][-5:]]
        return "\n".join(lines)[:2000]

    def list(self) -> list[dict]:
        projs = self._load()
        out = [
            {
                "key": k,
                "name": v.get("name", k),
                "threads": len(v.get("threads", [])),
                "updated": v.get("updated", ""),
            }
            for k, v in projs.items()
        ]
        out.sort(key=lambda x: x["updated"], reverse=True)
        return out

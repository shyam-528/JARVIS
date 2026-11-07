from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field

from .config import Config
from .db import connect as _db_connect
from .orchestrator import Orchestrator


@dataclass
class AutomationRule:
    id: str
    name: str
    trigger: dict  # {"type": "time"|"command"|"memory"|"tool_result", "condition": ...}
    actions: list[dict]  # [{"tool": "name", "args": {...}}, ...]
    enabled: bool = True
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    run_count: int = 0


class AutomationEngine:
    def __init__(self, config: Config, orchestrator: Orchestrator):
        self.config = config
        self.orchestrator = orchestrator
        self.db_path = config.db_path.parent / "automation.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _db_connect(self.db_path)
        self._init_db()
        self._rules: dict[str, dict] = {}
        self._load_rules()

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                trigger TEXT NOT NULL,
                actions TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                run_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        self._conn.commit()

    def _load_rules(self):
        # Load ALL rules (incl. disabled) so list/enable can see them.
        # evaluate() filters by enabled.
        rows = self._conn.execute("SELECT * FROM rules").fetchall()
        for row in rows:
            self._rules[row[0]] = {
                "id": row[0],
                "name": row[1],
                "trigger": json.loads(row[2]),
                "actions": json.loads(row[3]),
                "enabled": bool(row[4]),
                "created_at": row[5],
                "run_count": row[6],
            }

    def _save_rule(self, rule_id: str, rule: dict):
        self._conn.execute(
            "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?)",
            (
                rule_id,
                rule["name"],
                json.dumps(rule["trigger"]),
                json.dumps(rule["actions"]),
                1 if rule.get("enabled", True) else 0,
                rule.get("created_at", ""),
                rule.get("run_count", 0),
            ),
        )
        self._conn.commit()

    def _delete_rule(self, rule_id: str):
        self._conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        self._conn.commit()

    def add_rule(self, name: str, trigger: dict, actions: list[dict]) -> str:
        # Validate command patterns at add time: a bad regex would otherwise
        # throw on every turn (fail-silent). Also de-dupe slug ids.
        if str(trigger.get("type", "")).lower() == "command" and trigger.get("pattern"):
            try:
                re.compile(trigger["pattern"])
            except re.error as exc:
                raise ValueError(f"invalid trigger pattern: {exc}") from exc
        slug = name.lower().replace(" ", "_")[:30] or str(uuid.uuid4())[:8]
        rule_id = slug
        suffix = 2
        existing = {r["id"] for r in self._rules.values()}
        rows = {r[0] for r in self._conn.execute("SELECT id FROM rules").fetchall()}
        while rule_id in existing or rule_id in rows:
            rule_id = f"{slug}-{suffix}"
            suffix += 1
        rule = {
            "id": rule_id,
            "name": name,
            "trigger": trigger,
            "actions": actions,
            "enabled": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_count": 0,
        }
        self._rules[rule["id"]] = rule
        self._conn.execute(
            "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?)",
            (
                rule["id"],
                rule["name"],
                json.dumps(rule["trigger"]),
                json.dumps(rule["actions"]),
                1,
                rule["created_at"],
                0,
            ),
        )
        self._conn.commit()
        return rule["id"]

    def remove_rule(self, rule_id: str) -> bool:
        if rule_id in self._rules:
            del self._rules[rule_id]
            self._conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            self._conn.commit()
            return True
        return False

    def enable_rule(self, rule_id: str, enabled: bool = True) -> bool:
        if rule_id in self._rules:
            self._rules[rule_id]["enabled"] = enabled
            self._conn.execute(
                "UPDATE rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id)
            )
            self._conn.commit()
            return True
        return False

    def list_rules(self) -> list[dict]:
        return list(self._rules.values())

    def evaluate(self, event_type: str, context: dict) -> list[dict]:
        """Check rules and return actions to execute. Returns list of action dicts."""
        executed = []
        for rule in self._rules.values():
            if not rule["enabled"]:
                continue
            if self._matches(rule["trigger"], event_type, context):
                for action in rule["actions"]:
                    executed.append({"rule_id": rule["id"], "action": action})
                    rule["run_count"] += 1
        if executed:
            self._persist_counts()
        return executed

    def _matches(self, trigger: dict, event_type: str, context: dict) -> bool:
        ttype = trigger.get("type")
        if ttype == "command" and event_type == "command":
            pattern = trigger.get("pattern", "")
            user_text = context.get("text", "").lower()
            return bool(re.search(pattern, user_text, re.IGNORECASE))
        if ttype == "time" and event_type == "time":
            # {"cron": "0 9 * * *"} or {"hour": 9, "minute": 0}
            return True  # scheduler handles timing
        if ttype == "tool_result" and event_type == "tool_result":
            tool_name = trigger.get("tool")
            return context.get("tool") == tool_name
        if ttype == "memory" and event_type == "memory":
            keyword = trigger.get("keyword", "")
            return keyword.lower() in context.get("text", "").lower()
        return False

    def _persist_counts(self):
        for rid, rule in self._rules.items():
            self._conn.execute(
                "UPDATE rules SET run_count = ? WHERE id = ?", (rule.get("run_count", 0), rid)
            )
        self._conn.commit()

    def list_rules_formatted(self) -> list[dict]:
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "trigger": r["trigger"],
                "actions": r["actions"],
                "enabled": r.get("enabled", True),
                "runs": r.get("run_count", 0),
            }
            for r in self._rules.values()
        ]

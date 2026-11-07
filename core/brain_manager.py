from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import connect as _db_connect


@dataclass
class BrainAssignment:
    """One agent's dedicated brain: primary provider + ordered fallbacks."""

    agent: str
    primary: str
    fallbacks: list[str] = field(default_factory=list)
    reason: str = ""  # why this brain was assigned (manual / auto:<note>)
    assigned_at: float = 0.0
    failures: int = 0  # consecutive failures since last success
    last_success: float = 0.0


class BrainManager:
    """Maintains a dedicated brain for every agent, managed by JARVIS.

    - Auto-assign: uses the RoutingOptimizer's learned leaderboard to pick the
      best provider for each agent's task type (falls back to smart defaults).
    - Health maintenance: tracks consecutive failures; after a threshold, the
      brain is re-evaluated and swapped (primary demoted to fallbacks).
    - Persistence: assignments live in SQLite and survive restarts.
    - Managed by JARVIS: a scheduled maintenance task + failure hooks keep
      every agent on its healthiest brain without user intervention.
    """

    SWAP_THRESHOLD = 3  # consecutive failures before re-evaluation
    MAINTENANCE_INTERVAL_S = 3600  # hourly health pass
    MIN_OPTIMIZER_SAMPLES = 3  # need this many learned samples to auto-assign

    # Smart defaults per agent task-type (used before the optimizer has data)
    DEFAULT_PREFERENCES = {
        "coder": [
            "xkiro-qwen-coder-plus",
            "xkiro-codestral",
            "xkiro-devstral",
            "nim-nemo-super",
            "groq-gpt-oss-120b",
        ],
        "researcher": ["xkiro-qwen38-max", "gemini-3.5-flash", "gemini-3.6-flash"],
        "analyst": ["xkiro-mistral-large", "xkiro-qwen37-max", "gemini-3.5-flash"],
        "writer": ["xkiro-qwen37-plus", "gemini-3.5-flash", "groq-gpt-oss-120b"],
        "fast": ["xkiro-qwen37-flash", "xkiro-ministral-8b", "groq-gpt-oss-20b"],
        "general": [],  # full failover chain
    }

    def __init__(self, orchestrator, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self.orch = orchestrator
        self._assignments: dict[str, BrainAssignment] = {}
        self._load()
        self.swap_log: list[str] = []

    # ---------- storage ----------

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS brain_assignments (
                agent TEXT PRIMARY KEY,
                primary_provider TEXT NOT NULL,
                fallbacks_json TEXT DEFAULT '[]',
                reason TEXT DEFAULT '',
                assigned_at REAL,
                failures INTEGER DEFAULT 0,
                last_success REAL DEFAULT 0
            );
        """)
        self.conn.commit()

    def _load(self):
        for row in self.conn.execute("SELECT * FROM brain_assignments"):
            self._assignments[row["agent"]] = BrainAssignment(
                agent=row["agent"],
                primary=row["primary_provider"],
                fallbacks=json.loads(row["fallbacks_json"] or "[]"),
                reason=row["reason"],
                assigned_at=row["assigned_at"] or 0.0,
                failures=row["failures"] or 0,
                last_success=row["last_success"] or 0.0,
            )

    def _persist(self, a: BrainAssignment):
        self.conn.execute(
            "INSERT OR REPLACE INTO brain_assignments VALUES (?,?,?,?,?,?,?)",
            (
                a.agent,
                a.primary,
                json.dumps(a.fallbacks),
                a.reason,
                a.assigned_at or time.time(),
                a.failures,
                a.last_success,
            ),
        )
        self.conn.commit()

    # ---------- queries ----------

    def get(self, agent: str) -> BrainAssignment | None:
        return self._assignments.get(agent)

    def providers_for(self, agent: str) -> list[str]:
        """Ordered provider list for an agent: [primary, *fallbacks]. Empty = full chain."""
        a = self._assignments.get(agent)
        if a is None:
            return []
        return [a.primary] + [f for f in a.fallbacks if f != a.primary]

    def all(self) -> list[BrainAssignment]:
        return list(self._assignments.values())

    # ---------- assignment ----------

    def assign(
        self, agent: str, primary: str, fallbacks: list[str] | None = None, reason: str = "manual"
    ) -> BrainAssignment:
        a = BrainAssignment(
            agent=agent,
            primary=primary,
            fallbacks=[f for f in (fallbacks or []) if f != primary],
            reason=reason,
            assigned_at=time.time(),
        )
        self._assignments[agent] = a
        self._persist(a)
        return a

    def auto_assign_all(self) -> dict[str, str]:
        """Assign every agent its learned-best brain (optimizer data if enough
        samples; otherwise smart defaults validated against live providers)."""
        known = {p.name for p in self.orch.cfg.providers}
        result = {}
        for agent_name, spec in self.orch.agent_pool.agents.items():
            task = getattr(spec, "task_type", "general")
            primary = None
            reason = "auto"

            # 1) learned leaderboard (only if it has real samples)
            try:
                lb = self.orch.routing_optimizer.leaderboard(task)
                entries = lb.get(task, [])
                for e in entries:
                    if e["provider"] in known and e["score"] >= 0.55:
                        primary = e["provider"]
                        reason = f"auto:learned({e['score']})"
                        break
            except Exception:
                pass

            # 2) smart defaults, validated against live provider list
            if primary is None:
                prefs = [p for p in self.DEFAULT_PREFERENCES.get(agent_name, []) if p in known]
                # multi-key variants count as their base
                if prefs:
                    primary = prefs[0]
                    reason = "auto:default"

            # 3) first available provider
            if primary is None:
                avail = self.orch.router.available()
                if avail:
                    primary = avail[0].name
                    reason = "auto:first-available"

            if primary:
                fallbacks = []
                try:
                    base = self.orch.router._canonical_name(primary)
                    # same-model key variants first (no quality drop)
                    for p in self.orch.cfg.providers:
                        n = p.name
                        if (
                            n != primary
                            and (
                                self.orch.router._canonical_name(n) == base
                                or n in self.DEFAULT_PREFERENCES.get(agent_name, [])
                            )
                            and n in known
                        ):
                            fallbacks.append(n)
                    fallbacks = fallbacks[:4]
                except Exception:
                    pass
                self.assign(agent_name, primary, fallbacks, reason=reason)
                result[agent_name] = primary
        return result

    # ---------- health maintenance ----------

    def record_success(self, agent: str, provider: str):
        a = self._assignments.get(agent)
        if a is None:
            return
        a.failures = 0
        a.last_success = time.time()
        self._persist(a)

    def record_failure(self, agent: str, provider: str) -> BrainAssignment | None:
        """Count a failure; if the primary crosses the threshold, rotate the brain."""
        a = self._assignments.get(agent)
        if a is None:
            return None
        a.failures += 1
        if a.failures >= self.SWAP_THRESHOLD and a.fallbacks:
            old = a.primary
            new = a.fallbacks[0]
            a.fallbacks = a.fallbacks[1:] + [old]  # demote old to the tail
            a.primary = new
            a.failures = 0
            a.reason = f"auto:swap({old} failed {self.SWAP_THRESHOLD}x)"
            a.assigned_at = time.time()
            self.swap_log.append(f"{agent}: {old} -> {new} ({a.reason})")
        self._persist(a)
        return a

    def maintenance_pass(self) -> dict:
        """Hourly: re-assign brains whose primaries look unhealthy, refresh from
        optimizer learning, and report. Managed by JARVIS's own scheduler."""
        report = {"checked": len(self._assignments), "swapped": 0, "refreshed": 0}
        known = {p.name for p in self.orch.cfg.providers}
        for agent_name, a in list(self._assignments.items()):
            # primary vanished from config? refresh
            if a.primary not in known:
                self.auto_assign_all()
                report["refreshed"] += 1
                continue
            # primary breaker open for a long time + fallbacks exist? rotate
            try:
                state = self.orch.router.breakers.describe(a.primary)
                kind, wait = state
                if (kind.startswith("COOLING") or (kind not in ("ok",) and kind != "")) and (
                    a.fallbacks
                ):
                    healthy_fb = next(
                        (
                            f
                            for f in a.fallbacks
                            if f in known and self.orch.router.breakers.get(f).allows()
                        ),
                        None,
                    )
                    if healthy_fb:
                        old = a.primary
                        a.fallbacks = [old] + [f for f in a.fallbacks if f != healthy_fb]
                        a.primary = healthy_fb
                        a.reason = f"auto:maintenance({old} cooling)"
                        a.assigned_at = time.time()
                        self._persist(a)
                        self.swap_log.append(f"{agent_name}: {old} -> {healthy_fb} (maintenance)")
                        report["swapped"] += 1
            except Exception:
                pass
        return report

    def close(self):
        self.conn.close()

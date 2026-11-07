from __future__ import annotations

import time
from pathlib import Path

from .db import connect as _db_connect

# How strongly each outcome moves a provider's score (0..1 scale).
REWARD_SUCCESS = 1.0
REWARD_FAST_BONUS = 0.5  # awarded proportionally for first-token under 3s
REWARD_HOP_COST = -0.6  # this provider HAD to be bailed out by a hop
DECAY = 0.90  # exponential decay per new sample: adapts to provider changes
FAST_MS = 3000
BASE_SCORE = 0.5


class RoutingOptimizer:
    """Learns the best provider per task type from real request outcomes.

    A per-(task_type, provider) score in [0, 1] is updated after every
    completed chat with an exponentially-decayed average (a 1-armed bandit
    with recency bias). The router blends this score into candidate ordering
    so the chain tunes itself: providers that actually work well for coding
    float up, overloaded ones sink — no hand-maintained tag lists.

    Scores persist to SQLite so learning survives restarts.
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(str(db_path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS routing_scores ("
            "task TEXT NOT NULL, provider TEXT NOT NULL,"
            "score REAL NOT NULL, samples INTEGER NOT NULL,"
            "updated_at REAL NOT NULL, PRIMARY KEY (task, provider))"
        )
        self.conn.commit()
        self._cache: dict[str, dict[str, float]] = {}
        self._load()
        self.last_events: list[str] = []

    # ---------- persistence ----------

    def _load(self):
        for task, provider, score, _samples, _ in self.conn.execute(
            "SELECT task, provider, score, samples, updated_at FROM routing_scores"
        ):
            self._cache.setdefault(task, {})[provider] = score

    def _save(self, task: str, provider: str, score: float, samples: int):
        self.conn.execute(
            "INSERT OR REPLACE INTO routing_scores VALUES (?,?,?,?,?)",
            (task, provider, score, samples, time.time()),
        )
        self.conn.commit()

    # ---------- learning ----------

    def reward(
        self,
        task_type: str,
        provider: str,
        first_token_ms: float,
        success: bool = True,
        hopped: bool = False,
    ) -> float:
        """Record one outcome; returns the updated score."""
        reward = 0.0
        if success:
            reward += REWARD_SUCCESS
            if first_token_ms and first_token_ms > 0:
                # bonus scaled: 0ms -> full bonus, >=FAST_MS -> none
                speed = max(0.0, 1.0 - first_token_ms / FAST_MS)
                reward += REWARD_FAST_BONUS * speed
        else:
            reward -= REWARD_SUCCESS
        if hopped:
            reward += REWARD_HOP_COST

        old = self._cache.get(task_type, {}).get(provider, BASE_SCORE)
        # exponentially-decayed running average
        new = old * DECAY + reward * (1.0 - DECAY)
        # clamp
        new = max(0.0, min(1.0, new))

        self._cache.setdefault(task_type, {})[provider] = new
        row = self.conn.execute(
            "SELECT samples FROM routing_scores WHERE task = ? AND provider = ?",
            (task_type, provider),
        ).fetchone()
        samples = (row[0] + 1) if row else 1
        self._save(task_type, provider, new, samples)
        self.last_events.append(f"{task_type}/{provider}: {old:.2f}->{new:.2f} (r={reward:+.2f})")
        return new

    # ---------- inference ----------

    def score(self, task_type: str, provider: str) -> float:
        """Optimist score for a provider on a task; defaults to 0.5 (neutral)."""
        return self._cache.get(task_type, {}).get(provider, BASE_SCORE)

    def best(self, task_type: str, candidates: list[str]) -> str | None:
        """Highest-scoring provider among candidates (None if empty)."""
        if not candidates:
            return None
        return max(candidates, key=lambda p: self.score(task_type, p))

    def boost_names(self, task_type: str, k: int = 3) -> list[str]:
        """Top-k providers for a task — can be fed to router.set_preferred_names."""
        ranked = sorted(
            self._cache.get(task_type, {}).items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return [name for name, score in ranked[:k] if score > BASE_SCORE]

    def leaderboard(self, task_type: str = "") -> dict[str, list]:
        out = {}
        tasks = [task_type] if task_type else sorted(self._cache.keys())
        for t in tasks:
            ranked = sorted(self._cache.get(t, {}).items(), key=lambda kv: kv[1], reverse=True)
            out[t] = [{"provider": p, "score": round(s, 3)} for p, s in ranked[:8]]
        return out

    def close(self):
        self.conn.close()

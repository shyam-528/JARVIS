"""Cross-device surface journal (roadmap step 3).

One conversation across terminal / web / Telegram: every completed turn is
appended to a shared SQLite journal, and each surface injects the last few
turns from OTHER surfaces into the brain's context — so "that draft I asked
for on Telegram" makes sense when you walk back to the PC.

Design: append-only, WAL mode (the API child and CLI write concurrently),
deduped by (surface, turn id). Never raises into the chat path — a broken
journal must never break a reply.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

MAX_TEXT = 4000


class SurfaceJournal:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            f"file:{self.db_path}?timeout=10",
            uri=True,
            check_same_thread=False,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS surface_turns (
                   id TEXT PRIMARY KEY,
                   surface TEXT NOT NULL,
                   role TEXT NOT NULL,
                   text TEXT NOT NULL,
                   ts REAL NOT NULL
               )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_surface_ts ON surface_turns (ts)")
        self.conn.commit()

    def add(self, surface: str, role: str, text: str) -> str:
        """Record one turn. Returns the turn id. Never raises."""
        if not text or not text.strip():
            return ""
        tid = uuid.uuid4().hex[:10]
        try:
            self.conn.execute(
                "INSERT INTO surface_turns (id, surface, role, text, ts) VALUES (?,?,?,?,?)",
                (tid, surface, role, text[:MAX_TEXT], time.time()),
            )
            self.conn.commit()
        except Exception:
            return ""
        return tid

    def recent_cross(self, surface: str, limit: int = 6, max_age_s: float = 86400) -> list[dict]:
        """Last `limit` turns from OTHER surfaces, oldest first, fresh only."""
        cutoff = time.time() - max_age_s
        try:
            rows = self.conn.execute(
                "SELECT surface, role, text, ts FROM surface_turns "
                "WHERE surface != ? AND ts > ? ORDER BY ts DESC LIMIT ?",
                (surface, cutoff, limit),
            ).fetchall()
        except Exception:
            return []
        return [{"surface": r[0], "role": r[1], "text": r[2], "ts": r[3]} for r in reversed(rows)]

    def context_block(self, surface: str, limit: int = 6) -> str:
        """Ready-to-inject system-context string, '' when nothing to add."""
        turns = self.recent_cross(surface, limit)
        if not turns:
            return ""
        lines = []
        for t in turns:
            who = "You" if t["role"] == "user" else "JARVIS"
            via = t["surface"]
            lines.append(f"[{via}] {who}: {t['text'][:280]}")
        return (
            "Recent conversation from your other devices (same session, continue it naturally):\n"
            + "\n".join(lines)
        )

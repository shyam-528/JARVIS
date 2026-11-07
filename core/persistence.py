"""Persistent conversation history (``data/conversations.db``).

Contract
--------
- One row per session in ``sessions``; messages reference their session.
- ``add_message`` raises ``ValueError`` for an unknown session_id — writing
  an orphan message would be an invisible data-integrity failure.
- ``delete_session`` deletes messages first (SQLite FK enforcement is off
  in this codebase), so no orphans survive a session delete.
- ``search`` is full-text (FTS5). User input is sanitized to quoted terms
  and any residual FTS error degrades to ``[]`` — fail-soft by design,
  never an exception to the caller.
- Connections come from ``core.db.connect`` (WAL + busy_timeout).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from .db import connect as _db_connect
from .memory import _sanitize_fts


class ConversationStore:
    """Persistent conversation history: sessions, messages, full-text search."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New Conversation',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                provider TEXT DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions (updated_at DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content, content='messages', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
            END;
        """)
        self.conn.commit()

    # ---------- sessions ----------
    def create_session(self, title: str = "New Conversation") -> str:
        session_id = str(uuid.uuid4())[:12]
        now = time.time()
        self.conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (session_id, title, now, now),
        )
        self.conn.commit()
        return session_id

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, title, created_at, updated_at, message_count FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "title": row[1],
            "created_at": row[2],
            "updated_at": row[3],
            "message_count": row[4],
        }

    def list_sessions(self, limit: int = 50, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else "WHERE archived = 0"
        rows = self.conn.execute(
            f"SELECT id, title, created_at, updated_at, message_count FROM sessions {where} ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "message_count": r[4],
            }
            for r in rows
        ]

    def update_title(self, session_id: str, title: str):
        self.conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, time.time(), session_id),
        )
        self.conn.commit()

    def archive_session(self, session_id: str, archived: bool = True):
        self.conn.execute(
            "UPDATE sessions SET archived = ? WHERE id = ?",
            (1 if archived else 0, session_id),
        )
        self.conn.commit()

    def delete_session(self, session_id: str) -> bool:
        _cur = self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur2 = self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()
        return cur2.rowcount > 0

    def latest_session_id(self) -> str | None:
        # rowid tiebreak: Windows time.time() ticks ~15ms, rapid creates tie
        row = self.conn.execute(
            "SELECT id FROM sessions WHERE archived = 0 ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    # ---------- messages ----------
    def add_message(self, session_id: str, role: str, content: str, provider: str = ""):
        if not self.conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone():
            raise ValueError(f"unknown session: {session_id!r}")
        now = time.time()
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, provider, created_at) VALUES (?,?,?,?,?)",
            (session_id, role, content, provider, now),
        )
        self.conn.execute(
            "UPDATE sessions SET updated_at = ?, message_count = message_count + 1 WHERE id = ?",
            (now, session_id),
        )
        # Auto-title from first user message
        count = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
        ).fetchone()[0]
        if count == 1 and role == "user":
            title = content[:60].strip()
            if len(content) > 60:
                title += "..."
            self.conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        self.conn.commit()

    def get_messages(self, session_id: str, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content, provider, created_at FROM messages WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": r[0], "content": r[1], "provider": r[2], "created_at": r[3]} for r in rows]

    def get_last_n_messages(self, session_id: str, n: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content, provider, created_at FROM messages WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, n),
        ).fetchall()
        return [
            {"role": r[0], "content": r[1], "provider": r[2], "created_at": r[3]}
            for r in reversed(rows)
        ]

    # ---------- search ----------
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across all conversations.

        FTS-hostile input (quotes, AND/OR, parens) is sanitized to quoted
        terms; a residual FTS error returns [] instead of raising.
        """
        match = _sanitize_fts(query)
        if not match:
            return []
        try:
            rows = self.conn.execute(
                """SELECT m.session_id, m.role, m.content, m.created_at, s.title
                   FROM messages_fts f
                   JOIN messages m ON m.id = f.rowid
                   JOIN sessions s ON s.id = m.session_id
                   WHERE messages_fts MATCH ?
                   ORDER BY m.created_at DESC LIMIT ?""",
                (match, limit),
            ).fetchall()
        except Exception:
            return []

        return [
            {
                "session_id": r[0],
                "role": r[1],
                "content": r[2],
                "created_at": r[3],
                "session_title": r[4],
            }
            for r in rows
        ]

    def search_sessions(self, query: str, limit: int = 10) -> list[dict]:
        """Search and return unique sessions that match."""
        results = self.search(query, limit=100)
        seen = set()
        sessions = []
        for r in results:
            if r["session_id"] not in seen:
                seen.add(r["session_id"])
                sessions.append(
                    {
                        "session_id": r["session_id"],
                        "title": r["session_title"],
                        "snippet": r["content"][:200],
                    }
                )
                if len(sessions) >= limit:
                    break
        return sessions

    def total_messages(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def total_sessions(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

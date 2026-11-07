from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path


def _connect(db_path: Path):
    """SQLite connection with WAL + busy timeout: the CLI and its API child
    process share this token DB, so lock contention must wait, not raise."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn


class AuthManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = _connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used TEXT,
                expires_at TEXT,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.commit()
        conn.close()

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_token(self, name: str, expires_days: int = None) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)
        created = time.strftime("%Y-%m-%d %H:%M:%S")
        expires = None
        if expires_days:
            expires = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + expires_days * 86400)
            )

        conn = _connect(self.db_path)
        conn.execute(
            "INSERT INTO tokens VALUES (?,?,?,?,?,1)", (token_hash, name, created, None, expires)
        )
        conn.commit()
        conn.close()
        return token

    def verify_token(self, token: str) -> bool:
        if not token:
            return False
        token_hash = self._hash_token(token)
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT 1 FROM tokens WHERE token_hash = ? AND active = 1 AND (expires_at IS NULL OR expires_at > ?)",
            (token_hash, time.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tokens SET last_used = ? WHERE token_hash = ?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), token_hash),
            )
            conn.commit()
        conn.close()
        return bool(row)

    def has_valid_token(self) -> bool:
        """True if any token is active AND unexpired.

        Backs the mint route's bootstrap gate: once every token has expired,
        first-boot minting must re-open or the dashboard is locked out
        forever. That re-opened window *is* the refresh flow — same exposure
        profile as day-one bootstrap (no valid token exists, so anyone on the
        LAN can mint until one does).
        """
        conn = _connect(self.db_path)
        row = conn.execute(
            "SELECT 1 FROM tokens WHERE active = 1 "
            "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            (time.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchone()
        conn.close()
        return bool(row)

    def revoke_token(self, token: str) -> bool:
        token_hash = self._hash_token(token)
        conn = _connect(self.db_path)
        cur = conn.execute("UPDATE tokens SET active = 0 WHERE token_hash = ?", (token_hash,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def list_tokens(self) -> list[dict]:
        conn = _connect(self.db_path)
        rows = conn.execute(
            "SELECT name, created_at, last_used, expires_at, active FROM tokens"
        ).fetchall()
        conn.close()
        return [
            {
                "name": r[0],
                "created_at": r[1],
                "last_used": r[2],
                "expires_at": r[3],
                "active": bool(r[4]),
            }
            for r in rows
        ]

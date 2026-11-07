"""Account-scoped storage for the public web app; independent of desktop data."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SESSION_SECONDS = 7 * 24 * 3600


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return f"{salt}:{key.hex()}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class WebStore:
    def __init__(self, path: Path, database_url: str = ""):
        self.path = path
        self.database_url = database_url
        if not database_url:
            path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, password TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id)
                    ON DELETE CASCADE, title TEXT NOT NULL, updated REAL NOT NULL,
                    lease TEXT, busy_until REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS conversation_owner
                    ON conversations(user_id, updated);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id)
                    ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS message_conversation
                    ON messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS limits (
                    bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        if self.database_url:
            from api.public_postgres import connect

            with connect(self.database_url) as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, email: str, name: str, password: str) -> dict:
        user = {"id": secrets.token_hex(16), "email": email, "name": name}
        hashed = password_hash(password)
        with self.connect() as db:
            db.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                (user["id"], email, name, hashed, time.time()),
            )
        return user

    def login(self, email: str, password: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        # Run the same expensive KDF for unknown accounts, too.
        stored = row["password"] if row else "00" * 16 + ":" + "00" * 64
        candidate = password_hash(password, stored.split(":")[0])
        if not hmac.compare_digest(candidate, stored) or row is None:
            return None
        return {key: row[key] for key in ("id", "email", "name")}

    def session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (token_hash(token), user_id, time.time() + SESSION_SECONDS),
            )
        return token

    def authenticate(self, token: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT u.id, u.email, u.name FROM users u
                JOIN sessions s ON u.id=s.user_id WHERE s.token=? AND s.expires>?""",
                (token_hash(token), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))

    def allow(self, key: str, maximum: int, seconds: int) -> bool:
        now = time.time()
        bucket = f"{key}:{int(now // seconds)}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM limits WHERE expires < ?", (now,))
            row = db.execute("SELECT count FROM limits WHERE bucket=?", (bucket,)).fetchone()
            if row and row[0] >= maximum:
                return False
            db.execute(
                """INSERT INTO limits VALUES (?, 1, ?)
                ON CONFLICT(bucket) DO UPDATE SET count=count+1""",
                (bucket, (int(now // seconds) + 1) * seconds),
            )
        return True

    def usage(self, user_id: str) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT count FROM limits WHERE bucket=?",
                (f"chat:{user_id}:{int(time.time() // 86400)}",),
            ).fetchone()
        return row[0] if row else 0

    def list_conversations(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, title, updated FROM conversations WHERE user_id=? ORDER BY updated DESC",
                    (user_id,),
                )
            ]

    def create_conversation(self, user_id: str) -> dict:
        item = {"id": secrets.token_hex(16), "title": "New conversation", "updated": time.time()}
        with self.connect() as db:
            db.execute(
                "INSERT INTO conversations(id,user_id,title,updated) VALUES (?,?,?,?)",
                (item["id"], user_id, item["title"], item["updated"]),
            )
        return item

    @staticmethod
    def owned(db, user_id: str, conversation_id: str):
        row = db.execute(
            "SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
        ).fetchone()
        if row is None:
            raise KeyError("Conversation not found")
        return row

    def messages(self, user_id: str, conversation_id: str) -> list[dict]:
        with self.connect() as db:
            self.owned(db, user_id, conversation_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT role, content, created FROM messages WHERE conversation_id=? ORDER BY id",
                    (conversation_id,),
                )
            ]

    def delete_conversation(self, user_id: str, conversation_id: str):
        with self.connect() as db:
            self.owned(db, user_id, conversation_id)
            db.execute(
                "DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
            )

    def reserve(self, user_id: str, conversation_id: str) -> str:
        lease = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned(db, user_id, conversation_id)
            if row["busy_until"] > time.time():
                raise ValueError("A reply is already being generated for this conversation.")
            db.execute(
                "UPDATE conversations SET lease=?, busy_until=? WHERE id=?",
                (lease, time.time() + 120, conversation_id),
            )
        return lease

    def release(self, user_id: str, conversation_id: str, lease: str):
        with self.connect() as db:
            db.execute(
                """UPDATE conversations SET lease=NULL, busy_until=0
                WHERE id=? AND user_id=? AND lease=?""",
                (conversation_id, user_id, lease),
            )

    def save_turn(self, user_id: str, conversation_id: str, lease: str, message: str, answer: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned(db, user_id, conversation_id)
            if row["lease"] != lease:
                raise ValueError("This request expired. Please try again.")
            now = time.time()
            first = (
                db.execute(
                    "SELECT 1 FROM messages WHERE conversation_id=? LIMIT 1", (conversation_id,)
                ).fetchone()
                is None
            )
            db.executemany(
                "INSERT INTO messages(conversation_id,role,content,created) VALUES (?,?,?,?)",
                [
                    (conversation_id, "user", message, now),
                    (conversation_id, "assistant", answer, now),
                ],
            )
            title = " ".join(message.split())[:64] if first else row["title"]
            db.execute(
                "UPDATE conversations SET title=?, updated=? WHERE id=?",
                (title, now, conversation_id),
            )

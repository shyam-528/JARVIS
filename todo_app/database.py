"""
SQLite database layer for the todo application.
"""

import sqlite3
from pathlib import Path

from todo_app.models import Task, TaskPriority, TaskStatus


class Database:
    """SQLite database wrapper."""

    def __init__(self, db_path: Path):
        """Initialize database connection."""
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with row factory."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        return conn

    def _init_db(self) -> None:
        """Initialize database tables."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP NULL
                )
            """)
            conn.commit()

    def add_task(self, title: str, description: str, priority: str) -> Task:
        """Add a new task and return it."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (title, description, priority)
                VALUES (?, ?, ?)
                """,
                (title, description, priority),
            )
            task_id = cursor.lastrowid
            conn.commit()

            return self.get_task(task_id)

    def get_task(self, task_id: int) -> Task | None:
        """Get a task by ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

            if row:
                return self._row_to_task(row)
            return None

    def list_tasks(
        self, status_filter: TaskStatus | None = None, sort_by: str = "created"
    ) -> list[Task]:
        """List tasks with optional filtering and sorting."""
        # P0: map public sort keys to real columns (was: ORDER BY created -> no such column).
        sort_map = {"created": "created_at", "priority": "priority", "title": "title"}
        sort_col = sort_map.get(sort_by, "created_at")
        # Semantic priority order: high first, then medium, low.
        if sort_col == "priority":
            order_expr = (
                "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at DESC"
            )
        elif sort_col == "title":
            order_expr = "title COLLATE NOCASE ASC"
        else:
            order_expr = "created_at DESC"

        query = "SELECT * FROM tasks"
        params = []

        if status_filter:
            query += " WHERE status = ?"
            params.append(status_filter.value)

        query += f" ORDER BY {order_expr}"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_task(row) for row in rows]

    def complete_task(self, task_id: int) -> bool:
        """Mark task as completed."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
                """,
                (task_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def uncomplete_task(self, task_id: int) -> bool:
        """Mark task as pending."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'pending', completed_at = NULL
                WHERE id = ? AND status = 'completed'
                """,
                (task_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def delete_task(self, task_id: int) -> bool:
        """Delete a task."""
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_stats(self) -> dict:
        """Get task statistics."""
        with self._get_connection() as conn:
            # Total counts
            total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'completed'"
            ).fetchone()[0]
            pending = total - completed

            # Priority breakdown
            high_priority = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE priority = 'high'"
            ).fetchone()[0]
            medium_priority = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE priority = 'medium'"
            ).fetchone()[0]
            low_priority = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE priority = 'low'"
            ).fetchone()[0]

            return {
                "total": total,
                "completed": completed,
                "pending": pending,
                "high_priority": high_priority,
                "medium_priority": medium_priority,
                "low_priority": low_priority,
            }

    @staticmethod
    def _parse_ts(value) -> __import__("datetime").datetime | None:
        # P0: SQLite CURRENT_TIMESTAMP returns str; Task expects datetime.
        if value is None or isinstance(value, __import__("datetime").datetime):
            return value
        from datetime import datetime

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(str(value), fmt)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """Convert database row to Task object."""
        return Task(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            status=TaskStatus(row["status"]),
            priority=TaskPriority(row["priority"]),
            created_at=self._parse_ts(row["created_at"]),
            completed_at=self._parse_ts(row["completed_at"]),
        )

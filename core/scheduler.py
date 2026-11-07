"""Persistent task scheduler (cron/interval/once/daily/weekly) on SQLite.

Failure policy (Wave 7):
- A scheduled action failure is loud (printed), recorded on the task as
  ``last_error`` (surfaced via /health), retried once after 60s, and recurring
  tasks are pushed to their next slot so one failure never wedges the schedule.
- Storage corruption is loud: a bad cron expression or unparseable stored row
  is announced and that task is skipped/disabled — never silently dropped.
- Best-effort setup (WAL pragma, additive migrations) degrades gracefully but
  always with a printed diagnostic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config


@dataclass
class ScheduledTask:
    id: str
    name: str
    trigger_type: str  # "cron", "interval", "once", "daily", "weekly"
    trigger_spec: dict  # cron: {"expression": "0 9 * * *"}; interval: {"seconds": 3600}; once: {"run_at": "2024-01-01T09:00:00"}
    action: str  # tool name to call
    action_args: dict = field(default_factory=dict)
    enabled: bool = True
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    next_run: str | None = None
    last_run: str | None = None
    run_count: int = 0
    max_runs: int | None = None
    last_error: str | None = None


class TaskScheduler:
    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.db_path.parent / "scheduler.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(self.db_path, timeout=30.0)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=30000")
            except Exception as exc:
                # fail-soft: DB still works without WAL, just less concurrent —
                # but a degraded scheduler DB must not be invisible
                print(f"[scheduler] WAL/busy_timeout pragma failed on {self.db_path}: {exc}")
            self._init_db()
        except Exception as exc:
            # CHAOS FINDING: a garbage scheduler.db raised DatabaseError out of
            # the constructor — taking the whole brain down at boot (the
            # orchestrator constructs this in __init__ with no fallback).
            # Same contract as the JSON stores: quarantine aside (loud) and
            # start fresh. Quarantine needs the handle closed first (Windows
            # locks open files); stale -wal/-shm siblings go with it.
            print(f"[scheduler] database unusable ({type(exc).__name__}: {exc}) — quarantining")
            self._quarantine_db()
            self._conn = sqlite3.connect(self.db_path, timeout=30.0)
            self._init_db()
        self._tasks: dict[str, ScheduledTask] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._orchestrator: Any | None = None
        self._load_tasks()

    def _quarantine_db(self) -> None:
        """Move a corrupt scheduler DB (+ WAL sidecars) aside. Best-effort."""
        with contextlib.suppress(Exception):
            self._conn.close()
        stamp = int(time.time())
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.db_path) + suffix)
            if not src.exists():
                continue
            try:
                dst = src.with_name(f"{src.name}.corrupt-{stamp}")
                os.replace(src, dst)
                print(f"[scheduler] quarantined to {dst.name} (nothing deleted)")
            except Exception as exc:
                print(f"[scheduler] could not quarantine {src.name}: {exc}")

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_spec TEXT NOT NULL,
                action TEXT NOT NULL,
                action_args TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                next_run TEXT,
                last_run TEXT,
                run_count INTEGER NOT NULL DEFAULT 0,
                max_runs INTEGER,
                last_error TEXT
            )
        """)
        # Additive migration for older DBs
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()]
            if "last_error" not in cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN last_error TEXT")
        except Exception as exc:
            # fail-soft: pre-migration DBs keep working (last_error stays
            # in-memory) — but the migration failure must be visible
            print(f"[scheduler] last_error column migration failed: {exc}")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_next_run ON tasks (next_run)")
        self._conn.commit()

    def set_orchestrator(self, orch: Any):
        self._orchestrator = orch

    def _load_tasks(self):
        rows = self._conn.execute("SELECT * FROM tasks WHERE enabled = 1").fetchall()
        for row in rows:
            # Row may be 12 cols (old) or 13 (with last_error)
            last_err = row[12] if len(row) > 12 else None
            try:
                task = ScheduledTask(
                    id=row[0],
                    name=row[1],
                    trigger_type=row[2],
                    trigger_spec=json.loads(row[3]),
                    action=row[4],
                    action_args=json.loads(row[5]),
                    enabled=bool(row[6]),
                    created_at=row[7],
                    next_run=row[8],
                    last_run=row[9],
                    run_count=row[10],
                    max_runs=row[11] if row[11] is not None else None,
                    last_error=last_err,
                )
                self._compute_next_run(task)
            except Exception as exc:
                # fail-soft: one corrupt row must not take down the whole
                # schedule — but a skipped task must never be invisible
                print(
                    f"[scheduler] skipping corrupt stored task row "
                    f"id={row[0]!r} name={row[1]!r}: {type(exc).__name__}: {exc}"
                )
                continue
            self._tasks[task.id] = task

    def _save_task(self, task: ScheduledTask):
        self._conn.execute(
            """INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.id,
                task.name,
                task.trigger_type,
                json.dumps(task.trigger_spec),
                task.action,
                json.dumps(task.action_args),
                1 if task.enabled else 0,
                task.created_at,
                task.next_run,
                task.last_run,
                task.run_count,
                task.max_runs,
                getattr(task, "last_error", None),
            ),
        )
        self._conn.commit()

    def _delete_task(self, task_id: str):
        self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()

    def _compute_next_run(self, task: ScheduledTask) -> str | None:
        now = datetime.now()
        spec = task.trigger_spec

        if task.trigger_type == "once":
            run_at = datetime.fromisoformat(spec["run_at"])
            task.next_run = run_at.isoformat() if run_at > now else None
        elif task.trigger_type == "interval":
            seconds = spec.get("seconds", 3600)
            if task.last_run:
                last = datetime.fromisoformat(task.last_run)
                task.next_run = (last + timedelta(seconds=seconds)).isoformat()
            else:
                task.next_run = (now + timedelta(seconds=seconds)).isoformat()
        elif task.trigger_type == "daily":
            hour = spec.get("hour", 9)
            minute = spec.get("minute", 0)
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            task.next_run = next_run.isoformat()
        elif task.trigger_type == "weekly":
            weekday = spec.get("weekday", 0)  # 0=Monday
            hour = spec.get("hour", 9)
            minute = spec.get("minute", 0)
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0:
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    days_ahead = 7
            next_run = now + timedelta(days=days_ahead)
            next_run = next_run.replace(hour=hour, minute=minute, second=0, microsecond=0)
            task.next_run = next_run.isoformat()
        elif task.trigger_type == "cron":
            try:
                from croniter import croniter

                cron = croniter(spec["expression"], now)
                task.next_run = cron.get_next(datetime).isoformat()
            except Exception as exc:
                # A bad cron expression silently disabled the task before
                # Wave 7 — announce it and record why on the task
                task.next_run = None
                task.last_error = f"bad cron expression: {exc}"[:300]
                print(
                    f"[scheduler] task '{task.name}' has an unparseable cron "
                    f"expression {spec.get('expression')!r}: {exc} — disabling next_run"
                )
        else:
            task.next_run = None

        if task.max_runs is not None and task.run_count >= task.max_runs:
            task.next_run = None
        return task.next_run

    def add_task(
        self,
        name: str,
        trigger_type: str,
        trigger_spec: dict,
        action: str,
        action_args: dict = None,
        max_runs: int = None,
    ) -> str:
        # Idempotent: replace an existing enabled task with the same name
        # (prevents boot-time re-registration from stacking duplicates).
        for existing in list(self._tasks.values()):
            if existing.name == name:
                self.remove_task(existing.id)

        task_id = str(uuid.uuid4())[:8]
        task = ScheduledTask(
            id=task_id,
            name=name,
            trigger_type=trigger_type,
            trigger_spec=trigger_spec,
            action=action,
            action_args=action_args or {},
            max_runs=max_runs,
        )
        self._compute_next_run(task)
        self._tasks[task_id] = task
        self._save_task(task)
        return task_id

    def remove_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._delete_task(task_id)
            return True
        return False

    def find_tasks_by_name(self, name: str) -> list[ScheduledTask]:
        """Return enabled tasks whose name matches (case-insensitive substring)."""
        needle = (name or "").strip().lower()
        if not needle:
            return []
        return [t for t in self._tasks.values() if needle in t.name.lower()]

    def remove_task_by_name(self, name: str) -> int:
        """Remove enabled tasks matching `name`. Returns how many were removed."""
        matches = self.find_tasks_by_name(name)
        for t in matches:
            self.remove_task(t.id)
        return len(matches)

    def enable_task(self, task_id: str, enabled: bool = True) -> bool:
        if task_id in self._tasks:
            self._tasks[task_id].enabled = enabled
            self._save_task(self._tasks[task_id])
            return True
        return False

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    async def start(self):
        if self._running:
            return
        self._running = True
        self._catch_up_missed()
        self._loop_task = asyncio.create_task(self._run_loop())

    def _catch_up_missed(self):
        """Handle tasks whose slot passed while JARVIS was offline. Without this,
        a reminder set for 9pm that comes due while the process is down is
        silently skipped forever.

        Rules:
        - one-shot missed by <24h: claim + fire once (next_run=None first, so
          the run loop cannot double-fire the same slot);
        - one-shot missed by >24h: claim WITHOUT firing — too late to be
          useful, and the stale next_run must not reach the run loop;
        - recurring: a stale next_run must never reach the run loop. User-facing
          jobs skip the stale slot (a 7:30 briefing at a 9pm boot is not spoken);
          housekeeping jobs with trigger_spec {"catch_up_always": true} fire the
          missed slot once — a missed VACUUM is still useful days late.
        """
        now = datetime.now()
        now_iso = now.isoformat()
        fired = []
        for task in self._tasks.values():
            if not task.enabled:
                continue
            if task.trigger_type == "once":
                # next_run alone can't detect a missed one-shot: add_task already
                # nulls it when run_at is past, so the due time lives in the spec
                # (that's what _missed_due_time checks).
                due = self._missed_due_time(task, now)
                stale = task.next_run is not None and task.next_run <= now_iso
                if due is None and not stale:
                    continue  # future one-shot — nothing missed
                task.next_run = None  # claim either way, so the loop never sees it
                if due is not None:
                    task.last_run = now_iso
                    task.run_count += 1
                self._save_task(task)
                if due is not None:
                    print(
                        f"[scheduler] catch-up: firing missed task '{task.name}' (was due {due.isoformat()})"
                    )
                    asyncio.get_event_loop().create_task(self._execute_task_action(task))
                    fired.append(task.name)
                continue
            # Recurring: a stale next_run must never reach the run loop.
            if not task.next_run or task.next_run > now_iso:
                continue  # nothing missed
            if task.trigger_spec.get("catch_up_always"):
                task.last_run = now_iso
                task.run_count += 1
                self._compute_next_run(task)
                self._save_task(task)
                print(f"[scheduler] catch-up: firing missed recurring job '{task.name}'")
                asyncio.get_event_loop().create_task(self._execute_task_action(task))
                fired.append(task.name)
            else:
                self._compute_next_run(task)
                self._save_task(task)
        if fired:
            print(f"[scheduler] catch-up fired {len(fired)} missed task(s): {', '.join(fired)}")

    def _missed_due_time(self, task: ScheduledTask, now: datetime):
        """Return the due datetime of a task that was missed in the last 24h,
        or None if there is nothing to catch up."""
        candidates = []
        if task.next_run:
            try:
                candidates.append(datetime.fromisoformat(task.next_run))
            except ValueError as exc:
                # fail-soft: corrupt stored next_run is skipped for catch-up;
                # loud because it indicates storage corruption
                print(
                    f"[scheduler] task '{task.name}' has corrupt next_run "
                    f"{task.next_run!r}: {exc} — skipping for catch-up"
                )
        if task.trigger_type == "once" and not task.last_run:
            # Past once-tasks have next_run=None; the real due time lives in the spec
            try:
                candidates.append(datetime.fromisoformat(task.trigger_spec["run_at"]))
            except (KeyError, ValueError) as exc:
                # fail-soft: malformed once-spec just can't be caught up;
                # loud because it indicates storage corruption
                print(
                    f"[scheduler] task '{task.name}' has malformed once-spec "
                    f"{task.trigger_spec!r}: {exc} — skipping for catch-up"
                )
        for due in candidates:
            if now >= due and (now - due) < timedelta(days=1):
                return due
        return None

    async def _maybe_voice(self, action: str, action_args: dict) -> None:
        """Speak reminders/briefings/alerts aloud after they fire.
        Telegram delivery already happened via the tool; this only adds voice."""
        if action not in ("notify_reminder", "notify_alert", "notify_briefing"):
            return
        orch = self._orchestrator
        speak = getattr(orch, "speak_alert", None)
        if not callable(speak):
            return
        try:
            if action == "notify_briefing":
                kind = (action_args or {}).get("kind", "morning")
                text = (
                    "Good morning, sir. Your briefing has arrived on Telegram."
                    if kind != "evening"
                    else "Good evening, sir. Your daily review is on Telegram."
                )
            else:
                args = action_args or {}
                title = str(args.get("title", "")).strip()
                msg = str(args.get("message", "")).strip()
                text = f"{title}. {msg}".strip(". ") if title else msg
            if text:
                await speak(text)
        except Exception as exc:
            print(f"[scheduler] voice hook skipped: {exc}")

    async def _execute_task_action(self, task: ScheduledTask):
        """Run just the tool action (booking already done by the caller)."""
        if not self._orchestrator:
            print(f"[scheduler] no orchestrator for task {task.name}")
            return
        try:
            await self._orchestrator.toolbox.execute(task.action, json.dumps(task.action_args))
            await self._maybe_voice(task.action, task.action_args)
        except Exception as exc:
            print(f"[scheduler] task {task.name} failed: {exc}")

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            # Expected shutdown path: the task was just cancelled by us.
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task

    async def _run_loop(self):
        while self._running:
            now = datetime.now().isoformat()
            due_tasks = [
                t for t in self._tasks.values() if t.enabled and t.next_run and t.next_run <= now
            ]
            for task in due_tasks:
                await self._execute_task(task)
            await asyncio.sleep(30)

    async def _execute_task(self, task: ScheduledTask):
        if not self._orchestrator:
            print(f"[scheduler] no orchestrator for task {task.name}")
            return
        try:
            print(f"[scheduler] running task: {task.name}")
            await self._orchestrator.toolbox.execute(task.action, json.dumps(task.action_args))
            await self._maybe_voice(task.action, task.action_args)
            task.last_run = datetime.now().isoformat()
            task.run_count += 1
            task.last_error = None
            self._compute_next_run(task)
            self._save_task(task)
        except Exception as exc:
            # P1: one retry after 60s, record error for /health
            print(f"[scheduler] task {task.name} failed: {exc} — retry in 60s")
            try:
                await asyncio.sleep(60)
                await self._orchestrator.toolbox.execute(task.action, json.dumps(task.action_args))
                await self._maybe_voice(task.action, task.action_args)
                task.last_run = datetime.now().isoformat()
                task.run_count += 1
                task.last_error = None
                self._compute_next_run(task)
            except Exception as exc2:
                task.last_error = str(exc2)[:300]
                print(f"[scheduler] task {task.name} retry also failed: {exc2}")
                # push next slot so one failure doesn't wedge recurring tasks
                try:
                    self._compute_next_run(task)
                    if task.next_run is None and task.trigger_type in (
                        "daily",
                        "interval",
                        "weekly",
                        "cron",
                    ):
                        # keep alive: recompute already did; if still None, schedule +1 day
                        pass
                except Exception as exc3:
                    print(f"[scheduler] could not reschedule task {task.name}: {exc3}")
            self._save_task(task)

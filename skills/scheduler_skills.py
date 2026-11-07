"""Scheduler tools: schedule/list/remove/enable tasks and reminders.
Registered only when the scheduler is available.
"""

from __future__ import annotations

from core.scheduler import TaskScheduler
from core.tools import Tool, ToolBox


def register_scheduler(box: ToolBox, scheduler: TaskScheduler):
    async def schedule_task(
        name: str,
        trigger_type: str,
        trigger_spec,
        action: str,
        action_args="{}",
        max_runs: int = None,
    ) -> str:
        import json

        def _parse(v, what):
            if isinstance(v, dict):
                return v
            try:
                return json.loads(v) if v else {}
            except json.JSONDecodeError as e:
                raise ValueError(f"invalid {what} JSON - {e}") from e

        try:
            spec = _parse(trigger_spec, "trigger_spec")
            args = _parse(action_args, "action_args")
        except ValueError as e:
            return f"error: {e}"
        tid = scheduler.add_task(name, trigger_type, spec, action, args, max_runs)
        return f"scheduled '{name}' (id: {tid}, next: see /tasks)"

    async def list_tasks() -> str:
        tasks = scheduler.list_tasks()
        if not tasks:
            return "no scheduled tasks"
        lines = []
        for t in tasks:
            next_str = t.next_run or "never"
            lines.append(
                f"- {t.id}: {t.name} [{t.trigger_type}] next: {next_str} runs: {t.run_count}"
            )
        return "\n".join(lines)

    async def remove_task(task_id: str = "", name: str = "") -> str:
        if task_id:
            if scheduler.remove_task(task_id):
                return f"removed task {task_id}"
            return f"task {task_id} not found"
        if name:
            removed = scheduler.remove_task_by_name(name)
            if removed:
                return f"removed {removed} task(s) matching '{name}'"
            return f"no task matching '{name}'"
        return "error: provide task_id or name"

    async def enable_task(task_id: str, enabled: bool = True) -> str:
        if scheduler.enable_task(task_id, enabled):
            return f"task {task_id} {'enabled' if enabled else 'disabled'}"
        return f"task {task_id} not found"

    async def mark_day(date: str = "", title: str = "") -> str:
        """Mark a calendar day: "mark Diwali on Oct 20" (brain resolves the
        date) -> one-time 9am reminder. Shows in briefings + /tasks."""
        import re as _re
        from datetime import datetime as _dt

        clean = str(title or "").strip()
        if not clean:
            return "error: title required (what should I mark the day as?)"
        day = str(date or "").strip()
        try:
            fire = _dt.fromisoformat(day).date()
        except Exception:
            return (
                f"error: date must be ISO (YYYY-MM-DD), got {day!r}. "
                "Resolve relative dates first (today is in your context)."
            )
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return f"error: date must be ISO (YYYY-MM-DD), got {day!r}."
        tid = scheduler.add_task(
            f"Day: {clean}",
            "once",
            {"run_at": f"{fire.isoformat()}T09:00:00"},
            "notify_reminder",
            {"message": clean, "title": clean},
        )
        return (
            f"marked {fire.isoformat()} as '{clean}' (id: {tid}) — in your briefing that morning."
        )

    async def day_events(date: str = "") -> str:
        """What's marked on a day (ISO YYYY-MM-DD): reminders + events due."""
        import re as _re

        day = str(date or "").strip()
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return f"error: date must be ISO (YYYY-MM-DD), got {day!r}."
        hits = [
            t for t in scheduler.list_tasks() if t.enabled and str(t.next_run or "").startswith(day)
        ]
        if not hits:
            return f"nothing marked on {day}."
        return "\n".join(f"- {t.name} (next: {t.next_run})" for t in hits)

    box.register(
        Tool(
            name="schedule_task",
            description=(
                'Create a scheduled task/reminder. trigger_type: once (trigger_spec like {"run_at": "2026-09-01T21:00:00"}), '
                'daily ({"hour": 21, "minute": 0}), interval ({"seconds": 3600}), weekly, cron ({"expression": "0 9 * * *"}). '
                'action: tool name, e.g. notify_reminder. action_args: JSON like {"message": "...", "title": "..."}. '
                "For one-time reminders use trigger_type=once with a full datetime, or daily for recurring times."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "trigger_type": {
                        "type": "string",
                        "enum": ["cron", "interval", "once", "daily", "weekly"],
                    },
                    "trigger_spec": {"type": "string"},
                    "action": {"type": "string"},
                    "action_args": {"type": "string", "default": "{}"},
                    "max_runs": {"type": "integer"},
                },
                "required": ["name", "trigger_type", "trigger_spec", "action"],
            },
            handler=schedule_task,
        )
    )
    box.register(
        Tool(
            name="list_scheduled_tasks",
            description="List all scheduled tasks",
            parameters={"type": "object", "properties": {}},
            handler=list_tasks,
        )
    )
    box.register(
        Tool(
            name="remove_scheduled_task",
            description=(
                "Delete a scheduled task/reminder. Provide task_id OR name (case-insensitive partial match). "
                "To delete a reminder, call list_scheduled_tasks first to find it, then call this."
            ),
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}, "name": {"type": "string"}},
            },
            handler=remove_task,
        )
    )
    box.register(
        Tool(
            name="enable_scheduled_task",
            description="Enable or disable a scheduled task",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "enabled": {"type": "boolean", "default": True},
                },
                "required": ["task_id"],
            },
            handler=enable_task,
        )
    )
    box.register(
        Tool(
            name="mark_day",
            description=(
                'Mark a calendar day ("mark Diwali on 2026-10-20"): creates a one-time '
                "9am reminder shown in briefings + /tasks. date MUST be ISO YYYY-MM-DD "
                "(resolve today/this-Friday/festival names to ISO first — the current "
                "date is in your context). title is the event name."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["date", "title"],
            },
            handler=mark_day,
        )
    )
    box.register(
        Tool(
            name="day_events",
            description=(
                "List what's marked on one day (ISO YYYY-MM-DD): reminders and events due that day."
            ),
            parameters={
                "type": "object",
                "properties": {"date": {"type": "string"}},
                "required": ["date"],
            },
            handler=day_events,
        )
    )

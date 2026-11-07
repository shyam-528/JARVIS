"""One-tap routines (roadmap #4): bundles of existing pieces, on-demand only.

  "starting work" (morning_start): brief + inbox triage + today's tasks.
  "winding down"  (evening_wrap):  evening review + tomorrow preview.

On-demand: no scheduler slots, no push gate — the caller asked, so the
combined text goes straight back on the invoking surface. Every section
degrades to an "unavailable" line; the routine never raises.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core.tools import Tool


def _tasks_due(orch, day, include_overdue: bool) -> list[str]:
    """Once/daily reminders relevant for `day` (a date). Best-effort."""
    out = []
    try:
        tasks = orch.scheduler.list_tasks()
    except Exception:
        return []
    for t in tasks or []:
        try:
            if not getattr(t, "enabled", True):
                continue
            if getattr(t, "run_count", 0) or getattr(t, "last_run", None):
                continue
            ttype = getattr(t, "trigger_type", "")
            spec = getattr(t, "trigger_spec", {}) or {}
            if ttype == "daily":
                out.append(f"{t.name} (daily)")
            elif ttype == "once":
                run_at = datetime.fromisoformat(str(spec.get("run_at", "")))
                if run_at.date() == day or (include_overdue and run_at.date() < day):
                    out.append(f"{t.name} ({run_at.strftime('%H:%M')})")
        except (ValueError, TypeError):
            continue
    return out[:5]


async def _brief(kind: str) -> str:
    try:
        from skills.briefing import generate_briefing

        return (await generate_briefing(kind)).strip()
    except Exception:
        return ""


async def _triage(orch) -> str:
    try:
        return (await orch.toolbox.execute("mail_triage", '{"limit": 20}')).strip()
    except Exception:
        return ""


def _today_events() -> list[str]:
    try:
        from core.calendar_feed import fetch_today_events

        return [
            f"{e['start'].strftime('%H:%M')} {str(e.get('summary', ''))[:80]}"
            for e in fetch_today_events()
        ][:6]
    except Exception:
        return []


def _tomorrow_events(tomorrow) -> list[str]:
    try:
        from core.calendar_feed import fetch_upcoming_events

        out = []
        for e in fetch_upcoming_events(days=2):
            try:
                if e.get("start") is not None and e["start"].date() == tomorrow:
                    out.append(f"{e['start'].strftime('%H:%M')} {str(e.get('summary', ''))[:80]}")
            except Exception:
                continue
        return out[:6]
    except Exception:
        return []


def register_routines(box, orch) -> None:
    async def morning_start() -> str:
        """'Starting work': morning brief + inbox triage + today's tasks."""
        now = datetime.now()
        parts = [f"🌅 Starting work — {now.strftime('%A, %d %B')}"]
        brief = await _brief("morning")
        parts.append(f"\n📋 Brief\n{brief[:1200]}" if brief else "\n📋 Brief\n(unavailable)")
        triage = await _triage(orch)
        parts.append(f"\n✉️ Inbox\n{triage[:800]}" if triage else "\n✉️ Inbox\n(unavailable)")
        tasks = _tasks_due(orch, now.date(), include_overdue=True)
        parts.append(
            "\n📌 Today’s tasks\n"
            + ("\n".join(f"  - {t}" for t in tasks) if tasks else "  (nothing due)")
        )
        events = _today_events()
        parts.append(
            "\n📅 Today\n"
            + ("\n".join(f"  - {e}" for e in events) if events else "  (nothing on the calendar)")
        )
        return "\n".join(parts)[:3500]

    async def evening_wrap() -> str:
        """'Winding down': evening review + tomorrow preview."""
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).date()
        parts = [f"🌙 Winding down — {now.strftime('%A, %d %B')}"]
        review = await _brief("evening")
        parts.append(
            f"\n📋 Evening review\n{review[:1200]}"
            if review
            else "\n📋 Evening review\n(unavailable)"
        )
        events = _tomorrow_events(tomorrow)
        parts.append(
            "\n📅 Tomorrow\n"
            + ("\n".join(f"  - {e}" for e in events) if events else "  (nothing on the calendar)")
        )
        tasks = _tasks_due(orch, tomorrow, include_overdue=True)
        parts.append(
            "\n📌 Due tomorrow\n"
            + ("\n".join(f"  - {t}" for t in tasks) if tasks else "  (nothing due)")
        )
        return "\n".join(parts)[:3500]

    box.register(
        Tool(
            name="morning_start",
            description="'Starting work' routine: morning brief + inbox triage + today's tasks, one combined reply. On-demand.",
            parameters={"type": "object", "properties": {}},
            handler=morning_start,
        )
    )
    box.register(
        Tool(
            name="evening_wrap",
            description="'Winding down' routine: evening review + tomorrow preview (calendar + due reminders), one combined reply. On-demand.",
            parameters={"type": "object", "properties": {}},
            handler=evening_wrap,
        )
    )

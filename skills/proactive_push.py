"""Proactive push tools (Phase A): overnight digest + meeting watcher.

Both are scheduler-fireable toolbox tools AND user-invokable. Delivery
goes through PushGate (mute/cap/dedup) to Telegram + voice.
"""

from __future__ import annotations

from datetime import datetime

from core.proactive_push import (
    PushGate,
    compose_digest,
    digest_key,
    due_meetings,
    meeting_key,
    overnight_cutoff,
)
from core.tools import Tool


def _gate(orch) -> PushGate:
    return PushGate(orch.cfg.db_path.parent)


def _scheduler_todos(orch, now: datetime) -> list[dict]:
    """Once-reminders due (or overdue) and not yet run. Daily/interval
    churn (briefings, mail-watch) is not todo news."""
    out = []
    try:
        for t in orch.scheduler.list_tasks():
            if not getattr(t, "enabled", True):
                continue
            if getattr(t, "trigger_type", "") != "once":
                continue
            if getattr(t, "run_count", 0) or getattr(t, "last_run", None):
                continue
            spec = getattr(t, "trigger_spec", {}) or {}
            try:
                run_at = datetime.fromisoformat(str(spec.get("run_at", "")))
            except (ValueError, TypeError):
                continue
            if run_at <= now:
                out.append({"label": f"{t.name} ({run_at.strftime('%H:%M')})", "at": run_at})
    except Exception:
        pass
    out.sort(key=lambda x: x["at"])
    return [{"label": x["label"]} for x in out[:5]]


def _parse_mail_date(s: str) -> datetime | None:
    s = (s or "").strip()
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(s)
    except Exception:
        return None


def _overnight_mails(now: datetime, limit: int = 10) -> list[dict]:
    try:
        from core import mail as _mail

        res = _mail.unread(limit=limit)
    except Exception:
        return []
    if not isinstance(res, dict) or res.get("error") or not res.get("emails"):
        return []
    cutoff = overnight_cutoff(now)
    out = []
    for m in res["emails"]:
        dt = _parse_mail_date(str(m.get("date", "")))
        try:
            naive = dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt
        except Exception:
            naive = None
        if naive is not None and naive < cutoff:
            continue  # older than overnight window
        out.append(
            {
                "from": str(m.get("from", "?"))[:40],
                "subject": str(m.get("subject", "(no subject)"))[:80],
            }
        )
        if len(out) >= 5:
            break
    return out


def register_proactive_push(box, orch) -> None:
    async def proactive_digest(force: bool = False) -> str:
        """Build + deliver the overnight digest (due reminders, today's
        calendar, overnight mail). Telegram + voice. Idempotent per day
        unless force=true (explicit user order)."""
        now = datetime.now()
        todos = _scheduler_todos(orch, now)
        try:
            from core.calendar_feed import fetch_today_events

            events = [
                {"when": e["start"].strftime("%H:%M"), "title": e["summary"][:80]}
                for e in fetch_today_events()
            ]
        except Exception:
            events = []
        mails = _overnight_mails(now)
        detail, speak = compose_digest(todos, events, mails, now)
        res = await _gate(orch).send(
            "digest", digest_key(), "Morning digest", detail, speak, orch, force=bool(force)
        )
        if res.get("delivered"):
            return (
                f"Digest delivered ({len(todos)} todos, {len(events)} events, {len(mails)} mails)."
            )
        return f"Digest skipped: {res.get('reason')}."

    async def proactive_meeting_watch() -> str:
        """Check the calendar for meetings starting in ~30 min and nudge
        once each (Telegram + voice). Safe to run every 15 min."""
        now = datetime.now()
        try:
            from core.calendar_feed import fetch_today_events

            events = fetch_today_events()
        except Exception:
            return "Meeting watch skipped: calendar unreadable."
        due = due_meetings(events, now)
        if not due:
            return "Meeting watch: nothing starting in the next ~30 min."
        gate = _gate(orch)
        sent, skipped = 0, 0
        for item in due:
            e, mins = item["event"], item["in_min"]
            summary = str(e.get("summary", "(no title)"))[:80]
            start = e.get("start")
            try:
                when = start.strftime("%H:%M")
            except Exception:
                when = "soon"
            res = await gate.send(
                "meeting",
                meeting_key(None, summary, start),
                f"Meeting in ~{mins} min",
                f"🔔 {summary} at {when} (in ~{mins} min).",
                f"Heads up, sir. {summary} in {mins} minutes.",
                orch,
            )
            sent += 1 if res.get("delivered") else 0
            skipped += 0 if res.get("delivered") else 1
        return f"Meeting watch: {sent} nudge(s) sent, {skipped} skipped."

    box.register(
        Tool(
            name="proactive_digest",
            description="Build and deliver the overnight digest (due reminders, today's calendar, overnight mail) via Telegram + voice. Idempotent per day unless force=true.",
            parameters={
                "type": "object",
                "properties": {"force": {"type": "boolean", "default": False}},
            },
            handler=proactive_digest,
        )
    )
    box.register(
        Tool(
            name="proactive_meeting_watch",
            description="Nudge once per meeting starting in ~30 min (Telegram + voice). Safe every 15 min.",
            parameters={"type": "object", "properties": {}},
            handler=proactive_meeting_watch,
        )
    )

    async def proactive_nudge_sweep() -> str:
        """Phase B sweep: overdue reminders (throttled 6h each), bill/expiry
        calendar hits (once each), stale projects (weekly each). Telegram +
        voice via the push gate (mute/cap/dedup enforced). Safe every 30 min."""
        from core import proactive_push as _pp

        now = datetime.now()
        gate = _gate(orch)
        sent, skipped = 0, []

        # 1. overdue once-reminders — one nudge per task per 6h window
        try:
            for o in _pp.find_overdue(orch.scheduler.list_tasks(), now):
                key = f"nudge:overdue:{o['id']}:{_pp.nudge_bucket(now)}"
                res = await gate.send(
                    "nudge",
                    key,
                    "Overdue reminder",
                    f"⏰ {o['label']} — overdue.",
                    f"Sir, {o['short']} is overdue.",
                    orch,
                )
                sent += 1 if res.get("delivered") else 0
                if not res.get("delivered"):
                    skipped.append(f"overdue:{o['short']} ({res.get('reason')})")
        except Exception as exc:
            skipped.append(f"overdue sweep failed: {exc}")

        # 2. bill/expiry calendar hits in the next 7 days — once each, ever
        try:
            from core.calendar_feed import fetch_upcoming_events

            bills = _pp.find_bill_events(fetch_upcoming_events(days=7), now)
            for b in bills:
                res = await gate.send(
                    "nudge",
                    _pp.bill_key(b["title"], b["when"]),
                    "Bill/expiry watch",
                    f"🧾 {b['title']} — {b['when']}.",
                    f"Sir, {b['title']} is coming up {b['when']}.",
                    orch,
                )
                sent += 1 if res.get("delivered") else 0
                if not res.get("delivered") and res.get("reason") not in ("duplicate",):
                    skipped.append(f"bill:{b['title']} ({res.get('reason')})")
        except Exception as exc:
            skipped.append(f"bill sweep failed: {exc}")

        # 3. stale projects — no commits for 7+ days, once per project per week
        try:
            for p in _pp.find_stale_projects(now=now):
                res = await gate.send(
                    "nudge",
                    _pp.stale_week_key(now, p["name"]),
                    "Stale project",
                    f"🕸️ {p['name']}: no commits for {p['days']} days.",
                    f"Sir, {p['name']} has had no commits for {p['days']} days.",
                    orch,
                )
                sent += 1 if res.get("delivered") else 0
                if not res.get("delivered") and res.get("reason") not in ("duplicate",):
                    skipped.append(f"stale:{p['name']} ({res.get('reason')})")
        except Exception as exc:
            skipped.append(f"stale sweep failed: {exc}")

        summary = f"Nudge sweep: {sent} sent"
        if skipped:
            summary += f", {len(skipped)} skipped ({'; '.join(skipped[:3])})"
        return summary + "."

    box.register(
        Tool(
            name="proactive_nudge_sweep",
            description="Sweep overdue reminders (6h throttle), bill/expiry calendar hits, and stale projects. Telegram + voice via push gate. Safe every 30 min.",
            parameters={"type": "object", "properties": {}},
            handler=proactive_nudge_sweep,
        )
    )

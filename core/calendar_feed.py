"""Phase 5 calendar feed: today's events from an ICS URL, no OAuth.

Set CALENDAR_ICS_URL in .env to your calendar's secret iCal address:
- Google: Calendar settings > Integrate calendar > Secret address in iCal format
- Outlook: Settings > Calendar > Shared calendars > Publish (ICS link)
Empty = no calendar section in briefings (honest, not "schedule is clear").
"""

from __future__ import annotations

import os
import re
import urllib.request
from datetime import date, datetime, timedelta


def _prop(block: str, name: str) -> str:
    """One ICS property from a VEVENT block (module-level so loop closures
    can't capture a stale block — B023)."""
    m = re.search(rf"{name}(?:;[^:]*)?:(.*)", block)
    return m.group(1).strip() if m else ""


def _parse_ics_datetime(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def fetch_today_events(ics_url: str = "", timeout: int = 15) -> list[dict]:
    """Return today's VEVENTs as [{start, summary}]. Empty list on any failure."""
    url = ics_url or os.getenv("CALENDAR_ICS_URL", "").strip()
    if not url:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-MARK3"})
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")
    except Exception:
        return []
    # Unfold ICS line continuations
    raw = re.sub(r"\r?\n[ \t]", "", raw)
    events = []
    today = date.today()
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.DOTALL):
        start = _parse_ics_datetime(_prop(block, "DTSTART"))
        summary = _prop(block, "SUMMARY") or "(no title)"
        if start is not None and start.date() == today:
            events.append({"start": start, "summary": summary})
    events.sort(key=lambda e: e["start"])
    return events


def format_today(events: list[dict]) -> str:
    if not events:
        return ""
    lines = [f"Calendar ({len(events)} today):"]
    for e in events:
        t = e["start"].strftime("%H:%M") if e["start"].hour or e["start"].minute else "all-day"
        lines.append(f"  - {t} {e['summary'][:80]}")
    return "\n".join(lines)


def fetch_upcoming_events(days: int = 7, ics_url: str = "", timeout: int = 15) -> list[dict]:
    """VEVENTs from today through today+days as [{start, summary}].
    Empty list on any failure (no calendar configured, network down)."""
    url = ics_url or os.getenv("CALENDAR_ICS_URL", "").strip()
    if not url:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-MARK3"})
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")
    except Exception:
        return []
    raw = re.sub(r"\r?\n[ \t]", "", raw)
    today = date.today()
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.DOTALL):
        start = _parse_ics_datetime(_prop(block, "DTSTART"))
        summary = _prop(block, "SUMMARY") or "(no title)"
        if start is not None and today <= start.date() <= today + timedelta(days=max(0, days)):
            events.append({"start": start, "summary": summary})
    events.sort(key=lambda e: e["start"])
    return events

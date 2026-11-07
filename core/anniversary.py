"""Memory that compounds: 'a year ago today'.

Surfaces durable facts + past conversations from the same month/day in
prior years. Pure queries over the existing stores (no new data):
  - MemoryStore ts is "%Y-%m-%d %H:%M" -> match "-MM-DD " suffix, year < now.
  - ConversationStore sessions carry created_at epoch seconds -> same
    month/day in prior years within a ±3 day window (covers weekends).

All functions never raise; empty past returns [] / "".
"""

from __future__ import annotations

from datetime import datetime


def memory_anniversaries(memory, now: datetime | None = None, limit: int = 5) -> list[dict]:
    """Durable facts stored on this month/day in a prior year."""
    now = now or datetime.now()
    out = []
    try:
        rows = memory.all(limit=100000)
    except Exception:
        return []
    for mid, ts, text in rows:
        try:
            dt = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if dt.year >= now.year:
            continue
        if (dt.month, dt.day) != (now.month, now.day):
            continue
        out.append(
            {
                "id": mid,
                "years_ago": now.year - dt.year,
                "when": str(ts)[:10],
                "text": str(text)[:200],
            }
        )
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x["years_ago"])
    return out


def session_anniversaries(persistence, now: datetime | None = None, limit: int = 5) -> list[dict]:
    """Past conversation sessions from this date in prior years (±3 days)."""
    now = now or datetime.now()
    out = []
    try:
        sessions = persistence.list_sessions(limit=100000, include_archived=True)
    except Exception:
        return []
    for s in sessions:
        try:
            dt = datetime.fromtimestamp(float(s.get("created_at", 0)))
        except (ValueError, TypeError):
            continue
        if dt.year >= now.year:
            continue
        try:
            this_year = dt.replace(year=now.year)
        except ValueError:
            continue  # Feb 29 on a non-leap year
        if abs((this_year - now).days) > 3:
            continue
        out.append(
            {
                "id": s.get("id", "?"),
                "years_ago": now.year - dt.year,
                "when": dt.strftime("%Y-%m-%d"),
                "title": str(s.get("title", "(untitled)"))[:100],
            }
        )
        if len(out) >= limit:
            break
    out.sort(key=lambda x: (x["years_ago"], x["when"]))
    return out


def anniversary_text(memory, persistence, now: datetime | None = None) -> str:
    """One chat-ready block, or '' when the past is quiet."""
    now = now or datetime.now()
    mems = memory_anniversaries(memory, now)
    sess = session_anniversaries(persistence, now)
    if not mems and not sess:
        return ""
    lines = [f"🕰️ A year ago today ({now.strftime('%d %B')}):"]
    for m in mems:
        yr = "last year" if m["years_ago"] == 1 else f"{m['years_ago']} years ago"
        lines.append(f"  - {m['text']} ({yr})")
    for s in sess:
        yr = "last year" if s["years_ago"] == 1 else f"{s['years_ago']} years ago"
        lines.append(f"  - talked about “{s['title']}” ({yr})")
    return "\n".join(lines)[:2000]

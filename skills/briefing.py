"""Daily briefing tool: aggregates mail/tasks/schedule into a summary."""

from __future__ import annotations

import json
from datetime import datetime

from core.tools import Tool, ToolBox


async def get_weather(city: str = "") -> str:
    """Get current weather using web search."""
    try:
        from skills.web import web_search

        query = f"weather {city} now" if city else "weather now"
        result = await web_search(query, max_results=1)
        if result and "no results" not in result.lower():
            return result.strip()
    except Exception:
        pass
    return "Weather unavailable"


async def generate_briefing(kind: str = "morning") -> str:
    """Phase 1 briefing: date + tasks + memories + weather + headlines. Telegram-safe."""
    from pathlib import Path as _P

    parts: list[str] = []
    now = datetime.now()
    day = now.strftime("%A, %B %d, %Y")
    if kind == "evening":
        parts.append(f"Good evening, sir. {day}.")
    else:
        parts.append(f"Good morning, sir. It's {day}.")

    # Tasks due today (scheduler.db) — never crash briefing on DB errors
    try:
        # Lightweight read: open scheduler DB directly, no orchestrator needed

        from core.db import connect as _db_connect

        sdb = _P("data/scheduler.db")
        if sdb.exists():
            con = _db_connect(sdb)
            try:
                rows = con.execute(
                    "SELECT name, trigger_type, next_run, last_run FROM tasks WHERE enabled=1"
                ).fetchall()
            finally:
                con.close()
            today = now.date().isoformat()
            due = [r for r in rows if (r[2] or "")[:10] <= today]
            if due:
                parts.append(f"Tasks ({len(due)} due):")
                for name, _tt, nxt, _lr in due[:6]:
                    parts.append(f"  - {name} [{nxt or 'due'}]")
            else:
                parts.append("No tasks due today.")
    except Exception:
        pass

    # Memories: prefer memory_v2, fallback memory
    try:
        mem_texts: list[str] = []
        for cand in (_P("data/memory_v2.db"), _P("data/memory.db")):
            if cand.exists() and not mem_texts:
                try:
                    from core.memory import MemoryStore

                    mem = MemoryStore(cand)
                    for _, _ts, text in mem.all(5):
                        mem_texts.append(text)
                    break
                except Exception:
                    continue
        if mem_texts:
            parts.append("Recent notes:")
            for t in mem_texts[:4]:
                parts.append(f"  - {t[:140]}")
    except Exception:
        pass

    # Weather (cached 6h)
    try:
        cache = _P("data/briefing_cache.json")
        use_cache = False
        if cache.exists():
            try:
                cj = json.loads(cache.read_text(encoding="utf-8"))
                if (now.timestamp() - float(cj.get("ts", 0))) < 6 * 3600 and cj.get("weather"):
                    parts.append(f"Weather: {cj['weather'][:300]}")
                    use_cache = True
            except Exception:
                pass
        if not use_cache:
            weather = await get_weather()
            if weather and "unavailable" not in weather.lower():
                parts.append(f"Weather: {weather[:300]}")
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(
                        json.dumps({"ts": now.timestamp(), "weather": weather[:500]}),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # Headlines: one call, best-effort
    try:
        from skills.web import web_search

        news = await web_search("top technology news today", max_results=3)
        if news and "no results" not in news.lower():
            parts.append("Top tech headlines:")
            for line in [ln for ln in news.split("\n") if ln.strip()][:3]:
                parts.append(f"  {line[:180]}")
    except Exception:
        pass

    # Calendar (honest: empty URL = no section, never "schedule is clear")
    try:
        import os as _os

        from core.calendar_feed import fetch_today_events, format_today

        cal = format_today(fetch_today_events())
        if cal:
            parts.append(cal)
        elif _os.getenv("CALENDAR_ICS_URL", "").strip():
            parts.append("Calendar: nothing scheduled today.")
    except Exception:
        pass

    # Mail unread count (silent when unconfigured; hard 20s cap so a
    # stalled IMAP server can never hang the briefing)
    try:
        import asyncio as _aio

        from core import mail as _mail

        if _mail.configured():
            try:
                _res = await _aio.wait_for(_aio.to_thread(_mail.unread, 3), timeout=20)
            except Exception:
                _res = {"error": "timeout"}
            if "error" not in _res and _res.get("total", 0) > 0:
                _top = "; ".join(
                    f"{m['from'][:30]}: {m['subject'][:50]}" for m in _res["emails"][:3]
                )
                parts.append(f"Mail: {_res['total']} unread. Newest — {_top}")
            elif "error" not in _res:
                parts.append("Mail: inbox zero, sir.")
    except Exception:
        pass

    if kind == "evening":
        parts.append("Shall I carry anything over to tomorrow, sir?")
    else:
        if not any(p.startswith("Calendar (") or p.startswith("Calendar:") for p in parts):
            parts.append(
                "No calendar connected — set CALENDAR_ICS_URL in .env to see today's events here."
            )
        parts.append("Shall I prepare anything, sir?")
    out = "\n".join(parts)
    return out[:3500]


async def daily_briefing() -> str:
    return await generate_briefing("morning")


def register_briefing(box: ToolBox):
    box.register(
        Tool(
            name="daily_briefing",
            description="Generate a morning briefing with date, weather, notes, and tech news",
            parameters={"type": "object", "properties": {}},
            handler=daily_briefing,
        )
    )

"""Auto-speak policy for unprompted voice (reminders, briefings, alerts).

- Master switch: JARVIS_VOICE_ALERTS=1 (default off — no surprise talking).
- Runtime override: orchestrator.voice_alerts (True/False/None=follow env).
- Quiet hours: JARVIS_QUIET_HOURS="22-7" (default). Speech suppressed, text
  delivery (Telegram) unaffected.
"""

from __future__ import annotations

import os
from datetime import datetime


def parse_quiet(spec: str = "") -> tuple[int, int] | None:
    try:
        start_s, end_s = (spec or os.getenv("JARVIS_QUIET_HOURS", "22-7")).split("-")
        return int(start_s) % 24, int(end_s) % 24
    except Exception:
        return (22, 7)


def in_quiet_hours(now: datetime | None = None, spec: str = "") -> bool:
    window = parse_quiet(spec)
    if window is None:
        return False
    start, end = window
    hour = (now or datetime.now()).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def alerts_enabled(runtime: bool | None = None) -> bool:
    """Runtime True/False wins; None falls back to env flag."""
    if runtime is not None:
        return runtime
    return os.getenv("JARVIS_VOICE_ALERTS", "").strip() == "1"


def should_speak(runtime: bool | None = None, now: datetime | None = None) -> bool:
    return alerts_enabled(runtime) and not in_quiet_hours(now)

"""Session-unlock greeting (movie-JARVIS "welcome back, sir").

Watches the Windows lock state and, on every unlock (rate-limited), speaks
a short distilled status line through the alert voice path and prints the
full line to the terminal.

Detection: ctypes OpenInputDesktop poll. A locked workstation reports the
secure "Winlogon" desktop; an unlocked one reports "Default". No message
loop, no extra dependencies — just a 2.5s poll on a daemon thread that
hands unlock events to the asyncio loop.

Policy:
- Fires on every unlock, but at most once per `min_interval_min` (default 60).
- Quiet hours suppress *speech only* (the line still prints).
- Everything is best-effort: a greeting failure never breaks the watcher
  and never touches the rest of JARVIS.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime

from core.logging_config import get_logger

log = get_logger("greeting")

_POLL_S = 2.0
_DEBOUNCE_S = 1.5  # ignore lock->unlock flicker shorter than this
_MAIL_TIMEOUT_S = 4.0  # IMAP can be slow; never let it stall the greeting

# Greeting styles rotate so every unlock sounds different. Data-bearing
# styles (mail/reminder) fall through to the next style when there's no data.
GREETING_STYLES = ("welcome", "mail", "reminder", "build")

_WELCOME_LINES = (
    "Welcome back.",
    "Good to have you back.",
    "Back at your desk — at your service.",
    "Systems standing by.",
)

_BUILD_LINES = (
    "Ready to build something new?",
    "Shall we make something interesting today?",
    "The workbench is ready when you are.",
    "Back to the lab. What's today's project?",
)


def is_workstation_locked() -> bool:
    """True when the interactive desktop is the secure lock screen.

    Any ctypes/API failure assumes *not locked* so a broken check can never
    spam greetings.
    """
    try:
        import ctypes

        h = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_READOBJECTS
        if not h:
            return True
        try:
            buf = ctypes.create_unicode_buffer(64)
            if ctypes.windll.user32.GetUserObjectInformationW(h, 2, buf, 64, None):
                return buf.value.lower() == "winlogon"
            return False
        finally:
            ctypes.windll.user32.CloseDesktop(h)
    except Exception:
        return False


class UnlockGreeter:
    """Polls lock state on a daemon thread; fires a greeting on unlock."""

    def __init__(
        self,
        on_unlock,  # async callback() -> None
        min_interval_min: float = 60.0,
        quiet_spec: str = "22-7",
        poll_s: float = _POLL_S,
        debounce_s: float = _DEBOUNCE_S,
        locked_fn=is_workstation_locked,  # injectable for tests
    ):
        self._on_unlock = on_unlock
        self.min_interval_s = float(min_interval_min) * 60.0
        self.quiet_spec = quiet_spec
        self.poll_s = float(poll_s)
        self.debounce_s = float(debounce_s)
        self._locked_fn = locked_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Baseline is established on the FIRST poll (None = unknown). Starting
        # "locked" here would fabricate a lock->unlock edge at boot on an
        # already-unlocked PC and greet immediately after every restart.
        self._was_locked: bool | None = None
        self._last_fire = 0.0
        self.greetings_fired = 0

    # ---------- lifecycle ----------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._thread is not None:
            return
        self._loop = loop
        self._thread = threading.Thread(target=self._run, name="unlock-greeter", daemon=True)
        self._thread.start()
        log.info("unlock greeter started (every unlock, min %.0fm apart)", self.min_interval_s / 60)

    def stop(self) -> None:
        self._stop.set()

    # ---------- policy (pure, unit-tested) ----------

    def should_fire(self, now_mono: float) -> bool:
        return (now_mono - self._last_fire) >= self.min_interval_s

    # ---------- internals ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                locked = bool(self._locked_fn())
            except Exception:
                locked = False
            if self._was_locked is None:
                # First sample: adopt the current state as baseline — no edge.
                self._was_locked = locked
            elif self._was_locked and not locked:
                time.sleep(self.debounce_s)  # confirm it wasn't a flicker
                try:
                    if self._locked_fn():
                        self._was_locked = True
                        continue
                except Exception:
                    pass
                self._was_locked = False
                self._maybe_fire()
            elif locked:
                self._was_locked = True
            self._stop.wait(self.poll_s)

    def _maybe_fire(self) -> None:
        now_mono = time.monotonic()
        if not self.should_fire(now_mono):
            log.debug("unlock ignored (%.0fs since last greeting)", now_mono - self._last_fire)
            return
        self._last_fire = now_mono
        self.greetings_fired += 1
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._on_unlock(), self._loop)
        except Exception as exc:
            log.warning("greeting dispatch failed: %s", exc)


async def _mail_update(orch) -> str | None:
    """Unread-mail line, or None on failure. Hard-capped so IMAP latency
    can never delay the greeting past ~4s."""
    try:
        import json as _json

        raw = await asyncio.wait_for(
            orch.toolbox.execute("mail_unread", '{"limit": 50}'),
            timeout=_MAIL_TIMEOUT_S,
        )
        data = _json.loads(raw) if isinstance(raw, str) else raw
        n = int(data.get("total", 0)) if isinstance(data, dict) else 0
        if n > 0:
            return f"You have {n} unread email{'s' if n != 1 else ''} waiting."
        return "Inbox is clear."
    except Exception:
        return None


def _reminder_update(orch) -> str | None:
    """Next pending one-time/daily reminder line, or None."""
    try:
        for t in orch.scheduler.list_tasks() or []:
            if (
                getattr(t, "enabled", True)
                and getattr(t, "trigger_type", "") in ("once", "daily")
                and not getattr(t, "run_count", 0)
                and not getattr(t, "last_run", None)
            ):
                return f"A reminder ahead: {t.name}."
    except Exception:
        pass
    return None


async def build_greeting_text(orch, style_index: int = 0) -> str:
    """Rotating greeting: welcome / mail / reminder / build, cycling so no
    two consecutive unlocks sound the same. Data styles fall back to a plain
    welcome when there's nothing to report. Fast: mail is hard-capped."""
    now = datetime.now()
    daypart = "morning" if now.hour < 12 else "afternoon" if now.hour < 17 else "evening"
    # "sir" on roughly every other greeting — constant "sir" reads as a
    # template; occasional use reads as a butler.
    _sir = ", sir" if style_index % 2 == 0 else ""
    lead = f"Good {daypart}{_sir}."
    style = GREETING_STYLES[style_index % len(GREETING_STYLES)]
    variant = style_index // len(GREETING_STYLES)

    if style == "mail":
        line = await _mail_update(orch)
        if line:
            return f"{lead} {line}"
        style = "welcome"
    if style == "reminder":
        line = _reminder_update(orch)
        if line:
            return f"{lead} {line}"
        style = "welcome"
    if style == "build":
        return f"{lead} {_BUILD_LINES[variant % len(_BUILD_LINES)]}"
    # welcome (incl. fallback from a data style with nothing to report):
    # pick a different line each slot so consecutive unlocks never repeat.
    slot = style_index % len(GREETING_STYLES)
    return f"{lead} {_WELCOME_LINES[(variant + slot) % len(_WELCOME_LINES)]}"


# Module-level rotation: each greeting advances one style (welcome -> mail
# -> reminder -> build -> ...), so consecutive unlocks never sound the same.
_style_counter = {"i": 0}


async def greet_once(orch, console=None, quiet_spec: str = "22-7") -> bool:
    """Fire one greeting: print always, speak unless quiet hours."""
    idx = _style_counter["i"]
    _style_counter["i"] = idx + 1
    text = await build_greeting_text(orch, idx)
    if console is not None:
        try:
            console.print(f"[bold cyan]🔔 {text}[/]")
        except Exception:
            print(text)
    else:
        print(text)
    if in_quiet_hours(datetime.now(), quiet_spec):
        log.info("greeting printed only (quiet hours)")
        return False
    return await orch.speak_alert(text)


def in_quiet_hours(now: datetime, spec: str) -> bool:
    """Parse 'HH-HH' (e.g. '22-7'); overnight windows wrap midnight."""
    try:
        start_s, end_s = spec.split("-")
        start, end = int(start_s) % 24, int(end_s) % 24
    except Exception:
        return False
    if start == end:
        return False
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end

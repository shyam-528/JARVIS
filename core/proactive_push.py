"""Proactive push engine: Jarvis initiates contact (digest, meeting prep).

Every push passes through PushGate before delivery:
  - per-kind mute toggles (user standing preference)
  - daily cap (default 6; digests count too — keeps mornings calm)
  - dedup keys (no double-send across reboots/retries)

Delivery = Telegram via notify_manager + voice via orch.speak_alert.
Text is never suppressed by quiet hours here — voice applies its own
quiet-hours/master policy inside speak_alert, so late-night pushes still
land silently on Telegram.

Failure policy (Wave 7): delivery results are returned as
{"delivered": bool, "reason": str} — the caller always learns the outcome.
State files (mute toggles, daily cap, dedup keys) guard USER PREFERENCES:
a corrupt or unwritable state file is announced loudly via print — a lost
mute preference or dedup key silently unmutes the user or double-sends.
Pure builder helpers (git activity, bill/reminder scanning) are fail-soft
by contract (return None/[] on trouble) with per-item corruption skipped.
"""

from __future__ import annotations

import contextlib
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic

KINDS = ("digest", "meeting", "nudge")

DEFAULT_MAX_PER_DAY = 6


def _max_per_day() -> int:
    try:
        return max(1, int(os.getenv("PROACTIVE_MAX_PER_DAY", str(DEFAULT_MAX_PER_DAY))))
    except ValueError as exc:
        # fail-soft: malformed env var falls back to the default — loudly,
        # because a silently ignored operator setting is a misconfiguration
        print(
            f"[proactive_push] bad PROACTIVE_MAX_PER_DAY value, using {DEFAULT_MAX_PER_DAY}: {exc}"
        )
        return DEFAULT_MAX_PER_DAY


class PushGate:
    """One gate for every proactive push. State lives in data_dir."""

    def __init__(self, data_dir: str | Path, max_per_day: int | None = None):
        self.data_dir = Path(data_dir)
        self.max_per_day = max_per_day or _max_per_day()
        self._count_path = self.data_dir / "proactive_push.json"
        self._mute_path = self.data_dir / "proactive_mute.json"
        self._sent_path = self.data_dir / "proactive_sent.jsonl"
        self._sent: set[str] | None = None

    # ---------- mute toggles ----------

    def muted(self, kind: str) -> bool:
        # P0: shared corrupt-file contract — a corrupt mute file quarantines
        # aside on first read (one loud event) instead of printing on every
        # check; later reads see "missing" and stay quiet.
        data = load_json_store(self._mute_path, default={}, owner="proactive_push", expect=dict)
        return bool(data.get(kind, False))

    def set_muted(self, kind: str, muted: bool) -> None:
        data = load_json_store(self._mute_path, default={}, owner="proactive_push", expect=dict)
        data[kind] = bool(muted)
        if not save_json_atomic(self._mute_path, data):
            # The user's mute preference was just LOST — loud
            print(f"[proactive_push] failed to persist mute {kind}={muted}")

    # ---------- daily cap ----------

    def today_count(self) -> int:
        # P0: shared corrupt-file contract — a corrupt count file quarantines
        # aside (daily cap counter resets, announced once) instead of
        # printing on every check.
        data = load_json_store(self._count_path, default={}, owner="proactive_push", expect=dict)
        if data.get("date") == date.today().isoformat():
            try:
                return int(data.get("count", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    def _bump(self) -> int:
        today = date.today().isoformat()
        count = self.today_count() + 1
        if not save_json_atomic(self._count_path, {"date": today, "count": count}):
            # The daily cap won't survive a restart — loud
            print("[proactive_push] failed to persist daily count")
        return count

    # ---------- dedup keys ----------

    def _sent_keys(self) -> set[str]:
        if self._sent is None:
            keys: set[str] = set()
            try:
                lines = self._sent_path.read_text(encoding="utf-8").splitlines()
                for line in lines[-2000:]:
                    line = line.strip()
                    if line:
                        keys.add(line)
            except FileNotFoundError:
                pass  # fail-soft: no dedup log yet = nothing sent
            except Exception as exc:
                # A lost dedup log means DOUBLE-SENDS across this boot — loud
                print(
                    f"[proactive_push] failed to read dedup log {self._sent_path}: {exc} — dedup memory lost for this session"
                )
            self._sent = keys
        return self._sent

    def seen(self, key: str) -> bool:
        return bool(key) and key in self._sent_keys()

    def _mark_sent(self, key: str) -> None:
        if not key:
            return
        self._sent_keys().add(key)
        try:
            if len(self._sent_keys()) > 2000:
                # prune: keep the tail so the file can't grow forever
                tail = sorted(self._sent_keys())[-1500:]
                self._sent = set(tail)
                self._sent_path.write_text("\n".join(tail) + "\n", encoding="utf-8")
                return
            with open(self._sent_path, "a", encoding="utf-8") as f:
                f.write(key + "\n")
        except Exception as exc:
            # Dedup key not persisted → this push can double-send after a
            # restart — loud
            print(f"[proactive_push] failed to persist dedup key {key!r}: {exc}")

    def status(self) -> dict:
        return {
            "today": self.today_count(),
            "cap": self.max_per_day,
            "muted": {k: self.muted(k) for k in KINDS},
        }

    # ---------- delivery ----------

    async def send(
        self,
        kind: str,
        key: str,
        title: str,
        message: str,
        speak: str = "",
        orch=None,
        force: bool = False,
    ) -> dict:
        """Deliver one push. force=True bypasses mute/cap/dedup (explicit
        user command). Returns {"delivered": bool, "reason": str}."""
        if not force:
            if self.muted(kind):
                return {"delivered": False, "reason": f"{kind} muted"}
            if key and self.seen(key):
                return {"delivered": False, "reason": "duplicate"}
            if self.today_count() >= self.max_per_day:
                return {"delivered": False, "reason": "daily cap reached"}
        if orch is None:
            return {"delivered": False, "reason": "no orchestrator"}
        try:
            result = await orch.notify_manager.notify(message=message, title=title)
        except Exception as exc:
            return {"delivered": False, "reason": f"notify failed: {exc}"}
        if not (isinstance(result, dict) and result.get("success")):
            err = result.get("error", "unknown") if isinstance(result, dict) else "unknown"
            return {"delivered": False, "reason": f"notify failed: {err}"}
        if speak.strip():
            # text delivered; voice is best-effort
            with contextlib.suppress(Exception):
                await orch.speak_alert(speak.strip()[:300])
        self._bump()
        self._mark_sent(key)
        return {"delivered": True, "reason": "sent"}


# ---------- digest + meeting builders (pure data in, text out) ----------


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def compose_digest(
    todos: list[dict],
    events: list[dict],
    mails: list[dict],
    now: datetime | None = None,
) -> tuple[str, str]:
    """Build the overnight digest. Returns (telegram_text, voice_line)."""
    now = now or datetime.now()
    lines = [f"☀️ Morning digest — {now.strftime('%A, %d %B')}"]
    speak_bits: list[str] = []

    if todos:
        lines.append(f"\n📌 Reminders due ({len(todos)}):")
        for t in todos[:5]:
            lines.append(f"  - {t['label']}")
        speak_bits.append(f"{len(todos)} reminder{'s' if len(todos) != 1 else ''} due")
    else:
        lines.append("\n📌 No reminders due.")

    if events:
        lines.append(f"\n📅 Today ({len(events)}):")
        for e in events[:6]:
            lines.append(f"  - {e['when']} {e['title']}")
        speak_bits.append(f"{len(events)} meeting{'s' if len(events) != 1 else ''} today")
    else:
        lines.append("\n📅 Nothing on the calendar.")

    if mails:
        lines.append(f"\n✉️ Unread ({len(mails)} shown):")
        for m in mails[:5]:
            lines.append(f"  - {m['from']}: {m['subject']}")
        speak_bits.append(f"{len(mails)} unread mails")
    else:
        lines.append("\n✉️ Inbox quiet overnight.")

    detail = "\n".join(lines)
    if speak_bits:
        speak = f"Good morning, sir. {', '.join(speak_bits)}. Details are on Telegram."
    else:
        speak = "Good morning, sir. Quiet night — nothing pressing."
    return detail, speak


def due_meetings(
    events: list[dict],
    now: datetime | None = None,
    lead_min: int = 30,
    window_min: int = 15,
) -> list[dict]:
    """Events starting within [lead-window, lead+window] minutes that still
    need a prep nudge. Each event dict: {start: datetime, summary: str}."""
    now = now or datetime.now()
    lo = lead_min - window_min
    hi = lead_min + window_min
    out = []
    for e in events:
        start = e.get("start")
        if start is None:
            continue
        mins = (start - now).total_seconds() / 60.0
        if lo <= mins <= hi:
            out.append({"event": e, "in_min": int(round(mins))})
    return out


def meeting_key(day: date | None, summary: str, start: datetime) -> str:
    day = day or date.today()
    safe = "".join(c if c.isalnum() else "_" for c in (summary or "")[:40])
    return f"meeting:{day.isoformat()}:{start.strftime('%H%M')}:{safe}"


def digest_key(day: date | None = None) -> str:
    return f"digest:{(day or date.today()).isoformat()}"


# ---------- Phase B: nudge finders (pure data in, candidates out) ----------

NUDGE_THROTTLE_H = 6
STALE_DAYS = 7
BILL_KEYWORDS = (
    "bill",
    "payment",
    "due",
    "expir",
    "renew",
    "invoice",
    "emi",
    "recharge",
    "subscription",
    "premium",
)


def nudge_bucket(now: datetime | None = None, hours: int = NUDGE_THROTTLE_H) -> int:
    """Time bucket so one nudge fires per window (dedup key component)."""
    now = now or datetime.now()
    return int(now.timestamp() // (hours * 3600))


def find_overdue(tasks, now: datetime | None = None) -> list[dict]:
    """Enabled once-reminders past due and never run.
    Returns [{id, label, short, overdue_min}]. Daily/interval churn excluded."""
    now = now or datetime.now()
    out = []
    for t in tasks or []:
        try:
            if not getattr(t, "enabled", True):
                continue
            if getattr(t, "trigger_type", "") != "once":
                continue
            if getattr(t, "run_count", 0) or getattr(t, "last_run", None):
                continue
            run_at = datetime.fromisoformat(
                str((getattr(t, "trigger_spec", {}) or {}).get("run_at", ""))
            )
        except (ValueError, TypeError):
            continue  # fail-soft: one malformed task row must not break the digest
        if run_at <= now:
            mins = int((now - run_at).total_seconds() // 60)
            name = str(getattr(t, "name", "reminder"))[:80]
            out.append(
                {
                    "id": str(getattr(t, "id", "")),
                    "label": f"{name} (due {run_at.strftime('%H:%M')})",
                    "short": name,
                    "overdue_min": mins,
                }
            )
    out.sort(key=lambda x: x["overdue_min"], reverse=True)
    return out


def _resolve_git() -> str | None:
    import shutil

    found = shutil.which("git")
    if found:
        return found
    for cand in (r"C:\Program Files\Git\bin\git.exe", r"C:\Program Files (x86)\Git\bin\git.exe"):
        try:
            if Path(cand).exists():
                return cand
        except Exception:
            continue  # fail-soft: exists() basically never raises; try next candidate
    return None


def git_last_activity(repo: str | Path) -> datetime | None:
    """Latest commit time of a local git repo, else None (not a repo / no git)."""
    git = _resolve_git()
    if git is None:
        return None
    try:
        import subprocess

        r = subprocess.run(
            [git, "-C", str(repo), "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        ts = r.stdout.strip().split()[0] if r.returncode == 0 and r.stdout.strip() else ""
        return datetime.fromtimestamp(int(ts)) if ts else None
    except Exception:
        # fail-soft by contract: "not a repo / no git" → None; callers treat
        # None as 'no data' and simply skip the nudge for this repo
        return None


def default_project_roots() -> list[str]:
    roots: list[str] = []
    try:
        extra = os.getenv("PROACTIVE_PROJECT_ROOTS", "").strip()
        if extra:
            roots.extend([p for p in extra.split(os.pathsep) if p.strip()])
        home_projects = Path.home() / "projects"
        if home_projects.is_dir():
            roots.append(str(home_projects))
        mark3 = Path(__file__).resolve().parent.parent
        roots.append(str(mark3))
    except Exception:
        pass  # fail-soft: odd HOME/env setups just yield fewer roots
    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def find_stale_projects(
    roots=None, stale_days: int = STALE_DAYS, now: datetime | None = None
) -> list[dict]:
    """Git repos under roots with no commits for >= stale_days.
    Returns [{name, days}]. Repos without history are skipped (honest)."""
    now = now or datetime.now()
    out = []
    for root in roots if roots is not None else default_project_roots():
        try:
            base = Path(root)
            if not base.is_dir():
                continue
            candidates = (
                [base] if (base / ".git").is_dir() else [d for d in base.iterdir() if d.is_dir()]
            )
            for repo in candidates:
                if not (repo / ".git").is_dir():
                    continue
                last = git_last_activity(repo)
                if last is None:
                    continue
                days = (now - last).days
                if days >= stale_days:
                    out.append({"name": repo.name, "days": days})
        except Exception as exc:
            # fail-soft: one unreadable root (permissions etc.) must not kill
            # the whole stale-repo scan — but say which root was skipped
            print(f"[proactive_push] stale-repo scan skipped root {root}: {exc}")
            continue
    return out


def stale_week_key(now: datetime | None = None, name: str = "") -> str:
    now = now or datetime.now()
    year, week, _ = now.isocalendar()
    safe = "".join(c if c.isalnum() else "_" for c in name[:40])
    return f"nudge:stale:{year}W{week:02d}:{safe}"


def find_bill_events(events, now: datetime | None = None) -> list[dict]:
    """Upcoming calendar events matching bill/expiry keywords.
    Each event: {start: datetime, summary: str}. Returns [{title, when}]."""
    now = now or datetime.now()
    out = []
    for e in events or []:
        try:
            summary = str(e.get("summary", ""))
            start = e.get("start")
            if not summary or start is None:
                continue
            if start < now.replace(hour=0, minute=0, second=0, microsecond=0):
                continue
            if not any(k in summary.lower() for k in BILL_KEYWORDS):
                continue
            out.append(
                {
                    "title": summary[:80],
                    "when": start.strftime("%a %H:%M")
                    if start.date() != now.date()
                    else start.strftime("today %H:%M"),
                }
            )
        except Exception:
            continue  # fail-soft: one malformed calendar event must not break the bill scan
    return out


def bill_key(title: str, when: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in (title or "")[:40])
    safe_when = "".join(c if c.isalnum() else "_" for c in (when or "")[:20])
    return f"nudge:bill:{safe}:{safe_when}"


def overnight_cutoff(now: datetime | None = None) -> datetime:
    """Mails newer than yesterday 22:00 count as 'overnight'."""
    now = now or datetime.now()
    return (now - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)

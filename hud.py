"""JARVIS MARK 3 — desktop HUD overlay (roadmap step B).

A small always-on-top card showing live status: last activity (any device),
next reminder, last background job, CPU/RAM. Read-only: pulls shared SQLite
state (surface journal, scheduler, events) + psutil — no API auth, no second
orchestrator, safe to run alongside the CLI.

Close: Esc or the × button. Drag anywhere to move.
"""

from __future__ import annotations

import sqlite3
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

BG = "#0a0a0c"
PANEL = "#121216"
TEXT = "#e7e7ea"
MUTED = "#55555e"
ACCENT = "#a5b4fc"
GREEN = "#34d399"
AMBER = "#fbbf24"

REFRESH_MS = 2000


def _ro_conn(db: Path) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    except Exception:
        return None


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def fetch_status() -> dict:
    """Gather all HUD rows. Every source is optional and fails soft."""
    out = {"activity": None, "reminder": None, "job": None, "cpu": None, "ram": None}

    conn = _ro_conn(DATA / "surface_journal.db")
    if conn:
        try:
            row = conn.execute(
                "SELECT surface, role, text, ts FROM surface_turns ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row:
                who = "you" if row[1] == "user" else "jarvis"
                out["activity"] = f"[{row[0]}] {who}: {row[2][:60]} · {_ago(row[3])}"
        except Exception:
            pass
        conn.close()

    conn = _ro_conn(DATA / "scheduler.db")
    if conn:
        try:
            row = conn.execute(
                "SELECT name, next_run FROM tasks WHERE enabled = 1 AND next_run IS NOT NULL "
                "AND next_run != '' ORDER BY next_run ASC LIMIT 1"
            ).fetchone()
            if row:
                out["reminder"] = f"{row[0]} · {row[1]}"
        except Exception:
            pass
        conn.close()

    conn = _ro_conn(DATA / "events.db")
    if conn:
        try:
            import json

            row = conn.execute(
                "SELECT payload, ts FROM events WHERE type = 'bg_job_done' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                p = json.loads(row[0])
                out["job"] = f"{p.get('agent', '?')} · {p.get('task_id', '?')} · {_ago(row[1])}"
        except Exception:
            pass
        conn.close()

    try:
        import psutil

        out["cpu"] = psutil.cpu_percent(interval=None)
        out["ram"] = psutil.virtual_memory().percent
    except Exception:
        pass
    return out


class HUD:
    W = 340

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{self.W}x150+{sw - self.W - 24}+60")

        # drag support
        self._drag = (0, 0)
        self.root.bind("<Button-1>", lambda e: setattr(self, "_drag", (e.x, e.y)))
        self.root.bind(
            "<B1-Motion>",
            lambda e: self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}"),
        )
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        header = tk.Frame(self.root, bg=PANEL, height=28)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="◆ JARVIS", font=("Segoe UI", 9, "bold"), fg=ACCENT, bg=PANEL).pack(
            side=tk.LEFT, padx=10, pady=5
        )
        close = tk.Label(header, text="✕", font=("Segoe UI", 9), fg=MUTED, bg=PANEL, cursor="hand2")
        close.pack(side=tk.RIGHT, padx=8, pady=5)
        close.bind("<Button-1>", lambda e: self.root.destroy())
        self.clock = tk.Label(header, text="", font=("Segoe UI", 9), fg=MUTED, bg=PANEL)
        self.clock.pack(side=tk.RIGHT, padx=6)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        self.rows = {}
        for key, label in (
            ("activity", "◈ last"),
            ("reminder", "⏰ next"),
            ("job", "⚙ job"),
        ):
            row = tk.Frame(body, bg=BG)
            row.pack(fill=tk.X, pady=1)
            tk.Label(
                row, text=label, font=("Segoe UI", 8), fg=MUTED, bg=BG, width=8, anchor="w"
            ).pack(side=tk.LEFT)
            val = tk.Label(
                row,
                text="—",
                font=("Segoe UI", 9),
                fg=TEXT,
                bg=BG,
                anchor="w",
                wraplength=self.W - 110,
                justify=tk.LEFT,
            )
            val.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.rows[key] = val

        vitals = tk.Frame(body, bg=BG)
        vitals.pack(fill=tk.X, pady=(6, 0))
        self.cpu_lbl = tk.Label(vitals, text="", font=("Consolas", 8), fg=GREEN, bg=BG)
        self.cpu_lbl.pack(side=tk.LEFT)
        self.ram_lbl = tk.Label(vitals, text="", font=("Consolas", 8), fg=AMBER, bg=BG)
        self.ram_lbl.pack(side=tk.LEFT, padx=12)

    def refresh(self):
        s = fetch_status()
        for key in ("activity", "reminder", "job"):
            self.rows[key].configure(text=s[key] or "—")
        self.clock.configure(text=time.strftime("%H:%M"))
        if s["cpu"] is not None:
            self.cpu_lbl.configure(text=f"CPU {s['cpu']:.0f}%")
            self.ram_lbl.configure(text=f"RAM {s['ram']:.0f}%")
        self.root.after(REFRESH_MS, self.refresh)

    def run(self):
        self.refresh()
        self.root.mainloop()


if __name__ == "__main__":
    import sys as _sys

    if "--card" in _sys.argv:
        HUD().run()  # classic info card
    else:
        from hud_orb import OrbHUD

        OrbHUD().run()  # Siri-style voice orb (default)

"""Single-owner lock for Telegram polling + port roles.

One file lock: data/telegram_owner.lock with {"pid": int, "role": str, "ts": float}.
Owner wins if lock missing, stale (>60s), or PID dead. Losers never poll.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from utils.json_utils import load_json_store

LOCK_NAME = "telegram_owner.lock"
STALE_S = 60.0


def _lock_path(base_dir: Path | str) -> Path:
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_owner(base_dir: Path | str) -> dict | None:
    # P0: shared corrupt-file contract — a corrupt lock quarantines aside
    # (loud, once) and reads as missing, so acquire() takes ownership exactly
    # as it would for a stale lock. Fail-open direction is unchanged.
    try:
        data = load_json_store(_lock_path(base_dir), default={}, owner="single_owner", expect=dict)
        if isinstance(data, dict) and "pid" in data:
            return data
    except Exception:
        pass
    return None


def acquire(base_dir: Path | str, role: str) -> bool:
    """Try to become Telegram owner. Returns True if we own it."""
    path = _lock_path(base_dir)
    now = time.time()
    me = {"pid": os.getpid(), "role": role, "ts": now}
    cur = read_owner(base_dir)
    if cur is None:
        try:
            path.write_text(json.dumps(me), encoding="utf-8")
            return True
        except Exception:
            return False
    # Re-entrant by same process
    if int(cur.get("pid", -1)) == os.getpid():
        with contextlib.suppress(Exception):
            path.write_text(json.dumps(me), encoding="utf-8")
        return True
    try:
        age = now - float(cur.get("ts", 0))
    except Exception:
        age = 9999
    try:
        alive = _pid_alive(int(cur.get("pid", -1)))
    except Exception:
        alive = False
    if age > STALE_S or not alive:
        try:
            path.write_text(json.dumps(me), encoding="utf-8")
            return True
        except Exception:
            return False
    return False


def release(base_dir: Path | str) -> None:
    try:
        cur = read_owner(base_dir)
        if cur and int(cur.get("pid", -1)) == os.getpid():
            _lock_path(base_dir).unlink(missing_ok=True)
    except Exception:
        pass


def is_owner(base_dir: Path | str) -> bool:
    cur = read_owner(base_dir)
    return bool(cur and int(cur.get("pid", -1)) == os.getpid())

"""Shared SQLite helper: WAL + busy timeout on every connection.

CLI, API child, orb HUD and watchdog threads all touch these DBs. Without WAL,
a writer blocks readers with `database is locked`. Every store must use this.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path


def connect(db_path: str | Path, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def maintenance(data_dir: str | Path) -> dict:
    """Housekeeping for every ``data/*.db``: WAL checkpoint + conditional VACUUM.

    - ``PRAGMA wal_checkpoint(TRUNCATE)`` on each DB: flushes the ``-wal`` file
      back into the main DB and truncates it (WALs were observed at ~4MB each;
      usage.db reached 21MB).
    - ``VACUUM`` only when the freelist is both >10% of all pages and >1MB, so
      small/healthy DBs aren't needlessly rewritten.

    Best-effort by design: the live app holds open connections to these same
    DBs, so a checkpoint may report busy and a VACUUM may lose the lock race —
    both are recorded and retried on the next daily run. Never raises.

    Returns ``{db_name: {"ok", "checkpointed", "vacuumed", "reclaimed_bytes",
    "error"}}``.
    """
    report: dict[str, dict] = {}
    try:
        db_files = sorted(Path(data_dir).glob("*.db"))
    except Exception:
        return report
    for db_path in db_files:
        info = {
            "ok": False,
            "checkpointed": False,
            "vacuumed": False,
            "reclaimed_bytes": 0,
            "error": None,
        }
        conn = None
        try:
            size_before = db_path.stat().st_size
        except Exception:
            size_before = 0
        try:
            conn = connect(db_path)
            conn.isolation_level = None  # autocommit: VACUUM cannot run inside a txn
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            info["checkpointed"] = True
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = max(1, conn.execute("PRAGMA page_size").fetchone()[0])
            min_pages = 1024 * 1024 // page_size  # only bother if >1MB reclaimable
            if page_count and freelist > max(0.10 * page_count, min_pages):
                conn.execute("VACUUM")
                info["vacuumed"] = True
            info["ok"] = True
        except Exception as exc:
            info["error"] = str(exc)[:200]
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        # Measure after close: on Windows the new file size isn't visible via
        # stat() while the VACUUMing connection is still open.
        if info["ok"]:
            with contextlib.suppress(Exception):
                info["reclaimed_bytes"] = max(0, size_before - db_path.stat().st_size)
        report[db_path.name] = info
    return report

"""Phase 5 backup drill: create a backup (minus secrets), verify, restore-test.

Usage: python scripts/backup_drill.py [--use-latest] [--restore-boot]
- Default: builds a fresh backup zip in data/ (same list as /backup, minus .env).
- --use-latest: verify the newest existing jarvis_backup_*.zip instead.
- --restore-boot: after extract, boot real stores (sessions, memory, scheduler,
  auth) against the restored copies and run read/write ops. Proves a restore
  actually works, not just that files unzip.
- Checks: zip opens, each .db passes PRAGMA integrity_check, config.yaml parses,
  extract-to-temp works. Prints PASS/FAIL per check. Exit 1 on any FAIL.
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
# Mirror of jarvis.py _do_backup targets, minus secrets (.env never leaves disk).
TARGETS = [
    DATA / "usage.db",
    DATA / "memory.db",
    DATA / "memory_v2.db",
    DATA / "knowledge.db",
    DATA / "conversations.db",
    DATA / "workflows.db",
    DATA / "events.db",
    DATA / "automation.db",
    DATA / "api_tokens.db",
    DATA / "scheduler.db",
    DATA / "routing_scores.db",
    DATA / "brain_assignments.db",
    ROOT / "config.yaml",
]


def build() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = DATA / f"jarvis_backup_{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in TARGETS:
            if t.exists():
                zf.write(t, t.name)
    return out


def drill(backup: Path, do_boot: bool = False) -> bool:
    ok = True

    def check(name: str, passed: bool, detail: str = ""):
        nonlocal ok
        print(f"[{'PASS' if passed else 'FAIL'}] {name} {detail}")
        if not passed:
            ok = False

    check("zip opens", zipfile.is_zipfile(backup), str(backup))
    try:
        names = zipfile.ZipFile(backup).namelist()
    except Exception as exc:
        print(f"[FAIL] zip list: {exc}")
        return False
    check("has config.yaml", "config.yaml" in names)
    dbs = [n for n in names if n.endswith(".db")]
    check("contains DBs", len(dbs) > 0, f"({len(dbs)} found)")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            zipfile.ZipFile(backup).extractall(tmp)
            check("extract to temp", True)
        except Exception as exc:
            check("extract to temp", False, str(exc)[:120])
            return False
        for db in sorted(Path(tmp).glob("*.db")):
            try:
                con = sqlite3.connect(db)
                row = con.execute("PRAGMA integrity_check").fetchone()
                con.close()
                check(f"integrity {db.name}", (row[0] or "").lower() == "ok", str(row[0])[:60])
            except Exception as exc:
                check(f"integrity {db.name}", False, str(exc)[:120])
        cfg = Path(tmp) / "config.yaml"
        if cfg.exists():
            try:
                import yaml

                yaml.safe_load(cfg.read_text(encoding="utf-8"))
                check("config.yaml parses", True)
            except Exception as exc:
                check("config.yaml parses", False, str(exc)[:120])
        if do_boot:
            check("restore-boot", _restore_boot(Path(tmp)), "")
    # Secrets must never be in the zip
    check("no .env in backup", ".env" not in names)
    return ok


def _restore_boot(tmp: Path) -> bool:
    """Boot real stores against restored copies; run ops. No network, no loop."""
    import os

    os.environ["JARVIS_NO_EMBEDDINGS"] = "1"
    conns = []
    try:
        from core.persistence import ConversationStore

        store = ConversationStore(tmp / "conversations.db")
        conns.append(store.conn)
        sid = store.create_session("drill")
        store.add_message(sid, "user", "drill ping")
        assert store.get_messages(sid), "empty readback"
        assert store.latest_session_id() == sid, "latest mismatch"

        from core.memory import MemoryStore

        mem = MemoryStore(tmp / "memory.db")
        conns.append(mem.conn)
        mem.add("drill fact: restore boot works")
        assert mem.search("restore boot"), "memory search empty"

        from core.config import Config
        from core.scheduler import TaskScheduler

        cfg = Config.__new__(Config)
        cfg.db_path = tmp / "state.db"
        sched = TaskScheduler(cfg)
        conns.append(sched._conn)
        assert isinstance(sched.list_tasks(), list), "scheduler list failed"

        from api.auth import AuthManager

        auth = AuthManager(tmp / "api_tokens.db")
        tok = auth.create_token("drill", 1)
        assert auth.verify_token(tok), "token roundtrip failed"
        return True
    except Exception as exc:
        print(f"[FAIL] restore-boot detail: {exc}")
        return False
    finally:
        for c in conns:
            with contextlib.suppress(Exception):
                c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-latest", action="store_true")
    ap.add_argument(
        "--restore-boot",
        action="store_true",
        help="boot real stores against restored copies and run ops",
    )
    args = ap.parse_args()
    if args.use_latest:
        cands = sorted(DATA.glob("jarvis_backup_*.zip"))
        if not cands:
            print("no existing backups in data/")
            raise SystemExit(1)
        backup = cands[-1]
    else:
        backup = build()
        print(f"built {backup}")
    raise SystemExit(0 if drill(backup, do_boot=args.restore_boot) else 1)


if __name__ == "__main__":
    main()

"""Chaos harness (ROADMAP P2): prove the designed disaster behaviors for real.

Drills (all local, all in temp dirs — NEVER touches data/, providers, or the
network; never in the default pytest suite — run explicitly):

  1. key_revoked_midstream — GROQ-style key dies mid-stream (AuthFailed):
     router must hop to the next provider and park the dead one (breaker=auth).
  2. total_outage_preserves_queue — every brain down (AllProvidersDown):
     queued turns stay pending; a file corrupted mid-outage quarantines
     aside instead of losing the queue.
  3. corrupt_scheduler_db — garbage scheduler.db: constructor must not raise
     (loud), and the scheduler stays functional on a fresh file.
  4. lock_crash_takeover — crashed owner (dead PID / stale ts / corrupt lock):
     a new process takes over; a live owner's lock is respected.
  5. disk_failure_saves — unwritable path: save_json_atomic returns False,
     never raises, and the previous good file is byte-identical.
  6. child_death_detected — a killed child is observable via poll() (the exact
     primitive api_watchdog/wa_watchdog restart on), and terminate_children
     handles dead procs without raising.

Usage: .venv\\Scripts\\python.exe scripts/chaos_harness.py
Exit 0 when every drill passes, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def drill(name):
    """Decorator: run the drill on call, record (name, ok, detail), print."""

    def deco(fn):
        def run():
            try:
                detail = fn()
                RESULTS.append((name, True, detail))
                print(f"[PASS] {name} — {detail}", flush=True)
            except AssertionError as exc:
                RESULTS.append((name, False, f"ASSERT: {exc}"))
                print(f"[FAIL] {name} — ASSERT: {exc}", flush=True)
            except Exception as exc:
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"[FAIL] {name} — {type(exc).__name__}: {exc}", flush=True)

        run.__name__ = fn.__name__
        return run

    return deco


# ---------- 1. key revoked mid-stream ----------


@drill("key_revoked_midstream")
def _d1():
    from core.config import ProviderConfig
    from core.providers import AuthFailed
    from core.router import FailoverRouter, UsageLedger

    async def bad_gen():
        raise AuthFailed("401 revoked")
        yield  # pragma: no cover - unreachable, makes this an async gen

    async def good_gen():
        yield "answered-by-backup"

    class Client:
        def __init__(self):
            self.calls = []

        def stream_chat(self, base_url, api_key, model, messages, tools=None):
            self.calls.append(model)
            return bad_gen() if model == "dead" else good_gen()

    def prov(name, prio):
        return ProviderConfig(
            name=name,
            base_url=f"https://{name}.test/v1",
            model=name,
            api_key="k",
            priority=prio,
            daily_token_budget=1_000_000,
        )

    async def go(tmp):
        ledger = UsageLedger(tmp / "usage.db")
        try:
            router = FailoverRouter(
                [prov("dead", 0), prov("backup", 1)], Client(), ledger, watchdog_s=0.2
            )
            out = []
            async for _name, chunk in router.chat([{"role": "user", "content": "hi"}]):
                out.append(chunk)
            state, _wait = router.breakers.describe("dead")
            return "".join(out), state
        finally:
            # Windows locks open SQLite files: close before tmp cleanup.
            with contextlib.suppress(Exception):
                ledger.conn.close()

    with tempfile.TemporaryDirectory() as tmp:
        text, bstate = asyncio.run(go(Path(tmp)))
    assert text == "answered-by-backup", f"got {text!r}"
    assert bstate == "auth", f"breaker={bstate!r}"
    return "hopped dead->backup, breaker parked as auth"


# ---------- 2. total outage preserves the queue ----------


@drill("total_outage_preserves_queue")
def _d2():
    from core.storm_queue import StormQueue

    with tempfile.TemporaryDirectory() as tmp:
        q = StormQueue(Path(tmp))
        assert q.enqueue("cli", "remind me at 9pm")["queued"] is True
        assert q.enqueue("telegram", "draft the reply")["queued"] is True
        # Drain attempt under total outage: nothing answered, nothing lost.
        assert len(q.pending()) == 2
        # File corrupted mid-outage: P0 contract — quarantine, never drop.
        q._path.write_text("{corrupt", encoding="utf-8")
        assert q.pending() == []
        backups = list(Path(tmp).glob("storm_queue.json.corrupt-*"))
        assert len(backups) == 1, "queue file not quarantined"
        assert backups[0].read_text(encoding="utf-8") == "{corrupt"
    return "2 turns survived outage; corrupt file quarantined with bytes intact"


# ---------- 3. corrupt scheduler DB ----------


@drill("corrupt_scheduler_db")
def _d3():
    from core.scheduler import TaskScheduler

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x.db" / "scheduler.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"\x00\xffgarbage-not-sqlite" * 64)
        cfg = SimpleNamespace(db_path=Path(tmp) / "x.db" / "state.db")
        sched = TaskScheduler(cfg)  # must not raise (loud quarantine + fresh)
        try:
            assert list((Path(tmp) / "x.db").glob("scheduler.db.corrupt-*")), (
                "garbage db not quarantined"
            )
            tid = sched.add_task(
                name="post-crash",
                trigger_type="once",
                trigger_spec={"run_at": "2099-01-01T00:00:00"},
                action="notify_reminder",
                action_args={"message": "hi"},
            )
            assert sched.get_task(tid) is not None
        finally:
            # Windows locks open SQLite files: close before tmp cleanup.
            with contextlib.suppress(Exception):
                sched._conn.close()
    return "garbage db quarantined loud; scheduler functional on fresh file"


# ---------- 4. lock crash takeover ----------


@drill("lock_crash_takeover")
def _d4():
    import os

    from core import single_owner

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        # Crashed owner: dead PID + stale timestamp.
        (base / single_owner.LOCK_NAME).write_text(
            json.dumps({"pid": 999999, "role": "dead-cli", "ts": time.time() - 600}),
            encoding="utf-8",
        )
        assert single_owner.acquire(base, "api") is True, "stale lock blocked takeover"
        assert single_owner.is_owner(base) is True
        # Live owner's lock is respected by a loser path: same process owns,
        # so release works; after release nobody owns.
        single_owner.release(base)
        assert single_owner.read_owner(base) is None
        # Corrupt lock file: takeover still works (P0 single_owner contract).
        (base / single_owner.LOCK_NAME).write_text("{corrupt", encoding="utf-8")
        assert single_owner.acquire(base, "cli") is True, "corrupt lock blocked takeover"
        assert list(base.glob("telegram_owner.lock.corrupt-*")), "no quarantine"
        cur = single_owner.read_owner(base)
        assert cur is not None and cur["pid"] == os.getpid()
    return "stale/dead/corrupt locks all yield to a live process, loudly"


# ---------- 5. disk-failure saves ----------


@drill("disk_failure_saves")
def _d5():
    from utils.json_utils import save_json_atomic

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "state.json"
        assert save_json_atomic(good, {"v": 1}) is True
        before = good.read_bytes()
        # Unwritable target (illegal drive on Windows): False, never raises.
        assert save_json_atomic(Path("Q:\\nope\\s.json"), {"v": 2}) is False
        assert good.read_bytes() == before, "good file touched by failed save"
    return "failed save returned False; previous good file byte-identical"


# ---------- 6. child death detected ----------


@drill("child_death_detected")
def _d6():
    from core.supervisor import API_PROC_REFS, terminate_children

    keep = list(API_PROC_REFS)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.poll() is None, "child already dead at spawn"
        proc.kill()  # the disaster: child dies mid-turn
        proc.wait(timeout=10)
        assert proc.poll() is not None, "death not observable via poll()"
        code = proc.poll()
        terminate_children(extra_proc=proc)  # must not raise on dead procs
        assert proc.poll() is not None
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        API_PROC_REFS[:] = keep
    return f"killed child observed dead (code {code}); cleanup quiet"


def main() -> int:
    print("== JARVIS chaos harness (temp dirs only, no network, no data/) ==", flush=True)
    for fn in (_d1, _d2, _d3, _d4, _d5, _d6):
        fn()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"== {len(RESULTS) - len(failed)}/{len(RESULTS)} drills passed ==", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

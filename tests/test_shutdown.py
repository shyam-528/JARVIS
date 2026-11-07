"""Shutdown-hang fix: parallel capped child kills.

terminate_children() killed 3 children with sequential 5s waits (15s+ even
healthy). aterminate_children() signals all at once under one shared cap.
Fake procs exercise the logic (poll/terminate/kill only — no real processes).
"""

from __future__ import annotations

import time

from core.supervisor import API_PROC_REFS, aterminate_children


class FakeProc:
    def __init__(self, pid, deaths_on_terminate=True):
        self.pid = pid
        self._alive = True
        self._deaths_on_terminate = deaths_on_terminate
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        if self._deaths_on_terminate:
            self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False


async def _run(procs, **kw):
    keep = list(API_PROC_REFS)
    API_PROC_REFS[:] = procs
    try:
        return await aterminate_children(**kw)
    finally:
        API_PROC_REFS[:] = keep


async def test_empty_refs_fast():
    t0 = time.monotonic()
    assert await _run([]) == {}
    assert time.monotonic() - t0 < 2.0


async def test_all_children_signalled_at_once():
    procs = [FakeProc(101), FakeProc(102), FakeProc(103)]
    out = await _run(procs)
    assert all(p.terminated for p in procs)
    assert all(v == "terminated" for v in out.values())


async def test_stubborn_child_killed_loudly(capsys):
    procs = [FakeProc(201, deaths_on_terminate=False)]
    t0 = time.monotonic()
    out = await _run(procs, total_timeout=1.0)
    dt = time.monotonic() - t0
    assert procs[0].killed is True
    assert out == {"201": "killed"}
    assert dt < 4.0, f"cap not honored: {dt:.1f}s"
    assert "killed" in capsys.readouterr().out


async def test_already_dead_skipped():
    p = FakeProc(301)
    p._alive = False
    out = await _run([p])
    assert out == {"301": "already-dead"}
    assert p.terminated is False and p.killed is False


async def test_extra_proc_covered_and_deduped():
    p = FakeProc(401)
    out = await _run([p], **{"extra_proc": p})
    assert out == {"401": "terminated"}
    assert p.terminated is True

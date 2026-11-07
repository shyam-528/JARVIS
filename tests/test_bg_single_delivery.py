"""Single-delivery: every background job result reaches the user once.

Covers the coder+normal fix: inline/wait_bg/printed results file a short
receipt (never the full text again); unshown results inject once with a
present-first instruction; side-effects (event/memory) run once per job;
failures share the same dedup.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.orchestrator import Orchestrator


def make_orch():
    orch = Orchestrator.__new__(Orchestrator)  # bypass heavy __init__
    orch._bg_notes = []

    class FakeBus:
        def __init__(self):
            self.published = []

        def publish(self, t, payload):
            self.published.append((t, payload))

    class FakeMem:
        def __init__(self):
            self.added = []

        def add(self, *a, **k):
            self.added.append((a, k))

    bus, mem = FakeBus(), FakeMem()
    orch.event_bus = bus  # type: ignore[assignment]
    orch.memory = mem  # type: ignore[assignment]
    return orch, bus, mem


def make_task(task_id="j1", status="done", result="coder output here", error="boom"):
    return SimpleNamespace(
        status=status,
        agent="coder",
        task_id=task_id,
        prompt="do the thing",
        result=result,
        error=error,
    )


def test_unshown_result_injects_full_once():
    orch, bus, mem = make_orch()
    orch._register_bg_result(make_task())
    assert len(orch._bg_notes) == 1
    assert orch._bg_notes[0]["result"] == "coder output here"
    drained = orch._drain_bg_notes()
    assert drained and "coder output here" in drained
    assert "FIRST and distinctly" in drained
    assert orch._drain_bg_notes() is None  # cleared: never twice


def test_double_register_files_once_with_single_side_effects():
    orch, bus, mem = make_orch()
    task = make_task()
    orch._register_bg_result(task)
    orch._register_bg_result(task)  # CLI notify + API both firing, e.g.
    full = [n for n in orch._bg_notes if not n.get("receipt")]
    assert len(full) == 1
    assert sum(1 for t, _ in bus.published if t == "bg_job_done") == 1
    assert len(mem.added) == 1


def test_shown_result_files_receipt_not_full_text():
    orch, bus, mem = make_orch()
    orch.mark_bg_delivered("j1")  # printed / yielded inline already
    orch._register_bg_result(make_task())
    assert len(orch._bg_notes) == 1
    note = orch._bg_notes[0]
    assert note.get("receipt") is True
    assert "coder output here" not in note["result"]
    assert "already" in note["result"]


def test_note_bg_completion_marks_then_receipts():
    orch, bus, mem = make_orch()
    orch.note_bg_completion(make_task())  # the CLI completion path
    assert "j1" in orch._bg_sets()[0]
    assert orch._bg_notes[0].get("receipt") is True


def test_failed_job_shares_dedup():
    orch, bus, mem = make_orch()
    orch._register_bg_result(make_task(status="failed"))
    assert len(orch._bg_notes) == 1
    assert "AGENT FAILED" in orch._bg_notes[0]["result"]
    orch._register_bg_result(make_task(status="failed"))
    assert len([n for n in orch._bg_notes if not n.get("receipt")]) == 1
    # failures publish no dashboard event and write no memory
    assert bus.published == []
    assert mem.added == []


def test_mark_never_raises():
    orch, bus, mem = make_orch()
    orch.mark_bg_delivered(None)
    orch.mark_bg_delivered("")
    assert orch._drain_bg_notes() is None

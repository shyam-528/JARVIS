"""Wave 7 tests for core.orchestrator's section map + job bootstrap.

The §10 gate for the god-file: the section map must exist (navigation), and
scheduled-job registration must be LOUD — a job that silently never registers
is a lost capability, not a degraded one.
"""

from __future__ import annotations

from types import SimpleNamespace

from core import orchestrator as orch_mod
from core.orchestrator import Orchestrator


class FakeScheduler:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def add_task(self, *, name, trigger_type, trigger_spec, action, action_args=None):
        if self.fail:
            raise ValueError(f"bad trigger for {name}")
        self.calls.append(
            {
                "name": name,
                "trigger_type": trigger_type,
                "trigger_spec": trigger_spec,
                "action": action,
                "action_args": action_args,
            }
        )


def test_module_docstring_has_section_map():
    doc = orchestrator_doc()
    assert "Map — read this file in sections" in doc
    # the map must name the load-bearing entry points
    for marker in ("`process()`", "`start()`", "`_add_job", "`aclose()`"):
        assert marker in doc, f"section map missing {marker}"


def orchestrator_doc() -> str:
    import core.orchestrator as m

    return m.__doc__ or ""


def test_add_job_registers_and_reports_true():
    orch = SimpleNamespace(scheduler=FakeScheduler())
    ok = Orchestrator._add_job(
        orch, "inbox-triage", "daily", {"hour": 19, "minute": 0}, "inbox_report"
    )
    assert ok is True
    assert orch.scheduler.calls == [
        {
            "name": "inbox-triage",
            "trigger_type": "daily",
            "trigger_spec": {"hour": 19, "minute": 0},
            "action": "inbox_report",
            "action_args": {},
        }
    ]


def test_add_job_failure_is_loud_and_reports_false(capsys):
    orch = SimpleNamespace(scheduler=FakeScheduler(fail=True))
    ok = Orchestrator._add_job(orch, "mail-watch", "interval", {"seconds": 300}, "mail_check_new")
    assert ok is False
    out = capsys.readouterr().out
    assert "failed to schedule 'mail-watch'" in out
    assert "mail_check_new" in out and "ValueError" in out


def test_add_job_never_raises_on_broken_scheduler():
    class Exploding:
        def add_task(self, **kwargs):
            raise RuntimeError("db is gone")

    orch = SimpleNamespace(scheduler=Exploding())
    assert (
        Orchestrator._add_job(orch, "storm-drain", "interval", {"seconds": 300}, "storm") is False
    )


def test_start_uses_add_job_for_every_registration():
    """Regression: the boot job table must not go back to silent try/except."""
    import inspect

    src = inspect.getsource(orch_mod.Orchestrator.start)
    assert "add_task(" not in src, "register jobs via _add_job (loud), not add_task directly"
    for job in (
        "morning-briefing",
        "evening-review",
        "inbox-triage",
        "memory-consolidation",
        "mail-watch",
        "db-maintenance",
        "overnight-digest",
        "meeting-watch",
        "nudge-sweep",
        "storm-drain",
    ):
        assert job in src, f"job '{job}' disappeared from the boot table"


def test_init_registrations_are_loud_too():
    import inspect

    src = inspect.getsource(orch_mod.Orchestrator.__init__)
    assert "self.scheduler.add_task(" not in src
    assert "self._add_job(" in src
    assert '"nightly distillation"' in src
    assert '"brain maintenance"' in src


class FakeBus:
    def __init__(self):
        self.events: list[tuple] = []

    def publish(self, event_type, payload=None):
        self.events.append((event_type, payload or {}))


def test_add_job_failure_is_product_visible(capsys):
    """T3 (ROADMAP P0): a failed registration must reach the product
    (_job_errors + scheduler_job_failed event), not just the console."""
    orch = SimpleNamespace(
        scheduler=FakeScheduler(fail=True),
        event_bus=FakeBus(),
        _job_errors=[],
    )
    ok = Orchestrator._add_job(orch, "mail-watch", "interval", {"seconds": 300}, "mail_check_new")
    assert ok is False
    assert orch._job_errors == [
        {
            "name": "mail-watch",
            "action": "mail_check_new",
            "error": "ValueError: bad trigger for mail-watch",
        }
    ]
    assert orch.event_bus.events == [
        (
            "scheduler_job_failed",
            {
                "name": "mail-watch",
                "action": "mail_check_new",
                "error": "ValueError: bad trigger for mail-watch",
            },
        )
    ]
    out = capsys.readouterr().out
    assert "failed to schedule 'mail-watch'" in out


def test_add_job_failure_without_bus_still_records():
    """T3: no event_bus (early boot, bare SimpleNamespace) must not raise —
    the record in _job_errors is the durable half, the event is best-effort."""

    class NoBus:
        def __init__(self):
            self.scheduler = FakeScheduler(fail=True)
            self._job_errors = []

    orch = NoBus()
    assert (
        Orchestrator._add_job(orch, "storm-drain", "interval", {"seconds": 300}, "storm") is False
    )
    assert len(orch._job_errors) == 1
    assert orch._job_errors[0]["name"] == "storm-drain"


def test_add_job_failure_without_job_errors_attr_never_raises():
    """T3: pre-existing bare-SimpleNamespace callers (wave-7 tests) keep working."""
    orch = SimpleNamespace(scheduler=FakeScheduler(fail=True))
    assert (
        Orchestrator._add_job(orch, "mail-watch", "interval", {"seconds": 300}, "mail_check_new")
        is False
    )


def test_stats_surfaces_job_errors():
    """T3: stats() carries scheduler_job_errors for the /tasks view."""
    orch = SimpleNamespace(
        scheduler=FakeScheduler(),
        ledger=SimpleNamespace(summary=lambda: {}),
        router=SimpleNamespace(total_hops=0, last_provider="x"),
        history=[],
        context_budget_tokens=24000,
        _job_errors=[{"name": "mail-watch", "action": "mail_check_new", "error": "ValueError: x"}],
    )
    assert Orchestrator.stats(orch)["scheduler_job_errors"] == orch._job_errors


def test_stats_without_job_errors_defaults_empty():
    orch = SimpleNamespace(
        scheduler=FakeScheduler(),
        ledger=SimpleNamespace(summary=lambda: {}),
        router=SimpleNamespace(total_hops=0, last_provider="x"),
        history=[],
        context_budget_tokens=24000,
    )
    assert Orchestrator.stats(orch)["scheduler_job_errors"] == []

"""Wave 6: failure-path tests for peripheral modules.

Covers the hardening done in the final §10 wave: corrupt state files are
loud + quarantined, partial model pickles keep healthy defaults, audit-write
failures never change a workflow run's outcome, tool-reported "error:"
strings count as workflow failures, and aux-subsystem persistence failures
are logged, never silently swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
import time
from types import SimpleNamespace

from core.config import Config
from core.ml_proactive import BehaviorAnalyzer, MLFeatureVector, MLProactiveEngine
from core.proactive import ProactiveEngine
from core.self_improve import ImprovementCycle, SelfImprovementLoop
from core.workflow import WorkflowEngine, WorkflowStep


def make_cfg(tmp_path) -> Config:
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return cfg


# ---------------------------------------------------------------- proactive


def test_corrupt_suggestions_file_moved_aside(tmp_path, capsys):
    (tmp_path / "suggestions.json").write_text("{not json", encoding="utf-8")
    eng = ProactiveEngine(make_cfg(tmp_path), memory=None)
    assert eng._suggestions == []
    # P0: timestamped quarantine (never overwrites a previous one) + console-loud
    assert list(tmp_path.glob("suggestions.json.corrupt-*"))
    out = capsys.readouterr().out
    assert "corrupt store suggestions.json" in out and "quarantined" in out


def test_suggestions_save_failure_logged_not_raised(tmp_path, caplog):
    eng = ProactiveEngine(make_cfg(tmp_path), memory=None)
    # Point the save target at a directory so write_text raises.
    eng.suggestions_file = tmp_path
    with caplog.at_level(logging.WARNING):
        eng._save()  # must not raise
    assert any("save failed" in r.message for r in caplog.records)


# ------------------------------------------------------------------- vision


def test_analyze_screen_is_async():
    from core.vision import analyze_screen

    assert asyncio.iscoroutinefunction(analyze_screen)


async def test_analyze_screen_capture_failure_returns_error_dict(monkeypatch):
    import core.vision as vision

    def boom():
        raise RuntimeError("no display")

    monkeypatch.setattr(vision, "_capture_manager", boom)
    result = await vision.analyze_screen("test")
    assert result["success"] is False
    assert "capture failed" in result["error"]


def test_clipboard_read_failure_returns_none_and_logs(monkeypatch, caplog):
    from PIL import ImageGrab

    import core.vision as vision

    monkeypatch.setattr(
        ImageGrab, "grabclipboard", lambda: (_ for _ in ()).throw(OSError("no clipboard"))
    )
    with caplog.at_level(logging.WARNING):
        assert vision.get_clipboard_image() is None
    assert any("clipboard" in r.message for r in caplog.records)


# ------------------------------------------------------------- ml_proactive


def _fv(ts: float, action: str = "search", tool: str = "web_search") -> MLFeatureVector:
    return MLFeatureVector(
        timestamp=ts,
        hour_of_day=10,
        day_of_week=1,
        action_type=action,
        tool_used=tool,
        duration_ms=100,
        success=True,
    )


def test_feature_vector_encoding_is_stable():
    a = _fv(1000).to_array()
    b = _fv(1000).to_array()
    assert a == b  # crc32-based: deterministic, unlike salted hash()
    assert _fv(1000, action="other").to_array() != a


def test_extract_sequences_rebuilds_without_duplication(tmp_path):
    ba = BehaviorAnalyzer(None, make_cfg(tmp_path))
    base = time.time()
    for i in range(3):
        ba.action_history.append(_fv(base + i, action="a"))
    for i in range(3):
        ba.action_history.append(_fv(base + 1000 + i, action="b"))
    first = ba._extract_sequences()
    second = ba._extract_sequences()
    assert len(first) == len(second) == 2  # no unbounded growth across calls
    assert ba._sequences is second


def test_get_suggestions_none_context_does_not_crash(tmp_path):
    eng = MLProactiveEngine(make_cfg(tmp_path), None)
    assert eng.get_suggestions(None) == []
    assert eng.get_suggestions() == []


def test_corrupt_model_pickle_loud_and_unfitted(tmp_path, capsys):
    model_dir = tmp_path / "ml_models"
    model_dir.mkdir()
    (model_dir / "models.pkl").write_bytes(b"not a pickle")
    eng = MLProactiveEngine(make_cfg(tmp_path), None)
    assert eng.behavior_analyzer.is_fitted is False
    assert "Failed to load models" in capsys.readouterr().out


def test_partial_pickle_keeps_healthy_defaults(tmp_path, capsys):
    model_dir = tmp_path / "ml_models"
    model_dir.mkdir()
    # Claims fitted but ships no detector/scaler - must NOT be trusted.
    (model_dir / "models.pkl").write_bytes(pickle.dumps({"is_fitted": True}))
    eng = MLProactiveEngine(make_cfg(tmp_path), None)
    assert eng.behavior_analyzer.is_fitted is False
    assert "missing detector/scaler" in capsys.readouterr().out


def test_detect_anomaly_broken_state_is_loud(tmp_path, capsys):
    ba = BehaviorAnalyzer(None, make_cfg(tmp_path))
    ba.is_fitted = True
    ba.anomaly_detector = SimpleNamespace(decision_function=lambda x: [0.0], predict=lambda x: [-1])
    ba.scaler = SimpleNamespace(transform=lambda x: (_ for _ in ()).throw(ValueError("bad scaler")))
    assert ba.detect_anomaly(_fv(time.time())) is None
    assert "Anomaly detection failed" in capsys.readouterr().out


# ----------------------------------------------------------------- workflow


class _FailingBox:
    """ToolBox stub: reports failure the way ToolBox does — an 'error:' string."""

    def names(self):
        return ["failing_tool"]

    async def execute(self, name, args_json):
        return "error: unknown tool 'failing_tool'"


class _Events:
    def publish(self, *a, **kw):
        pass

    def subscribe(self, *a, **kw):
        return None

    def unsubscribe(self, *a, **kw):
        pass


def _wf_engine(tmp_path):
    orch = SimpleNamespace(toolbox=_FailingBox(), scheduler=None)
    return WorkflowEngine(orch, _Events(), tmp_path / "wf.db")


async def test_run_step_flags_error_string_output(tmp_path):
    eng = _wf_engine(tmp_path)
    rec = await eng._run_step(WorkflowStep(tool="failing_tool", args={}), {})
    assert rec.status == "error"
    assert "unknown tool" in rec.error


async def test_full_run_with_error_output_marked_failed_not_ok(tmp_path):
    eng = _wf_engine(tmp_path)
    eng.create({"name": "wf-fail", "steps": [{"tool": "failing_tool"}]})
    run_id = await eng.run("wf-fail", background=False)
    detail = eng.run_detail(run_id)
    assert detail["status"] == "failed"
    assert detail["error"]


def test_record_step_audit_failure_loud_not_raised(tmp_path, capsys):
    eng = _wf_engine(tmp_path)
    eng.conn.close()  # force every audit write to fail
    eng._record_step("run-x", 0, "failing_tool", "{}", "ok")  # must not raise
    assert "step audit write failed" in capsys.readouterr().out


# -------------------------------------------------------------- self_improve


class _FakeRouter:
    async def chat(self, messages, tools=None, task_type=None):
        yield "provider", "generic"


def _improve_loop(tmp_path) -> SelfImprovementLoop:
    return SelfImprovementLoop(_FakeRouter(), client=None, config=make_cfg(tmp_path))


def _cycle() -> ImprovementCycle:
    return ImprovementCycle(
        id="c1",
        task="t",
        original_output="o",
        critique="c",
        refined_output="r",
        iterations=1,
        score_improvement=1.0,
        issues=["i"],
    )


def test_persist_cycle_failure_logged_not_raised(tmp_path, caplog):
    loop = _improve_loop(tmp_path)
    loop._conn.close()
    with caplog.at_level(logging.ERROR):
        loop._persist_cycle(_cycle())  # must not raise
    assert any("persist improvement cycle" in r.message for r in caplog.records)


def test_record_lessons_failure_logged_not_raised(tmp_path, caplog):
    loop = _improve_loop(tmp_path)
    loop._conn.close()
    with caplog.at_level(logging.ERROR):
        loop._record_lessons(["some issue"], "task")  # must not raise
    assert any("record lessons" in r.message for r in caplog.records)


def test_load_examples_skips_malformed_lines(tmp_path):
    path = tmp_path / "synthetic_data.jsonl"
    path.write_text(json.dumps({"instruction": "ok"}) + "\n{bad json\n", encoding="utf-8")
    loop = _improve_loop(tmp_path)
    assert len(loop.distiller.examples) == 1


def test_save_example_write_failure_logged(tmp_path, caplog):
    loop = _improve_loop(tmp_path)
    # Make the append target a directory so open(..., "a") raises OSError.
    loop.distiller.examples_path = tmp_path
    with caplog.at_level(logging.ERROR):
        loop.distiller._save_example({"instruction": "x"})  # must not raise
    assert loop.distiller.examples[-1]["instruction"] == "x"  # kept in memory
    assert any("persist synthetic example" in r.message for r in caplog.records)

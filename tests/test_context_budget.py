"""Defect #1 (changes.txt): context window problem.

The 24k budget only governed history while injections piled on top unchecked,
and one overflow halved the budget for the whole session (trim death spiral)
with zero visibility. Tests pin the fix: pre-flight shed order, budget
recovery, summarizer fallback, and usage accounting.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.orchestrator import Orchestrator


def _msg(role, chars):
    return {"role": role, "content": "x" * chars}


def _stub(**kw):
    kw.setdefault("history", [_msg("system", 100)])
    kw.setdefault("context_budget_tokens", 24000)
    kw.setdefault("BASE_CONTEXT_BUDGET_TOKENS", 24000)
    kw.setdefault("MIN_CONTEXT_BUDGET_TOKENS", 4000)
    kw.setdefault("SHED_ORDER", Orchestrator.SHED_ORDER)
    return SimpleNamespace(**kw)


def _parts(**kw):
    base = {
        "time": "t",
        "bg_note": None,
        "memory": None,
        "episodic": None,
        "rag": None,
        "dialogue": None,
        "prefs": None,
        "xctx": None,
    }
    base.update(kw)
    return base


# ---------- pre-flight shed order ----------


def test_small_turn_sheds_nothing():
    orch = _stub(history=[_msg("system", 100), _msg("user", 50)])
    shed, usage = Orchestrator._preflight_context(orch, _parts(memory="abc"), [])
    assert shed == set()
    assert usage["shed"] == []
    assert usage["total"] <= usage["budget"]


def test_overflow_sheds_cheapest_first():
    big = "y" * 20000  # ~5k tokens each
    orch = _stub(
        history=[_msg("system", 100), _msg("user", 100)],
        context_budget_tokens=3000,
    )
    shed, usage = Orchestrator._preflight_context(
        orch,
        _parts(xctx=big, dialogue=big, prefs="p", rag="r", episodic="e", memory="m"),
        [],
    )
    # xctx (10) and dialogue (20) go before prefs/rag/episodic/memory.
    assert "xctx" in shed and "dialogue" in shed
    assert "memory" not in shed
    assert usage["total"] <= usage["budget"]


def test_time_and_history_never_shed():
    orch = _stub(history=[_msg("system", 100), _msg("user", 100)])
    shed, _ = Orchestrator._preflight_context(orch, _parts(time="t" * 5000), [])
    assert "time" not in shed


def test_tools_count_toward_budget():
    orch = _stub(history=[_msg("system", 100)])
    big_tool = [{"name": "t", "description": "z" * 100000}]
    _, usage = Orchestrator._preflight_context(orch, _parts(), big_tool)
    assert usage["tools"] > 20000


def test_usage_recorded_for_stats():
    orch = _stub(history=[_msg("system", 100), _msg("user", 50)])
    Orchestrator._preflight_context(orch, _parts(memory="abc"), [])
    assert orch._last_context_usage["history"] > 0
    assert orch._last_context_usage["budget"] == 24000


# ---------- budget recovery ----------


def test_hard_trim_halves_and_flags():
    orch = _stub(
        history=[_msg("system", 10), _msg("user", 10), _msg("assistant", 10)],
    )
    orch._trim = Orchestrator._trim.__get__(orch)
    orch._trim_list = Orchestrator._trim_list.__get__(orch)
    Orchestrator._hard_trim(orch)
    assert orch.context_budget_tokens == 12000
    assert orch._overflowed_last_turn is True


def test_two_clean_turns_recover():
    orch = _stub(context_budget_tokens=12000)
    orch._overflowed_last_turn = True
    Orchestrator._recover_budget(orch)  # turn after overflow: no recovery
    assert orch.context_budget_tokens == 12000
    orch._overflowed_last_turn = False
    Orchestrator._recover_budget(orch)  # next clean turn: step up
    assert orch.context_budget_tokens == 24000


def test_recovery_never_exceeds_base():
    orch = _stub(context_budget_tokens=24000)
    orch._overflowed_last_turn = False
    Orchestrator._recover_budget(orch)
    assert orch.context_budget_tokens == 24000


# ---------- summarizer fallback ----------


async def test_compress_uses_summary_when_available():
    async def _ok_summary(self, msgs):
        return "SUMMARY"

    orch = _stub(history=[_msg("system", 10)] + [_msg("user", 5000) for _ in range(16)])
    orch.summarizer = SimpleNamespace(
        should_summarize=lambda h, b: True,
        config=SimpleNamespace(min_turns_to_summarize=10),
        create_rolling_summary=_ok_summary,
    )
    assert await Orchestrator._compress_history(orch) is True
    assert orch.history[1]["content"] == "SUMMARY"


async def test_compress_falls_back_when_summarizer_dies(capsys):
    orch = _stub(history=[_msg("system", 10)] + [_msg("user", 5000) for _ in range(16)])

    async def _boom(self, msgs):
        raise RuntimeError("429 exhausted")

    orch.summarizer = SimpleNamespace(
        should_summarize=lambda h, b: True,
        config=SimpleNamespace(min_turns_to_summarize=10),
        create_rolling_summary=_boom,
    )
    assert await Orchestrator._compress_history(orch) is True
    # Same shape as a real summary (system + last 5), no lingering oversize.
    assert len(orch.history) == 6
    assert orch.history[0]["role"] == "system"
    assert "deterministic trim" in capsys.readouterr().out


async def test_compress_quiet_when_not_needed():
    orch = _stub(history=[_msg("system", 10), _msg("user", 10)])
    orch.summarizer = SimpleNamespace(should_summarize=lambda h, b: False)
    assert await Orchestrator._compress_history(orch) is False
    assert len(orch.history) == 2


# ---------- trim list ----------


def test_trim_list_keeps_current_turn_and_pairs():
    msgs = [
        _msg("system", 10),
        _msg("user", 9000),
        {"role": "assistant", "content": "a" * 9000, "tool_calls": [{"id": "1"}]},
        _msg("tool", 100),
        _msg("user", 50),  # current turn — must survive
    ]
    orch = _stub()
    out = Orchestrator._trim_list(orch, msgs, 12000)
    assert out[-1]["content"] == "x" * 50
    assert out[0]["role"] == "system"
    # orphan tool-call pairing preserved: no assistant tool_calls without tool
    for i, m in enumerate(out):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            assert out[i + 1].get("role") == "tool"

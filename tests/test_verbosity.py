"""Defect #2 companion: answers adapt to question size.

Simple asks get short answers, complex asks get thorough ones, normal asks
get nothing (today's behavior, byte-identical). Explicit markers always win;
learned communication prefs shift only "normal" verdicts.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.verbosity import Verbosity, score_question


def _prefs(*blobs):
    prefs = []
    for blob in blobs:
        key, _, value = blob.partition(":")
        prefs.append(SimpleNamespace(key=key.strip(), value=value.strip(), confidence=0.9))
    return SimpleNamespace(get_for_category=lambda cat: prefs)


def test_short_questions():
    assert score_question("what time is it").level == "short"
    assert score_question("fix my phone").level == "short"
    assert score_question("hi").level == "short"
    assert score_question("thanks").level == "short"


def test_normal_questions():
    v = score_question("what did we decide about the release date and who must be informed")
    assert v.level == "normal" and v.instruction == ""


def test_complex_questions():
    v = score_question(
        "compare our postgres and sqlite options for the telemetry pipeline, "
        "including tradeoffs, migration effort, and your recommendation with reasons"
    )
    assert v.level == "detailed"
    assert "headings" in v.instruction
    v2 = score_question("why does the sync fail? how does retry work? what should I change?")
    assert v2.level == "detailed"


def test_explicit_markers_win():
    v = score_question("explain black holes in detail, thoroughly")
    assert v.level == "detailed" and v.explicit is True
    v = score_question("summarize this briefly, just the answer")
    assert v.level == "short" and v.explicit is True
    # explicit short beats complex content
    v = score_question(
        "tldr on the postgres vs sqlite tradeoffs and migration plan",
    )
    assert v.level == "short" and v.explicit is True


def test_learned_default_shifts_normal_only():
    normal_q = "what did we decide about the release date and who must be informed"
    prefs = _prefs("style: I like brief answers")
    assert score_question(normal_q, prefs).level == "short"
    prefs = _prefs("style: give verbose thorough answers")
    assert score_question(normal_q, prefs).level == "detailed"
    # ...but never starves the trivial or the complex
    assert score_question("hi", prefs).level == "short"
    assert score_question("what did we decide about the release", prefs).level == "short"
    assert (
        score_question(
            "compare postgres and sqlite tradeoffs with migration plan and recommendation",
            prefs,
        ).level
        == "detailed"
    )
    assert score_question("hi", _prefs("style: brief")).level == "short"


def test_no_prefs_no_crash():
    assert score_question("hello there").level == "short"
    assert score_question("").level == "short"
    assert score_question(None).level == "short"
    assert score_question("a normal length question about yesterday").level in (
        "short",
        "normal",
        "detailed",
    )
    v = score_question("x", SimpleNamespace(get_for_category=lambda c: 1 / 0))
    assert v.level in ("short", "normal", "detailed")


def test_verbosity_dataclass():
    assert Verbosity("normal", "r").instruction == ""
    assert "1-3 sentences" in Verbosity("short", "r").instruction
    assert "headings" in Verbosity("detailed", "r").instruction

"""Defect #2 (changes.txt): simple questions answered in heavy mode.

The background gate fired on any 3+ word message containing one heavy verb
("fix my phone" -> coder agent) and matched substrings ("taste" -> test).
Now: word boundaries everywhere, and a lone verb is never enough (needs a
domain hint). These tests pin the user's own examples.
"""

from __future__ import annotations

from core.bg_router import _pick_agent, classify_background


def _fg(text):
    d = classify_background(text)
    assert d.background is False, f"{text!r} backgrounded: {d.reason}"
    return d


def _bg(text, agent):
    d = classify_background(text)
    assert d.background is True, f"{text!r} stayed foreground"
    assert d.agent == agent, f"{text!r} -> {d.agent}, want {agent}"
    return d


# ---------- defect #2: everyday verbs stay foreground ----------


def test_verb_alone_never_backgrounds():
    _fg("fix my phone")
    _fg("make dinner")
    _fg("write it down")
    _fg("review this photo")
    _fg("design my room")
    _fg("make me laugh")


def test_substring_traps_stay_foreground():
    _fg("taste this cake")  # was: test -> coding
    _fg("whatsapp message for mom")  # was: app -> coding
    _fg("makeup tips")  # was: make -> verb
    _fg("the latest news")  # was: test -> coding... "latest" contains test


def test_quick_chats_stay_foreground():
    _fg("hi")
    _fg("what time is it")
    _fg("remind me at 9pm")
    _fg("thanks a lot for yesterday")
    _fg("compare python and rust")


# ---------- genuinely heavy work still backgrounds ----------


def test_verb_plus_hint_backgrounds():
    _bg("implement the login endpoint with tests", "coder")
    _bg("refactor the auth module now", "coder")
    _bg("write a 10 page report on EVs", "analyst")
    _bg("research competitors in background", "researcher")


def test_explicit_marker_still_wins():
    d = classify_background("summarize this in background")
    assert d.background is True and d.reason == "explicit bg marker"


def test_bgmode_off_kills_everything():
    d = classify_background("implement the login endpoint with tests", enabled=False)
    assert d.background is False


def test_pick_agent_routing():
    assert _pick_agent("research competitors deeply", "") == "researcher"
    assert _pick_agent("fix the login bug", "fix") == "coder"
    assert _pick_agent("draft the quarterly report", "draft") == "analyst"

"""Voice taste memory: corrections stick, corrupt files never break speech.

Covers voice/prefs.py: address modes, brevity tastes, persistence round
trip, the 10-word hijack guard, and the corrupt-store contract."""

from __future__ import annotations

from voice.prefs import VoicePrefs


def test_sir_off_sticks(tmp_path):
    p = VoicePrefs.load(tmp_path / "vp.json")
    assert p.address == "sir"
    ack = p.note_correction("don't call me sir")
    assert ack and "sir" in ack.lower()
    assert p.address == "none"
    # persisted: a fresh load sees it
    assert VoicePrefs.load(tmp_path / "vp.json").address == "none"


def test_call_me_name(tmp_path):
    p = VoicePrefs.load(tmp_path / "vp.json")
    ack = p.note_correction("call me krithik")
    assert ack and "krithik" in ack.lower()
    assert p.address == "krithik"


def test_brevity_tastes(tmp_path):
    p = VoicePrefs.load(tmp_path / "vp.json")
    assert p.note_correction("too long") is not None
    assert p.brevity == "brief"
    assert p.note_correction("read everything") is not None
    assert p.brevity == "full"
    assert p.note_correction("back to normal") is not None
    assert p.brevity == "auto"


def test_real_question_never_hijacked(tmp_path):
    p = VoicePrefs.load(tmp_path / "vp.json")
    long_q = "why is my deployment taking too long to finish the migration step"
    assert p.note_correction(long_q) is None
    assert p.brevity == "auto"
    assert p.note_correction("is that too long for a variable name in python code") is None
    assert p.brevity == "auto"


def test_unrelated_chatter_ignored(tmp_path):
    p = VoicePrefs.load(tmp_path / "vp.json")
    assert p.note_correction("what time is it") is None
    assert p.note_correction("") is None
    assert p.note_correction(None) is None


def test_corrupt_store_starts_fresh(tmp_path):
    bad = tmp_path / "vp.json"
    bad.write_text("{not json", encoding="utf-8")
    p = VoicePrefs.load(bad)
    assert p.address == "sir" and p.brevity == "auto"
    assert p.note_correction("too long") is not None  # still works


def test_config_seed_used_when_no_file(tmp_path):
    p = VoicePrefs.load(tmp_path / "fresh.json", address_seed="none")
    assert p.address == "none"

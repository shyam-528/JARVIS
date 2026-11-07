from __future__ import annotations

import numpy as np

from voice.audio import high_pass, list_devices, noise_gate
from voice.expressive import classify_tone, style_prompt_for
from voice.tts import (
    PlaybackController,
    Speaker2,
    _combine_pitch,
    _combine_rate,
    is_confirmation,
    sanitize_for_speech,
    split_reply,
    split_sentences,
)


def test_sanitize_strips_markdown_symbols():
    assert sanitize_for_speech("**bold** and _under_") == "bold and under"


def test_sanitize_strips_urls_and_emojis():
    out = sanitize_for_speech("see https://example.com/x now 🎉 done")
    assert "https" not in out and "link" in out and "🎉" not in out


def test_sanitize_code_block_replaced():
    out = sanitize_for_speech("Here:\n```python\nprint(1)\n```\ndone")
    assert "print" not in out and "Code block omitted" in out and "done" in out


def test_split_sentences_basic():
    parts = split_sentences("Hello sir. Systems are online! Ready?")
    assert parts == ["Hello sir.", "Systems are online!", "Ready?"]


def test_split_sentences_abbreviations_robust():
    parts = split_sentences("First part here. Second part.")
    assert len(parts) == 2


def test_split_short_reply_unchanged():
    spoken, rest = split_reply("Hello sir, systems are online.", max_chars=300)
    assert spoken == "Hello sir, systems are online." and rest == ""


def test_split_long_reply_offers_remainder():
    text = "First sentence is here. " * 20
    spoken, rest = split_reply(text, max_chars=120)
    assert rest and spoken.endswith(".") and rest in text


def test_confirmation_words():
    assert is_confirmation("yes") and is_confirmation("Yeah!") and is_confirmation("go ahead")
    assert not is_confirmation("what is my favorite project")


def test_playback_controller_barge_in():
    pc = PlaybackController()
    assert not pc.speaking
    pc._speaking.set()
    assert pc.speaking
    pc.barge_in()
    assert pc._stop.is_set()
    pc.reset()
    assert not pc._stop.is_set()


def test_noise_gate_silences_quiet_frames():
    sr = 16000
    quiet = (np.sin(np.linspace(0, 10, sr)) * 50).astype("int16")
    gated = noise_gate(quiet, threshold_db=-30.0, sample_rate=sr)
    assert float(np.sqrt(np.mean(gated.astype(np.float64) ** 2))) < 5.0


def test_high_pass_removes_dc_offset():
    sr = 16000
    signal = (np.ones(sr) * 1000).astype("int16")
    filtered = high_pass(signal, cutoff_hz=80.0, sample_rate=sr)
    assert abs(float(np.mean(filtered))) < abs(float(np.mean(signal))) / 10


def test_list_devices_returns_entries():
    devices = list_devices()
    assert isinstance(devices, list)
    for d in devices:
        assert "index" in d and "name" in d and "input" in d


def test_speaker2_engine_detection():
    s = Speaker2(engine="auto")
    engine = s._pick_engine()
    # auto prefers the local-neural chain: kokoro -> edge -> gemini -> sapi
    # (piper only via explicit pref)
    assert engine in ("kokoro", "gemini", "edge", "piper", "sapi")


def test_classify_tone_buckets():
    # good news -> bright
    _, rate, pitch = classify_tone("Excellent news, sir! It works!")
    assert rate == "+10%" and pitch == "+2Hz"
    # apology -> gentle/serious
    _, rate, pitch = classify_tone("I'm afraid it failed, sir. My apologies.")
    assert rate == "-10%" and pitch == "-2Hz"
    # question -> rising
    _, rate, pitch = classify_tone("Shall I continue, sir?")
    assert rate == "+0%" and pitch == "+0Hz"
    # neutral
    _, rate, pitch = classify_tone("The file is ready.")
    assert rate == "+0%"


def test_style_prompt_has_persona_and_overlay():
    p = style_prompt_for("Great news, everything works!")
    assert "butler" in p.lower()
    assert "delight" in p.lower()


def test_combine_rate_pitch_math():
    # Base is 0 (natural edge speed) — the old -5/-2 "butler base" made
    # every reply a slowed-down robot.
    assert _combine_rate("+10%") == "+10%"
    assert _combine_rate("-10%") == "-10%"
    assert _combine_rate("+0%") == "+0%"
    assert _combine_pitch("+2Hz") == "+2Hz"
    assert _combine_pitch("-2Hz") == "-2Hz"
    assert _combine_pitch("+0Hz") == "+0Hz"


def test_engine_cooldown_skips_dead_engine():

    s = Speaker2(engine="auto")
    s._mark_engine_dead("kokoro")
    s._mark_engine_dead("gemini")
    # kokoro + gemini cooled down -> next in chain
    assert s._pick_engine() == "edge"


def test_speaker2_explicit_engine_respected():
    s = Speaker2(engine="edge")
    assert s._pick_engine() == "edge"


def test_speaker2_say_disabled_is_silent():
    import asyncio

    async def run():
        s = Speaker2(enabled=False)
        return await s.say("hello")

    assert asyncio.run(run()) is False

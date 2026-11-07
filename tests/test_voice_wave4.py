"""Wave 4 failure-path tests for the voice stack.

Covers the pure decision/transform cores that were previously untested:
barge-in trigger truth table, STT resample/join, TTS text splitting,
Gemini response->WAV conversion, voice-command matching + volume failure
visibility, and the DSP cleanup pipeline.
"""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace

import numpy as np

from voice.audio import clean_audio
from voice.expressive import _response_to_wav_bytes, split_for_tts
from voice.mic import BargeInMonitor
from voice.stt import Transcriber

# ---------- mic.BargeInMonitor._decide (truth table) ----------


def _monitor() -> BargeInMonitor:
    mon = BargeInMonitor.__new__(BargeInMonitor)  # no hardware
    mon.trigger_ms = 400
    mon.energy_margin = 2.2
    return mon


def test_decide_requires_settled():
    mon = _monitor()
    assert mon._decide(5000, energy=999.0, floor=1.0, settled=False) is False


def test_decide_requires_min_run():
    mon = _monitor()
    assert mon._decide(399, energy=999.0, floor=1.0, settled=True) is False
    assert mon._decide(400, energy=999.0, floor=1.0, settled=True) is True


def test_decide_energy_margin_boundary():
    mon = _monitor()
    floor = 100.0
    # strictly greater than floor * margin required
    assert mon._decide(400, energy=floor * mon.energy_margin, floor=floor, settled=True) is False
    assert mon._decide(400, energy=floor * mon.energy_margin + 1, floor=floor, settled=True) is True


# ---------- stt.Transcriber ----------


class _FakeModel:
    def __init__(self):
        self.seen_audio = None

    def transcribe(self, audio, language=None, beam_size=1, vad_filter=False):
        self.seen_audio = audio
        return [SimpleNamespace(text=" hello"), SimpleNamespace(text="sir ")], None


def _transcriber_with_fake(monkeypatch):
    t = Transcriber()
    fake = _FakeModel()
    monkeypatch.setattr(t, "_load", lambda: fake)
    return t, fake


def test_transcriber_empty_input_returns_empty():
    assert Transcriber().transcribe(np.zeros(0, dtype=np.int16)) == ""


def test_transcriber_joins_segments(monkeypatch):
    t, _ = _transcriber_with_fake(monkeypatch)
    assert t.transcribe(np.zeros(1600, dtype=np.int16)) == "hello sir"


def test_transcriber_resamples_to_16k(monkeypatch):
    t, fake = _transcriber_with_fake(monkeypatch)
    pcm = np.zeros(1600, dtype=np.int16)  # 200 ms at 8 kHz
    t.transcribe(pcm, samplerate=8000)
    assert len(fake.seen_audio) == 3200  # upsampled 2x to 16 kHz
    assert fake.seen_audio.dtype == np.float32


# ---------- expressive.split_for_tts ----------


def test_split_for_tts_empty():
    assert split_for_tts("") == []
    assert split_for_tts("   ") == []


def test_split_for_tts_first_segment_kept_short():
    first = (
        "Sir, the first item is done, the second is pending, "
        "the third needs review, the fourth is queued, "
        "and the fifth is waiting for parts"
    )  # > 110 chars, has delimiters
    text = first + ". Second sentence here. Third sentence follows."
    segs = split_for_tts(text)
    assert len(segs[0]) <= 110
    joined = " ".join(segs)
    for w in ("first", "second", "third", "fourth", "fifth", "Second", "Third"):
        assert w in joined


def test_split_for_tts_groups_under_budget():
    text = " ".join(["Short one.", "Short two.", "Short three."] * 10)
    segs = split_for_tts(text, max_chars=100, first_max_chars=50)
    assert len(segs) > 1
    assert all(len(s) <= 100 for s in segs)


# ---------- expressive._response_to_wav_bytes ----------


def _fake_gemini_response(data, mime="audio/L16;rate=16000"):
    inline = SimpleNamespace(data=data, mime_type=mime)
    part = SimpleNamespace(inline_data=inline)
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def test_response_to_wav_riff_passthrough():
    blob = b"RIFF" + b"\x00" * 100
    assert _response_to_wav_bytes(_fake_gemini_response(blob)) == blob


def test_response_to_wav_wraps_raw_pcm():
    pcm = b"\x01\x02" * 800
    out = _response_to_wav_bytes(_fake_gemini_response(pcm, "audio/L16;rate=16000"))
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.readframes(800) == pcm


def test_response_to_wav_strips_audio_header():
    pcm = b"\x00\x00" * 100
    raw = b"RATES=16000\r\n some header\r\nAUDIO=" + pcm
    out = _response_to_wav_bytes(_fake_gemini_response(raw, "audio/L16;rate=8000"))
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == 8000
        assert w.readframes(100) == pcm


def test_response_to_wav_none_when_no_audio():
    assert _response_to_wav_bytes(SimpleNamespace(candidates=[])) is None
    assert _response_to_wav_bytes(_fake_gemini_response(None)) is None


# ---------- session voice commands ----------


def test_match_voice_command_normalizes_and_maps():
    from voice.session import VOICE_COMMANDS, VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    for phrase, cmd in VOICE_COMMANDS.items():
        assert vs._match_voice_command(phrase.upper() + "!") == cmd
    assert vs._match_voice_command("open chrome for me") is None


def test_volume_command_failure_is_visible():
    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)

    class FailingBox:
        async def execute(self, *a, **k):
            raise RuntimeError("no audio ctl")

    printed = []

    async def fake_say(text):
        return True

    vs.orch = SimpleNamespace(toolbox=FailingBox())
    vs.speaker = SimpleNamespace(say=fake_say)
    vs.console = SimpleNamespace(print=lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    ok = asyncio.run(vs._handle_voice_command("volume_up"))
    assert ok is True
    assert any("volume command failed" in p for p in printed)


# ---------- audio DSP pipeline ----------


def test_clean_audio_removes_dc_and_gates_quiet():
    dc = np.full(4800, 200, dtype=np.int16)  # constant hum offset
    out = clean_audio(dc)
    assert out.dtype == np.int16
    assert np.max(np.abs(out)) == 0  # high-pass kills DC, gate zeroes the rest


def test_clean_audio_loud_tone_survives():
    t = np.arange(16000, dtype=np.float32) / 16000.0
    tone = (np.sin(2 * np.pi * 200 * t) * 12000).astype(np.int16)
    out = clean_audio(tone)
    assert out.dtype == np.int16
    assert np.max(np.abs(out)) > 1000  # loud 200 Hz tone passes through

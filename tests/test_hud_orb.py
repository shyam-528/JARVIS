"""Tests for the orb HUD's headless logic (state machine, envelope, render)."""

from __future__ import annotations

import struct

from hud_orb import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
    OrbStateMachine,
    render_orb,
    rms_envelope,
)


class TestStateMachine:
    def test_happy_path(self):
        sm = OrbStateMachine()
        assert sm.state == STATE_IDLE
        assert sm.go(STATE_LISTENING)
        assert sm.go(STATE_THINKING)
        assert sm.go(STATE_SPEAKING)
        assert sm.go(STATE_IDLE)

    def test_illegal_transitions_rejected(self):
        sm = OrbStateMachine()
        assert not sm.go(STATE_SPEAKING)  # idle -> speaking: no
        assert not sm.go(STATE_THINKING)  # idle -> thinking: no
        assert sm.state == STATE_IDLE

    def test_error_recovers_to_idle(self):
        sm = OrbStateMachine()
        sm.go(STATE_LISTENING)
        assert sm.go(STATE_ERROR)
        assert sm.go(STATE_IDLE)

    def test_speaking_only_returns_to_idle(self):
        sm = OrbStateMachine()
        sm.go(STATE_LISTENING)
        sm.go(STATE_THINKING)
        sm.go(STATE_SPEAKING)
        assert not sm.go(STATE_LISTENING)
        assert sm.go(STATE_IDLE)


class TestRmsEnvelope:
    def _pcm16(self, samples):
        return struct.pack(f"<{len(samples)}h", *samples)

    def test_loud_and_quiet(self):
        loud = [16000] * 1600 + [100] * 1600  # 100ms loud, 100ms quiet @16k
        env = rms_envelope(self._pcm16(loud), 16000, 1, frame_ms=33)
        assert len(env) >= 5
        assert env[0] > env[-1]  # loud start > quiet tail

    def test_normalized_to_one(self):
        env = rms_envelope(self._pcm16([8000] * 3200), 16000, 1)
        assert max(env) <= 1.0
        assert max(env) > 0.9

    def test_stereo_downmix(self):
        # interleaved stereo: L loud, R silent
        interleaved = []
        for _ in range(1600):
            interleaved.extend([12000, 0])
        env = rms_envelope(self._pcm16(interleaved), 16000, 2)
        assert env and max(env) > 0.5

    def test_empty_audio(self):
        assert rms_envelope(b"", 16000, 1) == []


class TestOrbRender:
    def test_render_produces_image(self):
        img = render_orb(120, [(99, 102, 241), (165, 180, 252), (49, 46, 129)])
        assert img.size == (120, 120)
        # center pixel is the bright core dot
        px = img.getpixel((60, 60))
        assert px[3] == 255

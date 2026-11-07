"""Microphone capture: VAD-gated recording + barge-in monitoring.

Contract: record_vad records until sustained silence (webrtcvad when
installed, energy-gate fallback otherwise) and returns cleaned int16
PCM. mic_stream is a raw-stream context manager for live monitors.

BargeInMonitor is a daemon thread that watches the mic during playback
and fires on_barge on real user speech. It is fail-silent BY DESIGN: a
monitor that cannot open the mic simply never triggers and playback is
unaffected, so _run swallows hardware errors instead of destabilising
the speaker path. Trigger decisions live in _decide (pure function,
unit-tested); on_barge runs in a thread and its errors are contained.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable

import numpy as np


def record_vad(
    silence_ms: int = 900,
    max_s: float = 15.0,
    samplerate: int = 16000,
    aggressiveness: int = 1,  # quieter laptop mics: mode 2 eats speech onsets
    pre_roll_ms: int = 500,  # capture the first syllable, not from mid-word
    noise_db: float = -50.0,  # gentler gate — quiet speech edges survive
    on_level: Callable[[str], None] | None = None,
    input_device: int | None = None,
    use_vad: bool = True,
    fallback_threshold: float = 300.0,
) -> np.ndarray:
    """Record until VAD detects sustained silence. Falls back to energy gate if webrtcvad unavailable.

    Phase 3: input_device defaults to $JARVIS_MIC_INDEX when set (sticky mic
    selection without code edits). Silence/noise stay tunable via config.
    """
    import os as _os

    if input_device is None:
        try:
            input_device = int(_os.getenv("JARVIS_MIC_INDEX", ""))
        except ValueError:
            input_device = None
    try:
        import webrtcvad

        vad = webrtcvad.Vad(aggressiveness)
        use_vad = use_vad and True
    except ImportError:
        vad = None
        use_vad = False

    import sounddevice as sd

    frame_ms = 30
    frame_len = samplerate * frame_ms // 1000
    max_frames = int(max_s * 1000 / frame_ms)
    silence_limit = max(1, silence_ms // frame_ms)
    pre_roll = max(1, pre_roll_ms // frame_ms)

    frames: list[np.ndarray] = []
    ring: list[np.ndarray] = []
    trailing_silence = 0
    heard_speech = False

    with sd.InputStream(
        samplerate=samplerate,
        channels=1,
        dtype="int16",
        blocksize=frame_len,
        device=input_device,
    ) as stream:
        for _ in range(max_frames):
            data, _overflow = stream.read(frame_len)
            chunk = data[:, 0].copy()

            if use_vad and vad is not None:
                is_speech = vad.is_speech(chunk.tobytes(), samplerate)
            else:
                rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
                is_speech = rms > fallback_threshold

            if on_level:
                on_level("speech" if is_speech else "silence")

            if is_speech:
                heard_speech = True
                trailing_silence = 0
                if ring and not frames:
                    frames.extend(ring[-pre_roll:])
                    ring.clear()
                frames.append(chunk)
            elif heard_speech:
                trailing_silence += 1
                frames.append(chunk)
                if trailing_silence >= silence_limit:
                    break
            else:
                ring.append(chunk)
                if len(ring) > pre_roll:
                    ring.pop(0)

    if not frames:
        return np.zeros(0, dtype="int16")
    audio = np.concatenate(frames).astype("int16")

    from voice.audio import clean_audio

    return clean_audio(audio, samplerate, noise_db)


class BargeInMonitor:
    """Daemon thread: VAD/energy watch during playback -> on_barge callback."""

    def __init__(
        self,
        on_barge: Callable[[], None],
        should_stop: Callable[[], bool],
        trigger_ms: int = 400,
        settle_ms: int = 600,
        energy_margin: float = 2.2,
        samplerate: int = 16000,
        input_device: int | None = None,
    ):
        self.on_barge = on_barge
        self.should_stop = should_stop
        self.trigger_ms = trigger_ms
        self.settle_ms = settle_ms
        self.energy_margin = energy_margin
        self.samplerate = samplerate
        self.input_device = input_device
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.triggered = False  # diagnostics/tests

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="barge-in", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---------- decision logic (pure, unit-tested) ----------

    def _decide(self, speech_run_ms: int, energy: float, floor: float, settled: bool) -> bool:
        if not settled:
            return False
        if speech_run_ms < self.trigger_ms:
            return False
        return energy > floor * self.energy_margin

    # ---------- internals ----------

    def _run(self) -> None:
        try:
            import webrtcvad

            vad = webrtcvad.Vad(2)
        except ImportError:
            vad = None
        try:
            import sounddevice as sd
        except Exception:
            return

        frame_ms = 30
        frame_len = self.samplerate * frame_ms // 1000
        started = time.monotonic()
        speech_run = 0
        floor = 300.0  # adapts from early frames (room + speaker bleed)
        floor_samples: list[float] = []

        try:
            with sd.InputStream(
                samplerate=self.samplerate,
                channels=1,
                dtype="int16",
                blocksize=frame_len,
                device=self.input_device,
            ) as stream:
                while not (self._stop.is_set() or self.should_stop()):
                    try:
                        data, _ = stream.read(frame_len)
                    except Exception:
                        break
                    chunk = data[:, 0].copy()
                    rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
                    settled = (time.monotonic() - started) * 1000 >= self.settle_ms

                    if not settled:
                        floor_samples.append(rms)
                        if len(floor_samples) >= 10:
                            floor = max(300.0, float(np.percentile(floor_samples, 75)))
                            floor_samples = floor_samples[-10:]

                    if vad is not None:
                        is_speech = vad.is_speech(chunk.tobytes(), self.samplerate)
                    else:
                        is_speech = rms > floor * self.energy_margin

                    speech_run = speech_run + frame_ms if is_speech else 0
                    if self._decide(speech_run, rms, floor, settled):
                        self.triggered = True
                        with contextlib.suppress(Exception):
                            self.on_barge()
                        return
        except Exception:
            return

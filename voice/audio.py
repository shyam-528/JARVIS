"""Audio device discovery + lightweight DSP cleanup for mic input.

Contract: list_devices/find_*_device import sounddevice lazily and
raise ImportError if it is absent -- device listing is meaningless
without audio hardware, so absence is loud by design. The DSP helpers
(rms_db, noise_gate, high_pass, clean_audio) are pure numpy: same input,
same output, no device access; they are the unit-tested core.
"""

from __future__ import annotations

import numpy as np


def list_devices() -> list[dict]:
    import sounddevice as sd

    devices = []
    defaults_in, defaults_out = sd.default.device
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 or d["max_output_channels"] > 0:
            devices.append(
                {
                    "index": i,
                    "name": d["name"],
                    "input": d["max_input_channels"] > 0,
                    "output": d["max_output_channels"] > 0,
                    "default_in": i == defaults_in,
                    "default_out": i == defaults_out,
                    "samplerate": d["default_samplerate"],
                }
            )
    return devices


def find_input_device(name_hint: str = "") -> int | None:
    for d in list_devices():
        if d["input"] and (not name_hint or name_hint.lower() in d["name"].lower()):
            return d["index"]
    return None


def find_output_device(name_hint: str = "") -> int | None:
    for d in list_devices():
        if d["output"] and (not name_hint or name_hint.lower() in d["name"].lower()):
            return d["index"]
    return None


def noise_gate(
    audio: np.ndarray, threshold_db: float = -45.0, sample_rate: int = 16000
) -> np.ndarray:
    """Spectral-gate noise: zero out frames below threshold."""
    if audio.size == 0:
        return audio
    audio = audio.astype(np.float32) / 32768.0
    frame = 480
    out = np.zeros_like(audio)
    for start in range(0, audio.size - frame + 1, frame):
        chunk = audio[start : start + frame]
        rms_db = 20.0 * np.log10(max(float(np.sqrt(np.mean(chunk**2))), 1e-10))
        if rms_db > threshold_db:
            out[start : start + frame] = chunk
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype(np.int16)


def high_pass(audio: np.ndarray, cutoff_hz: float = 80.0, sample_rate: int = 16000) -> np.ndarray:
    """Simple first-order high-pass to remove hum/rumble."""
    if audio.size == 0:
        return audio
    rc = 1.0 / (2 * 3.14159265 * cutoff_hz)
    dt = 1.0 / sample_rate
    alpha = rc / (rc + dt)
    x = audio.astype(np.float32)
    y = np.zeros_like(x)
    prev_x = x[0]
    prev_y = x[0]
    for i in range(x.size):
        prev_y = alpha * (prev_y + x[i] - prev_x)
        prev_x = x[i]
        y[i] = prev_y
    return y.astype(np.int16)


def clean_audio(audio: np.ndarray, sample_rate: int = 16000, noise_db: float = -45.0) -> np.ndarray:
    return noise_gate(high_pass(audio, sample_rate=sample_rate), noise_db, sample_rate)

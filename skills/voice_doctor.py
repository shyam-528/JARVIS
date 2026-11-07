"""Voice doctor: guided audio self-check, no hardware assumptions.

Checks imports, mic/output devices, Kokoro synth (bytes only, no speakers
needed), wake-word models. Returns a report card. Nothing throws.
"""

from __future__ import annotations

import contextlib

from core.tools import Tool, ToolBox


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


async def _run_checks() -> list[str]:
    lines = []

    def _ok(c: bool) -> str:
        return "✅" if c else "❌"

    mods = {
        m: _have(m)
        for m in (
            "sounddevice",
            "faster_whisper",
            "edge_tts",
            "kokoro_onnx",
            "openwakeword",
            "miniaudio",
            "webrtcvad",
        )
    }
    lines.append("Engines: " + ", ".join(f"{m} {_ok(v)}" for m, v in mods.items()))
    # Devices (needs sounddevice only)
    try:
        import sounddevice as sd

        devs = sd.query_devices()
        ins = sum(1 for d in devs if d.get("max_input_channels", 0) > 0)
        outs = sum(1 for d in devs if d.get("max_output_channels", 0) > 0)
        lines.append(f"Devices: {len(devs)} total ({ins} mic, {outs} speakers).")
        with contextlib.suppress(Exception):
            lines.append(
                f"Default output: {sd.default.device[1]}. "
                f"Mic override: JARVIS_MIC_INDEX env or pick from /audio."
            )
    except Exception as exc:
        lines.append(f"Devices: unavailable ({exc}). Voice output will fail.")
    # Configured chain (what SHOULD speak)
    try:
        from core.config import load_config

        _v = load_config().voice
        lines.append(
            f"Config: enabled={_v.tts_enabled} engine={_v.tts_engine} "
            f"edge_voice={_v.tts_voice} distill={_v.distill_speech}/{_v.distill_threshold}."
        )
    except Exception as exc:
        lines.append(f"Config: unreadable ({str(exc)[:80]})")
    # Kokoro synth (proves voice path without speakers)
    if mods["kokoro_onnx"]:
        try:
            import time as _t

            from voice.tts import Speaker2

            s = Speaker2()
            t0 = _t.monotonic()
            pcm, sr = await s.synth_kokoro("Doctor check.", speed=1.0)
            dt = _t.monotonic() - t0
            secs = len(pcm) / 2 / sr if sr else 0
            lines.append(
                f"Kokoro ({s.kokoro_voice}): {len(pcm)} bytes, "
                f"{secs:.1f}s audio in {dt:.1f}s. {'✅' if secs > 0.3 else '⚠️ short'}"
            )
        except Exception as exc:
            lines.append(f"Kokoro synth failed: {str(exc)[:120]}")
    else:
        lines.append("Kokoro: not installed (pip install kokoro-onnx).")
    # Edge synth (web fallback + terminal chain link)
    if mods["edge_tts"]:
        try:
            import time as _t2

            from voice.tts import Speaker2 as _Spk

            _s = _Spk()
            _t0 = _t2.monotonic()
            _mp3 = await _s.synth_edge("Doctor check.")
            _dt = _t2.monotonic() - _t0
            lines.append(
                f"Edge ({_s.edge_voice}): {len(_mp3)} bytes in {_dt:.1f}s. "
                f"{'✅' if len(_mp3) > 1000 else '⚠️ short'}"
            )
        except Exception as exc:
            lines.append(f"Edge synth failed: {str(exc)[:120]}")
    else:
        lines.append("Edge: not installed (pip install edge-tts).")
    # Wake-word models
    try:
        from voice import wake as _w  # noqa: F401

        lines.append("Wake-word module: ✅ (say the word to test live with /voice)")
    except Exception as exc:
        lines.append(f"Wake-word module: ❌ ({str(exc)[:100]})")
    return lines


def register_voice_doctor(box: ToolBox):
    async def voice_doctor() -> str:
        try:
            return "Voice doctor:\n" + "\n".join(await _run_checks())
        except Exception as exc:
            return f"doctor failed: {exc}"

    box.register(
        Tool(
            name="voice_doctor",
            description="Self-check audio: engines, mics, Kokoro synth, wake-word. No hardware needed except listing.",
            parameters={"type": "object", "properties": {}},
            handler=voice_doctor,
        )
    )

"""Text-to-speech: engine cascade + playback + speech-shaping helpers.

Contract: Speaker2.speak_chunk(text) -> bool is the entry point. Engine
policy: edge-tts first (fast, streams to ffplay), then gemini
(expressive, needs a key), piper (local ONNX), kokoro (explicit-pin
only: too slow on CPU to auto-select), and SAPI as the always-available
Windows last resort. A failed engine cools down for 5 minutes so repeat
utterances fall back fast instead of re-timing-out every turn.

Failure policy: failures are never silent. Speaker2.last_error carries
the last failure in plain words and status() exposes it to chat/doctor.

Also home to the pure text helpers shared by the voice stack:
sanitize_for_speech, split_sentences, split_reply, is_confirmation,
number_to_words.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

YES_WORDS = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "go",
    "go ahead",
    "continue",
    "read it",
    "read the rest",
    "please do",
    "do it",
    "go on",
    "yes please",
    "carry on",
    "proceed",
    "affirmative",
}

OFFER_LINE = " I have the full detail ready, sir. Shall I continue?"

_SYMBOLS_RE = re.compile(r"[*_#`~>|^{}\[\]<>=+\\]")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def sanitize_for_speech(text: str) -> str:
    text = _CODE_RE.sub(" Code block omitted. ", text)
    text = _URL_RE.sub(" link ", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = "".join(ch for ch in text if not (unicodedata.category(ch) or "").startswith("So"))
    text = _SYMBOLS_RE.sub(" ", text)
    text = re.sub(r"(?<=\w)@(?=\w)", " at ", text)
    text = re.sub(r"@(?=\w)", " ", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


def split_reply(text: str, max_chars: int = 300) -> tuple[str, str]:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text, ""
    sentences = split_sentences(text)
    first: list[str] = []
    length = 0
    taken = 0
    for part in sentences:
        if first and length + len(part) + 1 > max_chars:
            break
        first.append(part)
        length += len(part) + 1
        taken += 1
    if taken == len(sentences):
        return text, ""
    spoken = " ".join(first)
    if not spoken.rstrip().endswith((".", "!", "?")):
        spoken = spoken.rstrip(".!,? ") + "."
    return spoken, " ".join(sentences[taken:])


def is_confirmation(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    if t in YES_WORDS:
        return True
    return (
        any(t.startswith(w) for w in ("yes", "yeah", "sure", "go ahead", "continue", "read"))
        and len(t) <= 40
    )


def sapi_fallback(text: str) -> bool:
    # Here-string $@'...'@$ breaks on embedded "'@" / "@'". Neutralize
    # quotes (spoken identically) so arbitrary LLM text can't break/inject.
    safe = text.replace("'", "\u2019")
    try:
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            f"$s.Speak(@'\n{safe}\n'@);"
        )
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", enc],
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def _kokoro_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "kokoro"


# Process-wide shared Kokoro session. The 325MB model must load ONCE:
# the streaming TTS endpoint serves one HTTP request per sentence, and a
# per-request Speaker2 reloaded the model every time (~1.4s wasted per
# sentence on top of synthesis). InferenceSession.run is thread-safe, so
# concurrent requests can share it.
_SHARED_KOKORO: dict = {"sess": None, "kokoro": None}
_SHARED_KOKORO_LOCK: asyncio.Lock | None = None


def _shared_lock() -> asyncio.Lock:
    global _SHARED_KOKORO_LOCK
    if _SHARED_KOKORO_LOCK is None:
        _SHARED_KOKORO_LOCK = asyncio.Lock()
    return _SHARED_KOKORO_LOCK


async def get_shared_kokoro():
    """Return the process-wide (session, Kokoro), loading the model once.

    Returns (None, None) when kokoro is unavailable (no package/files).
    """
    if _SHARED_KOKORO["kokoro"] is not None:
        return _SHARED_KOKORO["sess"], _SHARED_KOKORO["kokoro"]
    async with _shared_lock():
        if _SHARED_KOKORO["kokoro"] is not None:
            return _SHARED_KOKORO["sess"], _SHARED_KOKORO["kokoro"]
        try:
            import onnxruntime as _ort
            from kokoro_onnx import Kokoro
        except ImportError:
            return None, None
        kdir = _kokoro_dir()
        if not ((kdir / "kokoro-v1.0.onnx").exists() and (kdir / "voices-v1.0.bin").exists()):
            return None, None
        loop = asyncio.get_running_loop()

        def _load():
            import os as _os

            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = _os.cpu_count() or 4
            _opts.inter_op_num_threads = 1
            _sess = _ort.InferenceSession(
                str(kdir / "kokoro-v1.0.onnx"),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            return _sess, Kokoro.from_session(_sess, str(kdir / "voices-v1.0.bin"))

        try:
            _sess, _k = await loop.run_in_executor(None, _load)
        except Exception:
            return None, None
        _SHARED_KOKORO.update(sess=_sess, kokoro=_k)
        return _sess, _k


async def synth_kokoro_shared(text: str, voice: str = "", speed: float | None = None):
    """Synthesize with the shared Kokoro session. Returns (pcm16, rate)
    or None on any failure (caller falls through to the next engine)."""
    import os as _os

    pair = await get_shared_kokoro()
    if pair[0] is None:
        return None
    _, k = pair
    v = voice or _os.getenv("JARVIS_KOKORO_VOICE", "bf_emma")
    try:
        sp = speed if speed is not None else float(_os.getenv("JARVIS_KOKORO_SPEED", "1.0"))
    except ValueError:
        sp = 1.0
    loop = asyncio.get_running_loop()

    def _synth():
        import numpy as _np

        audio, sr = k.create(text, voice=v, speed=sp)
        pcm = (_np.clip(audio, -1.0, 1.0) * 32767).astype("int16")
        return pcm.tobytes(), int(sr)

    try:
        return await loop.run_in_executor(None, _synth)
    except Exception:
        return None


class PlaybackController:
    """Controls TTS playback with barge-in support."""

    def __init__(self):
        self._stop = asyncio.Event()
        self._speaking = asyncio.Event()

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def barge_in(self):
        """User spoke — stop playback immediately."""
        self._stop.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass

    def reset(self):
        self._stop.clear()

    async def wait_if_speaking(self):
        while self._speaking.is_set():
            await asyncio.sleep(0.05)


def _combine_rate(adj: str, base: int = 0) -> str:
    """Combine the tone adjustment (e.g. '+10%') onto the base rate.

    Base is 0 (natural edge speed) — a slowed baseline made every reply sound
    like a dragging robot, which the user found unbearable.
    """
    delta = 0
    if adj.endswith("%"):
        try:
            delta = int(adj[:-1])
        except ValueError:
            delta = 0
    return f"{base + delta:+d}%"


def _combine_pitch(adj: str, base: int = 0) -> str:
    """Combine the tone adjustment (e.g. '+2Hz') onto the base pitch (natural)."""
    delta = 0
    if adj.endswith("Hz"):
        try:
            delta = int(adj[:-2])
        except ValueError:
            delta = 0
    return f"{base + delta:+d}Hz"


class Speaker2:
    """Voice 3.0 speaker: Kokoro (local neural, human) → edge-tts (prosody)
    → Gemini (expressive) → SAPI, with streaming + barge-in."""

    MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "piper_voices"
    KOKORO_DIR = Path(__file__).resolve().parent.parent / "data" / "kokoro"

    def __init__(
        self,
        voice: str = "en_GB-alan-medium",
        edge_voice: str = "en-GB-RyanNeural",
        enabled: bool = True,
        engine: str = "auto",
        playback: PlaybackController | None = None,
        persona: str = "",
        gemini_voice: str = "Kore",
        kokoro_voice: str = "bf_emma",
        kokoro_speed: float = 1.0,
    ):
        self.voice = voice
        self.edge_voice = edge_voice
        self.enabled = enabled
        self.engine_pref = engine
        self.playback = playback or PlaybackController()
        self._piper_voice = None
        self._kokoro = None
        self._engine_used = ""
        self.persona = persona
        self.gemini_voice = gemini_voice
        import os as _os

        self.kokoro_voice = _os.getenv("JARVIS_KOKORO_VOICE", kokoro_voice)
        try:
            self.kokoro_speed = float(_os.getenv("JARVIS_KOKORO_SPEED", str(kokoro_speed)))
        except ValueError:
            self.kokoro_speed = 1.0
        # Remember engines that failed recently so we don't retry them
        # every single utterance (e.g. no network -> skip Gemini fast).
        self._engine_dead: dict[str, float] = {}
        self._ENGINE_COOLDOWN_S = 300.0
        # Last failure in plain words — surfaced by chat/doctor so a dead
        # voice chain is never a silent mystery.
        self.last_error: str = ""

    def _engine_ok(self, name: str) -> bool:
        import time as _t

        until = self._engine_dead.get(name, 0.0)
        return _t.time() >= until

    def _mark_engine_dead(self, name: str) -> None:
        import time as _t

        self._engine_dead[name] = _t.time() + self._ENGINE_COOLDOWN_S

    def _piper_available(self) -> bool:
        try:
            from piper import PiperVoice  # noqa: F401

            return (self.MODEL_DIR / f"{self.voice}.onnx").exists()
        except ImportError:
            return False

    def _get_piper(self):
        if self._piper_voice is None:
            from piper import PiperVoice

            self._piper_voice = PiperVoice.load(str(self.MODEL_DIR / f"{self.voice}.onnx"))
        return self._piper_voice

    def _kokoro_available(self) -> bool:
        try:
            import kokoro_onnx  # noqa: F401

            return (self.KOKORO_DIR / "kokoro-v1.0.onnx").exists() and (
                self.KOKORO_DIR / "voices-v1.0.bin"
            ).exists()
        except ImportError:
            return False

    def _get_kokoro(self):
        if self._kokoro is None:
            # Reuse the process-wide session when already loaded (no 1.4s reload).
            if _SHARED_KOKORO["kokoro"] is not None:
                self._kokoro = _SHARED_KOKORO["kokoro"]
                return self._kokoro
            import os as _os

            import onnxruntime as _ort
            from kokoro_onnx import Kokoro

            # Default ORT session is single-threaded (6s+ per reply on
            # 12-core box). Pin intra-op threads to all CPUs: ~0.8s.
            _opts = _ort.SessionOptions()
            _opts.intra_op_num_threads = _os.cpu_count() or 4
            _opts.inter_op_num_threads = 1
            _sess = _ort.InferenceSession(
                str(self.KOKORO_DIR / "kokoro-v1.0.onnx"),
                sess_options=_opts,
                providers=["CPUExecutionProvider"],
            )
            self._kokoro = Kokoro.from_session(_sess, str(self.KOKORO_DIR / "voices-v1.0.bin"))
            _SHARED_KOKORO.update(sess=_sess, kokoro=self._kokoro)
        return self._kokoro

    async def synth_kokoro(self, text: str, speed: float | None = None) -> tuple[bytes, int]:
        """Synthesize with Kokoro locally. Returns (pcm16_bytes, sample_rate)."""
        import numpy as _np

        loop = asyncio.get_running_loop()

        def _synth() -> tuple[bytes, int]:
            k = self._get_kokoro()
            audio, sr = k.create(text, voice=self.kokoro_voice, speed=speed or self.kokoro_speed)
            pcm = (_np.clip(audio, -1.0, 1.0) * 32767).astype("int16")
            return pcm.tobytes(), int(sr)

        return await loop.run_in_executor(None, _synth)

    # Failure fall-through order after the picked engine. Edge is the
    # default primary (instant, streams, reliable); kokoro stays available
    # for explicit use but never blocks the chain when slow.
    FALLBACK_ORDER = ("edge", "gemini", "piper", "sapi")

    def _pick_engine(self) -> str:
        """Preference order (auto): edge -> gemini -> piper -> sapi.

        Explicit config pins one engine; failures cool an engine down for
        5 minutes so fallback is fast on repeat utterances. Kokoro is
        opt-in via engine="kokoro" (best quality offline, but slow on CPU).
        """
        if self.engine_pref not in ("", "auto"):
            return self.engine_pref
        for name in self.FALLBACK_ORDER:
            if name == "sapi":
                return "sapi"  # always available on Windows
            if name == "piper" and not self._piper_available():
                continue
            if self._engine_ok(name):
                return name
        return "sapi"

    def _engine_chain(self, first: str) -> list[str]:
        """Full cascade from `first`, skipping unavailable/dead engines."""
        chain = [first]
        for name in self.FALLBACK_ORDER:
            if name in chain:
                continue
            if name == "piper" and not self._piper_available():
                continue
            if name != "sapi" and not self._engine_ok(name):
                continue
            chain.append(name)
        return chain

    async def synth_piper(self, text: str) -> tuple[bytes, int]:
        """Synthesize with Piper locally. Returns (pcm16_bytes, sample_rate)."""
        loop = asyncio.get_running_loop()

        def _synth() -> tuple[bytes, int]:
            v = self._get_piper()
            chunks: list[bytes] = []
            sr = 22050
            for audio_chunk in v.synthesize(text):
                chunks.append(audio_chunk.audio_int16_bytes)
                sr = audio_chunk.sample_rate
            return b"".join(chunks), sr

        return await loop.run_in_executor(None, _synth)

    @staticmethod
    def _play_pcm(pcm: bytes, sample_rate: int, stop: asyncio.Event) -> None:
        import numpy as np
        import sounddevice as sd

        audio = np.frombuffer(pcm, dtype="int16")
        sd.play(audio, samplerate=sample_rate, blocking=False)
        while sd.get_stream().active:
            if stop.is_set():
                sd.stop()
                return
            sd.sleep(50)
        sd.stop()

    async def synth_edge(self, text: str) -> bytes:
        import edge_tts

        # Prosody: delivery adapts to the reply's tone
        from voice.expressive import classify_tone

        _, rate_adj, pitch_adj = classify_tone(text)
        rate = _combine_rate(rate_adj)
        pitch = _combine_pitch(pitch_adj)
        communicate = edge_tts.Communicate(text, self.edge_voice, rate=rate, pitch=pitch)
        buf = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    @staticmethod
    def _play_mp3(data: bytes, stop: asyncio.Event) -> None:
        import miniaudio
        import numpy as np
        import sounddevice as sd

        decoded = miniaudio.decode(data)
        # Convert int16 interleaved samples to float32 for sounddevice.
        raw = np.frombuffer(bytes(decoded.samples), dtype=np.int16)
        audio = raw.astype("float32") / 32768.0
        if decoded.nchannels == 2:
            audio = audio.reshape(-1, 2)
        # Play at the DECODED sample rate with the real channel count.
        # The old int16-direct path made some sounddevice builds play
        # stereo 44.1kHz edge audio as mono garbage (the "slowed robot").
        sd.play(audio, samplerate=decoded.sample_rate, blocking=False)
        while sd.get_stream().active:
            if stop.is_set():
                sd.stop()
                return
            sd.sleep(50)
        sd.stop()

    @staticmethod
    def _play_wav_file(path, stop: asyncio.Event) -> None:
        import wave

        import numpy as np
        import sounddevice as sd

        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            data = wf.readframes(wf.getnframes())
        audio = np.frombuffer(data, dtype="int16")
        sd.play(audio, samplerate=sr, blocking=False)
        while sd.get_stream().active:
            if stop.is_set():
                sd.stop()
                return
            sd.sleep(50)
        sd.stop()

    async def speak_chunk(self, text: str) -> bool:
        """Speak one chunk with barge-in support."""
        if not self.enabled:
            self.last_error = "speaker disabled (/tts on to enable)"
            return False
        spoken = sanitize_for_speech(text)
        if not spoken:
            self.last_error = "nothing speakable after cleanup"
            return False
        self.last_error = ""

        self.playback.reset()
        self.playback._speaking.set()
        # Barge-in: while we speak, watch the mic for real user speech and
        # stop playback immediately. Guarded by config; echo-resistant via
        # settle delay + adaptive energy floor + sustained-speech window.
        _barge_mon = None
        try:
            from core.config import load_config as _lc

            _cfgv = _lc().voice
            if getattr(_cfgv, "barge_in", True):
                from voice.mic import BargeInMonitor

                _barge_mon = BargeInMonitor(
                    on_barge=self.playback.barge_in,
                    should_stop=lambda: not self.playback.speaking,
                    trigger_ms=int(getattr(_cfgv, "barge_in_ms", 400)),
                )
                _barge_mon.start()
        except Exception:
            _barge_mon = None
        try:
            for engine in self._engine_chain(self._pick_engine()):
                if engine == "kokoro":
                    try:
                        # Tone-adaptive pacing from the shared classifier
                        # (excited +8% -> slightly faster, calm -> slower).
                        speed = self.kokoro_speed
                        try:
                            from voice.expressive import classify_tone

                            _, rate_adj, _ = classify_tone(spoken)
                            if rate_adj.startswith("+"):
                                with contextlib.suppress(ValueError):
                                    speed = self.kokoro_speed * (1.0 + int(rate_adj[1:-1]) / 200.0)
                            elif rate_adj.startswith("-"):
                                speed = self.kokoro_speed * 0.95
                        except Exception:
                            pass
                        pcm, sr = await self.synth_kokoro(spoken, speed=speed)
                        await asyncio.to_thread(self._play_pcm, pcm, sr, self.playback._stop)
                        self._engine_used = "kokoro"
                        return True
                    except Exception as exc:
                        self.last_error = f"kokoro failed: {type(exc).__name__}: {str(exc)[:120]}"
                        self._mark_engine_dead("kokoro")
                        continue
                if engine == "gemini":
                    try:
                        from voice.expressive import synth_gemini

                        wav = await synth_gemini(
                            spoken, persona=self.persona, voice=self.gemini_voice
                        )
                        if wav is not None and wav.exists() and wav.stat().st_size > 1000:
                            await asyncio.to_thread(self._play_wav_file, wav, self.playback._stop)
                            self._engine_used = "gemini"
                            return True
                        self.last_error = "gemini returned no audio"
                    except Exception as exc:
                        self.last_error = f"gemini failed: {type(exc).__name__}: {str(exc)[:120]}"
                    self._mark_engine_dead("gemini")
                    continue
                if engine == "piper":
                    try:
                        pcm, sr = await self.synth_piper(spoken)
                        await asyncio.to_thread(self._play_pcm, pcm, sr, self.playback._stop)
                        self._engine_used = "piper"
                        return True
                    except Exception as exc:
                        self.last_error = f"piper failed: {type(exc).__name__}: {str(exc)[:120]}"
                        self._mark_engine_dead("piper")
                        continue
                if engine == "edge":
                    try:
                        mp3 = await self.synth_edge(spoken)
                        await asyncio.to_thread(self._play_mp3, mp3, self.playback._stop)
                        self._engine_used = "edge"
                        return True
                    except Exception as exc:
                        self.last_error = f"edge failed: {type(exc).__name__}: {str(exc)[:120]}"
                        self._mark_engine_dead("edge")
                        continue
                if engine == "sapi":
                    ok = await asyncio.to_thread(sapi_fallback, spoken)
                    if not ok and not self.last_error:
                        self.last_error = "sapi fallback failed (no speech device?)"
                    self._engine_used = "sapi"
                    return ok
            # Chain exhausted without ever reaching sapi (defensive; the
            # chain builder always ends with sapi).
            ok = await asyncio.to_thread(sapi_fallback, spoken)
            self._engine_used = "sapi"
            return ok
        finally:
            if _barge_mon is not None:
                _barge_mon.stop()
            self.playback._speaking.clear()

    async def say(self, text: str) -> bool:
        return await self.speak_chunk(text)

    async def synth_audio(self, spoken: str) -> tuple[str, Any]:
        """Synthesize audio bytes/pcm using the active engine chain without playing it."""
        for engine in self._engine_chain(self._pick_engine()):
            if engine == "kokoro":
                try:
                    speed = self.kokoro_speed
                    try:
                        from voice.expressive import classify_tone

                        _, rate_adj, _ = classify_tone(spoken)
                        if rate_adj.startswith("+"):
                            with contextlib.suppress(ValueError):
                                speed = self.kokoro_speed * (1.0 + int(rate_adj[1:-1]) / 200.0)
                        elif rate_adj.startswith("-"):
                            speed = self.kokoro_speed * 0.95
                    except Exception:
                        pass
                    pcm, sr = await self.synth_kokoro(spoken, speed=speed)
                    return "kokoro", ("pcm", pcm, sr)
                except Exception as exc:
                    self.last_error = f"kokoro failed: {type(exc).__name__}: {str(exc)[:120]}"
                    self._mark_engine_dead("kokoro")
                    continue
            if engine == "gemini":
                try:
                    from voice.expressive import synth_gemini

                    wav = await synth_gemini(spoken, persona=self.persona, voice=self.gemini_voice)
                    if wav is not None and wav.exists() and wav.stat().st_size > 1000:
                        return "gemini", ("wav", wav)
                    self.last_error = "gemini returned no audio"
                except Exception as exc:
                    self.last_error = f"gemini failed: {type(exc).__name__}: {str(exc)[:120]}"
                self._mark_engine_dead("gemini")
                continue
            if engine == "piper":
                try:
                    pcm, sr = await self.synth_piper(spoken)
                    return "piper", ("pcm", pcm, sr)
                except Exception as exc:
                    self.last_error = f"piper failed: {type(exc).__name__}: {str(exc)[:120]}"
                    self._mark_engine_dead("piper")
                    continue
            if engine == "edge":
                try:
                    mp3 = await self.synth_edge(spoken)
                    return "edge", ("mp3", mp3)
                except Exception as exc:
                    self.last_error = f"edge failed: {type(exc).__name__}: {str(exc)[:120]}"
                    self._mark_engine_dead("edge")
                    continue
            if engine == "sapi":
                return "sapi", ("sapi", spoken)
        return "sapi", ("sapi", spoken)

    async def play_audio(self, engine: str, audio_data: Any) -> bool:
        """Play pre-synthesized audio data with barge-in support."""
        kind = audio_data[0]
        if kind == "pcm":
            _, pcm, sr = audio_data
            await asyncio.to_thread(self._play_pcm, pcm, sr, self.playback._stop)
            self._engine_used = engine
            return True
        elif kind == "wav":
            _, wav = audio_data
            await asyncio.to_thread(self._play_wav_file, wav, self.playback._stop)
            self._engine_used = engine
            return True
        elif kind == "mp3":
            _, mp3 = audio_data
            await asyncio.to_thread(self._play_mp3, mp3, self.playback._stop)
            self._engine_used = engine
            return True
        elif kind == "sapi":
            _, spoken = audio_data
            ok = await asyncio.to_thread(sapi_fallback, spoken)
            if not ok and not self.last_error:
                self.last_error = "sapi fallback failed (no speech device?)"
            self._engine_used = "sapi"
            return ok
        return False

    async def say_pipelined(self, text: str, on_first_audio: Any = None) -> bool:
        """Sentence-pipelined TTS: synthesizes sentence N+1 concurrently while sentence N plays.

        Invokes `on_first_audio` callback as soon as audio playback of the first sentence begins.
        """
        if not self.enabled:
            self.last_error = "speaker disabled (/tts on to enable)"
            return False

        sentences = split_sentences(text)
        cleaned = [sanitize_for_speech(s) for s in sentences]
        cleaned = [s for s in cleaned if s]
        if not cleaned:
            single = sanitize_for_speech(text)
            if not single:
                self.last_error = "nothing speakable after cleanup"
                return False
            cleaned = [single]

        self.last_error = ""
        self.playback.reset()
        self.playback._speaking.set()

        _barge_mon = None
        try:
            from core.config import load_config as _lc

            _cfgv = _lc().voice
            if getattr(_cfgv, "barge_in", True):
                from voice.mic import BargeInMonitor

                _barge_mon = BargeInMonitor(
                    on_barge=self.playback.barge_in,
                    should_stop=lambda: not self.playback.speaking,
                    trigger_ms=int(getattr(_cfgv, "barge_in_ms", 400)),
                )
                _barge_mon.start()
        except Exception:
            _barge_mon = None

        first_audio_called = False
        try:
            next_task = asyncio.create_task(self.synth_audio(cleaned[0]))

            for i in range(len(cleaned)):
                if self.playback._stop.is_set():
                    break

                assert next_task is not None  # set before loop; None only after final await
                engine, audio_data = await next_task
                if self.playback._stop.is_set():
                    break

                if i + 1 < len(cleaned):
                    next_task = asyncio.create_task(self.synth_audio(cleaned[i + 1]))
                else:
                    next_task = None

                if not first_audio_called:
                    first_audio_called = True
                    if on_first_audio is not None:
                        try:
                            if asyncio.iscoroutinefunction(on_first_audio):
                                await on_first_audio()
                            else:
                                on_first_audio()
                        except Exception:
                            pass

                await self.play_audio(engine, audio_data)
            return True
        finally:
            if _barge_mon is not None:
                _barge_mon.stop()
            self.playback._speaking.clear()

    def status(self) -> dict:
        """One-line health for chat/doctor. Never raises."""
        import time as _t

        try:
            dead = {
                k: max(0, int(v - _t.time())) for k, v in self._engine_dead.items() if v > _t.time()
            }
        except Exception:
            dead = {}
        return {
            "enabled": bool(self.enabled),
            "engine_pref": self.engine_pref,
            "engine_used": self._engine_used or "none yet",
            "last_error": self.last_error or "none",
            "cooling_down_s": dead,
        }

    async def say_streaming(self, text_stream) -> str:
        """Consume an async text stream, speaking sentence-by-sentence with barge-in."""
        buffer = ""
        spoken_any = False
        async for chunk in text_stream:
            if self.playback._stop.is_set():
                break
            buffer += chunk
            sentences = split_sentences(buffer)
            while len(sentences) > 1:
                sentence = sentences.pop(0)
                if sentence.strip():
                    await self.speak_chunk(sentence)
                    spoken_any = True
            buffer = sentences[0] if sentences else ""
        if buffer.strip() and not self.playback._stop.is_set():
            await self.speak_chunk(buffer)
            spoken_any = True
        return "spoken" if spoken_any else "silent"

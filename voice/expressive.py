"""Expressive TTS: Gemini speech synthesis with natural-language style
directions, plus a tone classifier that drives delivery (rate/pitch/style)
per reply — so JARVIS sounds like a person, not a reader.

Chain (each stage falls back on failure):
  gemini -> edge-tts (prosody-adjusted) -> piper -> sapi
"""

from __future__ import annotations

import asyncio
import os
import re
import struct
import wave
from pathlib import Path

_GEMINI_MODEL = "gemini-2.5-flash-preview-tts"
_GEMINI_VOICE_DEFAULT = "Kore"  # firm, clear, butler-adjacent
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "voice_samples" / "cache"

# Butler persona base style — every Gemini request carries this.
_BASE_STYLE = (
    "Speak like a composed British butler: warm, precise, quietly confident. "
    "Natural pace with smooth transitions. Subtle dry humour is welcome."
)

# Tone buckets: (keywords in the reply, style overlay, edge rate, edge pitch)
_TONES: list[tuple[tuple[str, ...], str, str, str]] = [
    (
        (
            "great news",
            "excellent",
            "congratulations",
            "well done",
            "success",
            "it works",
            "fixed",
            "nailed it",
            "wonderful",
            "amazing",
        ),
        "Bright and pleased, a touch of genuine delight.",
        "+10%",
        "+2Hz",
    ),
    (
        (
            "sorry",
            "unfortunately",
            "failed",
            "error",
            "problem",
            "issue",
            "cannot",
            "can't",
            "unable",
            "went wrong",
            "apolog",
        ),
        "Gently serious and apologetic, steady and calm.",
        "-10%",
        "-2Hz",
    ),
    (
        ("warning", "careful", "danger", "risk", "attention", "before you"),
        "Calm and alert, measured seriousness — not alarmist.",
        "-5%",
        "-1Hz",
    ),
    (
        ("funny", "haha", "joke", "amusing", "chuckle", "witty"),
        "Lightly amused, with a knowing smile in the voice.",
        "+5%",
        "+1Hz",
    ),
    (
        ("?",),
        "Attentive and engaged, ending with a natural rising question tone.",
        "+0%",
        "+0Hz",
    ),
]

_EMOTION_EXCLAIM_RE = re.compile(r"!{2,}|\b(wow|incredible)\b", re.IGNORECASE)


def classify_tone(text: str) -> tuple[str, str, str]:
    """Return (gemini_style_overlay, edge_rate, edge_pitch) for a reply."""
    low = text.lower()
    for keywords, style, rate, pitch in _TONES:
        if any(k in low for k in keywords):
            return style, rate, pitch
    if _EMOTION_EXCLAIM_RE.search(text):
        return "Animated and energetic.", "+8%", "+1Hz"
    # default: neutral butler
    return "", "+0%", "+0Hz"


def style_prompt_for(text: str, persona: str = "") -> str:
    """Full Gemini TTS style instruction for this reply."""
    overlay, _, _ = classify_tone(text)
    parts = [persona or _BASE_STYLE]
    if overlay:
        parts.append(overlay)
    return " ".join(parts)


# ── Gemini TTS (SDK) ──────────────────────────────────────────────────

_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY not set")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


async def synth_gemini(
    text: str,
    persona: str = "",
    voice: str = "",
    timeout_s: float = 20.0,
) -> Path | None:
    """Synthesize expressive speech with Gemini TTS. Returns a WAV path.

    Returns None on any failure (including timeout) — the caller falls
    back to the next engine.
    """
    try:
        from google import genai

        client = _get_gemini_client()
        style = style_prompt_for(text, persona)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_GEMINI_MODEL,
                # Style direction prefixes the text (Gemini TTS convention:
                # leading direction steers delivery without being spoken).
                contents=f"{style}\n\n{text}" if style else text,
                config=genai.types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai.types.SpeechConfig(
                        voice_config=genai.types.VoiceConfig(
                            prebuilt_voice_config=genai.types.PrebuiltVoiceConfig(
                                voice_name=voice or _GEMINI_VOICE_DEFAULT
                            )
                        )
                    ),
                ),
            ),
            timeout=timeout_s,
        )
        return _response_to_wav(response)
    except Exception:
        return None


def _response_to_wav(response) -> Path | None:
    """Extract inline audio from a Gemini response and wrap it as a WAV."""
    try:
        # Extract inline audio data
        part = response.candidates[0].content.parts[0]
        inline = getattr(part, "inline_data", None)
        if inline is None or inline.data is None:
            return None
        mime = getattr(inline, "mime_type", "") or ""
        raw = inline.data

        if "wav" in mime or "audio/L16" in mime or "pcm" in mime:
            return _pcm_to_wav_file(raw, mime)
        if "mp3" in mime:
            return _raw_to_file(raw, ".mp3")
        # unknown container: try WAV-shaped parse, else dump raw
        return _raw_to_file(raw, ".bin")
    except Exception:
        return None


def _response_to_wav_bytes(response) -> bytes | None:
    """Gemini response -> in-memory WAV bytes (no disk) for streaming."""
    try:
        part = response.candidates[0].content.parts[0]
        inline = getattr(part, "inline_data", None)
        if inline is None or inline.data is None:
            return None
        mime = getattr(inline, "mime_type", "") or ""
        raw = inline.data
        if raw[:4] == b"RIFF":
            return bytes(raw)
        # strip optional text header, wrap PCM in a RIFF header in memory
        idx = raw.find(b"AUDIO=")
        if idx >= 0:
            raw = raw[idx + len(b"AUDIO=") :]
        m = _RATE_RE.search(mime)
        sample_rate = int(m.group(1)) if m else 24000
        data = bytes(raw)
        header = (
            b"RIFF"
            + struct.pack("<I", 36 + len(data))
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
            + b"data"
            + struct.pack("<I", len(data))
        )
        return header + data
    except Exception:
        return None


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_for_tts(text: str, max_chars: int = 220, first_max_chars: int = 110) -> list[str]:
    """Split text into speakable segments at sentence boundaries.

    Latency-first design: the FIRST segment is just the first sentence
    (kept short) so it synthesizes fast and audio starts as early as
    possible; later segments group up to max_chars since their synthesis
    overlaps the playback of earlier ones.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    if not sentences:
        return []

    first = sentences[0]
    rest = sentences[1:]
    segments: list[str] = []
    if len(first) > first_max_chars:
        # very long opening sentence: split it, emit its head first
        parts = _split_long_sentence(first, first_max_chars)
        segments.append(parts[0])
        rest = parts[1:] + rest
    else:
        segments.append(first)

    # remaining sentences grouped at the normal budget
    buf: list[str] = []
    buf_len = 0
    for s in rest:
        if len(s) > max_chars:
            if buf:
                segments.append(" ".join(buf))
                buf, buf_len = [], 0
            parts = _split_long_sentence(s, max_chars)
            segments.extend(parts[:-1])
            if parts:
                buf = [parts[-1]]
                buf_len = len(parts[-1])
            continue
        if buf and buf_len + len(s) + 1 > max_chars:
            segments.append(" ".join(buf))
            buf, buf_len = [s], len(s)
        else:
            buf.append(s)
            buf_len += len(s) + 1
    if buf:
        segments.append(" ".join(buf))
    return segments


def _split_long_sentence(s: str, max_chars: int) -> list[str]:
    """Break an oversized sentence at commas/semicolons."""
    delimiters = [", ", "; ", " — ", " - "]
    parts = [s]
    for d in delimiters:
        pieces = []
        for p in parts:
            if len(p) <= max_chars:
                pieces.append(p)
                continue
            sub = p.split(d)
            cur = ""
            for x in sub:
                if cur and len(cur) + len(x) + len(d) > max_chars:
                    pieces.append(cur)
                    cur = x
                else:
                    cur = (cur + d + x) if cur else x
            if cur:
                pieces.append(cur)
        parts = pieces
    return parts


async def synth_gemini_bytes(
    text: str,
    persona: str = "",
    voice: str = "",
    timeout_s: float = 12.0,
) -> bytes | None:
    """Low-latency TTS: Gemini -> WAV bytes in memory. None = use fallback."""
    try:
        from google import genai

        client = _get_gemini_client()
        style = style_prompt_for(text, persona)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_GEMINI_MODEL,
                # Style direction prefixes the text (Gemini TTS convention:
                # leading direction steers delivery without being spoken).
                contents=f"{style}\n\n{text}" if style else text,
                config=genai.types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai.types.SpeechConfig(
                        voice_config=genai.types.VoiceConfig(
                            prebuilt_voice_config=genai.types.PrebuiltVoiceConfig(
                                voice_name=voice or _GEMINI_VOICE_DEFAULT
                            )
                        )
                    ),
                ),
            ),
            timeout=timeout_s,
        )
        return _response_to_wav_bytes(response)
    except Exception:
        return None


# ── helpers ───────────────────────────────────────────────────────────

_RATE_RE = re.compile(r"rate=(\d+)")


def _pcm_to_wav_file(raw: bytes, mime: str) -> Path | None:
    """Wrap raw PCM (with or without RIFF header) into a playable WAV."""
    try:
        if raw[:4] == b"RIFF":
            out = _CACHE_DIR / "gemini_riff.wav"
            out.write_bytes(raw)
            return out
        # Parse the 'params' header some responses carry: RATE=... etc.
        sample_rate = 24000
        m = _RATE_RE.search(mime)
        if m:
            sample_rate = int(m.group(1))
        # Data may be prefixed with a text header ending in 'AUDIO='
        idx = raw.find(b"AUDIO=")
        if idx >= 0:
            raw = raw[idx + len(b"AUDIO=") :]
        out = _CACHE_DIR / "gemini_out.wav"
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(raw)
        return out
    except Exception:
        return None


def _raw_to_file(raw: bytes, suffix: str) -> Path | None:
    try:
        out = _CACHE_DIR / f"gemini_out{suffix}"
        out.write_bytes(raw)
        return out
    except Exception:
        return None


def ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


ensure_cache_dir()

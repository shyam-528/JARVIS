"""Voice/TTS endpoints: config, test, say (+streaming), distill."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api import state
from api.state import get_auth_token

router = APIRouter()


@router.get("/api/voice/config")
async def voice_get_config(token: str = Depends(get_auth_token)):
    """Current voice configuration (engine, voices, distiller)."""
    v = state.orch.cfg.voice
    return {
        "tts_enabled": v.tts_enabled,
        "tts_engine": v.tts_engine,
        "tts_voice": v.tts_voice,
        "piper_voice": v.piper_voice,
        "whisper_model": v.whisper_model,
        "distill_speech": v.distill_speech,
        "distill_threshold": v.distill_threshold,
    }


@router.post("/api/voice/test")
async def voice_test(
    text: str = "Good evening, sir. All systems are operational.",
    token: str = Depends(get_auth_token),
):
    """Speak a line through the server's speakers (tests TTS + voice choice)."""
    try:
        from voice.tts import Speaker2

        speaker = Speaker2(
            voice=state.orch.cfg.voice.piper_voice,
            edge_voice=state.orch.cfg.voice.tts_voice,
            enabled=True,
            engine=state.orch.cfg.voice.tts_engine,
            persona=getattr(state.orch.cfg.voice, "tts_persona", ""),
            gemini_voice=getattr(state.orch.cfg.voice, "gemini_voice", "Kore"),
        )
        ok = await speaker.speak_chunk(text)
        return {"ok": ok, "voice": state.orch.cfg.voice.tts_voice}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def _distill_for_speech(text: str, user_text: str = "") -> str:
    """Condense a full reply into the short spoken line (movie-JARVIS style).

    Shared by /api/voice/say and /api/voice/say/stream so the website speaks
    the same distilled version the terminal does instead of reading the
    entire screen aloud. Fails open: any distiller error returns the text
    unchanged (truncation still caps it), so speech never breaks.
    """
    try:
        if state.orch is None or not getattr(state.orch.cfg.voice, "distill_speech", True):
            return text
        threshold = int(getattr(state.orch.cfg.voice, "distill_threshold", 200))
        if len(" ".join(text.split())) <= threshold:
            return text  # short enough to speak verbatim
        from voice.distiller import SpeechDistiller

        d = SpeechDistiller(state.orch.router, state.orch.cfg)
        return await d.spoken_version(text, user_text=user_text)
    except Exception:
        return text


@router.post("/api/voice/say")
async def voice_say(request: Request, token: str = Depends(get_auth_token)):
    """Synthesize speech server-side (Gemini -> edge chain, same voice as
    terminal voice mode) and return audio for the browser to play.

    Body: {"text": "..."} — returns audio/wav (Gemini) or audio/mpeg (edge).
    Falls back to 503 so the client can use browser speechSynthesis.
    """

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    # Speak the distilled butler line, not the whole screen (same as terminal).
    if not body.get("verbatim"):
        text = await _distill_for_speech(text, user_text=body.get("user_text", ""))
    if len(text) > 1200:
        text = text[:1200]

    persona = getattr(state.orch.cfg.voice, "tts_persona", "")
    gvoice = getattr(state.orch.cfg.voice, "gemini_voice", "Kore")
    evoice = getattr(state.orch.cfg.voice, "tts_voice", "en-GB-SoniaNeural")
    pvoice = getattr(state.orch.cfg.voice, "piper_voice", "en_GB-alan-medium")
    engines_tried: list[str] = []

    def _wav_response(pcm: bytes, sr: int, engine: str):
        import io as _io
        import wave as _wave

        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm)
        return Response(
            content=buf.getvalue(),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store", "X-TTS-Engine": engine},
        )

    # Chain: edge -> gemini -> piper. Kokoro stays explicit-only: on CPU it
    # can take 10-30s per utterance, which reads as "voice broken" on the web.
    # 1) edge-tts: instant, streams, free (needs internet)
    try:
        from voice.tts import Speaker2

        s = Speaker2(voice=pvoice, edge_voice=evoice, enabled=True, engine="edge", persona=persona)
        mp3 = await s.synth_edge(text)
        if mp3 and len(mp3) > 500:
            return Response(
                content=mp3,
                media_type="audio/mpeg",
                headers={"Cache-Control": "no-store", "X-TTS-Engine": "edge"},
            )
    except Exception as exc:
        engines_tried.append(f"edge: {type(exc).__name__}")

    # 2) Gemini TTS: expressive, WAV
    try:
        from voice.expressive import synth_gemini

        wav = await synth_gemini(text, persona=persona, voice=gvoice)
        if wav is not None and wav.exists() and wav.stat().st_size > 1000:
            return Response(
                content=wav.read_bytes(),
                media_type="audio/wav",
                headers={"Cache-Control": "no-store", "X-TTS-Engine": "gemini"},
            )
        engines_tried.append("gemini: no audio")
    except Exception as exc:
        engines_tried.append(f"gemini: {type(exc).__name__}")

    # 3) Piper: fully local, fast, offline-safe
    try:
        from voice.tts import Speaker2

        s = Speaker2(voice=pvoice, edge_voice=evoice, enabled=True, engine="piper", persona=persona)
        if s._piper_available():
            pcm, sr = await s.synth_piper(text)
            if pcm and len(pcm) > 2000:
                return _wav_response(pcm, sr, "piper")
            engines_tried.append("piper: no audio")
    except Exception as exc:
        engines_tried.append(f"piper: {type(exc).__name__}")

    detail = "tts unavailable — use browser voice"
    if engines_tried:
        detail += f" (tried: {'; '.join(engines_tried)})"
    raise HTTPException(status_code=503, detail=detail)


@router.post("/api/voice/say/stream")
async def voice_say_stream(request: Request, token: str = Depends(get_auth_token)):
    """Low-latency streaming TTS: synthesizes sentence-by-sentence and emits
    NDJSON lines: {"seg": "<base64 wav>"} as each segment is ready, then
    {"done": true}. First audio leaves when the first sentence is complete
    instead of after the entire reply.

    Engine chain per segment: edge -> gemini -> piper (kokoro explicit-only:
    10-30s per utterance on CPU reads as "voice broken" on the web). Long
    replies are distilled to the short spoken line before synthesis.
    """
    import base64 as _b64

    from fastapi.responses import StreamingResponse

    from voice.expressive import split_for_tts, synth_gemini_bytes

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if not body.get("verbatim"):
        text = await _distill_for_speech(text, user_text=body.get("user_text", ""))
    if len(text) > 1500:
        text = text[:1500]

    persona = getattr(state.orch.cfg.voice, "tts_persona", "")
    gvoice = getattr(state.orch.cfg.voice, "gemini_voice", "Kore")
    evoice = getattr(state.orch.cfg.voice, "tts_voice", "en-GB-SoniaNeural")
    pvoice = getattr(state.orch.cfg.voice, "piper_voice", "en_GB-alan-medium")
    segments = split_for_tts(text)

    async def gen():
        spoke_any = False
        seg_count = 0
        engines: dict = {}

        # One shared speaker per request: edge is stateless, piper caches
        # its model on the instance (a fresh Speaker2 per segment would
        # reload piper every sentence). Chain per segment is
        # edge -> gemini -> piper; kokoro stays explicit-only (~15s per
        # utterance on CPU reads as "voice broken" on the web).
        from voice.tts import Speaker2 as _SegSpk

        _spk = _SegSpk(
            voice=pvoice, edge_voice=evoice, enabled=True, engine="edge", persona=persona
        )

        async def seg_wav(seg_text: str):
            try:
                mp3 = await _spk.synth_edge(seg_text)
                if mp3 and len(mp3) > 500:
                    engines["edge"] = engines.get("edge", 0) + 1
                    return mp3
            except Exception:
                pass
            try:
                wav = await synth_gemini_bytes(seg_text, persona=persona, voice=gvoice)
                if wav:
                    engines["gemini"] = engines.get("gemini", 0) + 1
                    return wav
            except Exception:
                pass
            try:
                if _spk._piper_available():
                    import io as _io2
                    import wave as _wave2

                    pcm, sr = await _spk.synth_piper(seg_text)
                    if pcm and len(pcm) > 1000:
                        buf = _io2.BytesIO()
                        with _wave2.open(buf, "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(sr)
                            wf.writeframes(pcm)
                        engines["piper"] = engines.get("piper", 0) + 1
                        return buf.getvalue()
            except Exception:
                pass
            return None

        # Pipeline: start synthesizing the NEXT segment while the current
        # one streams out — total latency ~= first-segment time, not sum.
        next_task = None
        for i, seg in enumerate(segments):
            cur_task = next_task or asyncio.ensure_future(seg_wav(seg))
            next_task = (
                asyncio.ensure_future(seg_wav(segments[i + 1])) if i + 1 < len(segments) else None
            )
            wav = await cur_task
            if wav:
                spoke_any = True
                seg_count += 1
                yield '{"seg":"' + _b64.b64encode(wav).decode() + '"}\n'
        if next_task is not None:
            next_task.cancel()
        from core.logging_config import get_logger as _vlog

        _vlog("api").info(
            "voice/say/stream: segs=%d spoke=%s engines=%s",
            seg_count,
            spoke_any,
            engines,
        )
        import json as _json

        yield (
            '{"done": '
            + ("true" if spoke_any else "false")
            + ', "engines": '
            + _json.dumps(engines)
            + "}\n"
        )

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/voice/distill")
async def voice_distill(request: Request, token: str = Depends(get_auth_token)):
    """Distill a long reply into the short spoken version (website preview)."""
    body = await request.json()
    text = body.get("text", "")
    user_text = body.get("user_text", "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text required")
    try:
        from voice.distiller import SpeechDistiller

        d = SpeechDistiller(state.orch.router, state.orch.cfg)
        spoken = await d.spoken_version(text, user_text=user_text)
        return {"spoken": spoken, "full_chars": len(text), "spoken_chars": len(spoken)}
    except Exception as exc:
        return {"spoken": text[:300], "error": str(exc)}

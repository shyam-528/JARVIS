from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from dataclasses import dataclass

import httpx

from core.config import Config
from core.providers import OpenAICompatClient
from core.router import FailoverRouter

log = logging.getLogger(__name__)


@dataclass
class VisionResult:
    provider: str
    analysis: str
    confidence: float
    metadata: dict


@dataclass
class AudioResult:
    provider: str
    transcript: str
    language: str
    confidence: float
    duration: float


@dataclass
class MultiModalInput:
    text: str | None = None
    image_b64: str | None = None
    audio_b64: str | None = None
    image_path: str | None = None
    audio_path: str | None = None


class MultiModalBrain:
    """Unified multi-modal brain: text + vision + audio via free APIs."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, config: Config):
        self.router = router
        self.client = client
        self.config = config

        # Vision providers (support images)
        self.vision_providers = [
            p
            for p in router.providers
            if "vision" in getattr(p, "tags", []) or "multimodal" in getattr(p, "tags", [])
        ]

        # Audio providers (STT/TTS)
        self.audio_providers = [
            p
            for p in router.providers
            if "audio" in getattr(p, "tags", [])
            or p.name.startswith("google")
            or p.name.startswith("openrouter")
        ]

        # Initialize external clients
        self._init_external_clients()

    def _init_external_clients(self):
        """Init external vision/audio clients."""
        # Google Vision (if key available)
        self.google_vision_key = (
            self.config.notify.google_vision_key
            if hasattr(self.config.notify, "google_vision_key")
            else None
        )

        # AssemblyAI / Deepgram / Whisper (if keys available)
        self.assemblyai_key = getattr(self.config.notify, "assemblyai_key", None)
        self.deepgram_key = getattr(self.config.notify, "deepgram_key", None)

    # ===== Vision =====

    async def analyze_image(
        self,
        image_input: MultiModalInput,
        prompt: str = "Describe this image in detail",
        task_type: str = "vision",
    ) -> VisionResult:
        """Analyze image using best available vision model."""

        # Get image bytes
        img_bytes = await self._get_image_bytes(image_input)
        if not img_bytes:
            return VisionResult("error", "No valid image provided", 0.0, {})

        img_b64 = base64.b64encode(img_bytes).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }
        ]

        # ONLY use vision-capable providers (image-capable models)
        vision_names = [p.name for p in self.vision_providers]
        if not vision_names:
            return VisionResult(
                "error",
                "No vision-capable provider available. Configure a multimodal model "
                "(e.g. gemini-3.5-flash) in config.yaml.",
                0.0,
                {},
            )

        try:
            collected = []
            used = ""
            async for name, chunk in self.router.chat(
                messages, tools=None, task_type="vision", only=vision_names
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
                    used = name
            analysis = "".join(collected).strip()
            if analysis:
                return VisionResult(
                    provider=used or "vision",
                    analysis=analysis,
                    confidence=0.85,
                    metadata={"prompt": prompt},
                )
        except Exception as e:
            return VisionResult("error", f"Vision analysis failed: {e}", 0.0, {})

        # Fallback to external vision APIs
        return await self._fallback_vision(img_bytes, prompt)

    async def _analyze_with_provider(
        self,
        provider,
        img_bytes: bytes,
        prompt: str,
    ) -> dict:
        """Analyze using a vision-capable LLM provider."""
        img_b64 = base64.b64encode(img_bytes).decode()

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }
        ]

        async for _, chunk in self.router.chat(messages, None, "vision"):
            if isinstance(chunk, str):
                return {"analysis": chunk, "confidence": 0.8}
        return {"analysis": "", "confidence": 0.0}

    async def _fallback_vision(self, img_bytes: bytes, prompt: str) -> VisionResult:
        """Fallback to external vision APIs."""
        # Try Google Vision API
        if self.google_vision_key:
            result = await self._google_vision(img_bytes)
            if result:
                return VisionResult("google_vision", result, 0.9, {})

        return VisionResult("error", "No vision provider available", 0.0, {})

    async def _google_vision(self, img_bytes: bytes) -> str | None:
        """Google Vision API analysis."""
        if not self.google_vision_key:
            return None
        try:
            img_b64 = base64.b64encode(img_bytes).decode()
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://vision.googleapis.com/v1/images:annotate?key={self.google_vision_key}",
                    json={
                        "requests": [
                            {
                                "image": {"content": img_b64},
                                "features": [
                                    {"type": "LABEL_DETECTION", "maxResults": 10},
                                    {"type": "TEXT_DETECTION", "maxResults": 5},
                                    {"type": "OBJECT_LOCALIZATION", "maxResults": 10},
                                    {"type": "FACE_DETECTION", "maxResults": 5},
                                ],
                            }
                        ]
                    },
                    timeout=30.0,
                )
                data = resp.json()
                if "responses" in data:
                    r = data["responses"][0]
                    parts = []
                    if "labelAnnotations" in r:
                        labels = [ann["description"] for ann in r["labelAnnotations"][:5]]
                        parts.append(f"Labels: {', '.join(labels)}")
                    if "textAnnotations" in r:
                        parts.append(f"Text: {r['textAnnotations'][0]['description'][:200]}")
                    if "localizedObjectAnnotations" in r:
                        objs = [o["name"] for o in r["localizedObjectAnnotations"][:5]]
                        parts.append(f"Objects: {', '.join(objs)}")
                    return "\n".join(parts)
        except Exception:
            pass
        return None

    async def _get_image_bytes(self, input: MultiModalInput) -> bytes | None:
        """Get image bytes from various input types."""
        if input.image_bytes:
            return input.image_bytes
        if input.image_b64:
            return base64.b64decode(input.image_b64)
        if input.image_path:
            try:
                with open(input.image_path, "rb") as f:
                    return f.read()
            except OSError as e:
                log.warning("failed to read image file %s: %s", input.image_path, e)
        return None

    # ===== Audio =====

    async def transcribe_audio(
        self,
        audio_input: MultiModalInput,
        language: str | None = None,
    ) -> AudioResult:
        """Transcribe audio using best available STT."""

        audio_bytes = await self._get_audio_bytes(audio_input)
        if not audio_bytes:
            return AudioResult("error", "", "", 0.0, 0.0)

        # Try AssemblyAI
        if self.assemblyai_key:
            result = await self._assemblyai_transcribe(audio_bytes, language)
            if result:
                return result

        # Try Deepgram
        if self.deepgram_key:
            result = await self._deepgram_transcribe(audio_bytes, language)
            if result:
                return result

        # Fallback to local Whisper (if available)
        return await self._local_whisper(audio_bytes, language)

    async def _assemblyai_transcribe(
        self, audio_bytes: bytes, language: str | None
    ) -> AudioResult | None:
        """AssemblyAI transcription."""
        if not self.assemblyai_key:
            return None
        try:
            async with httpx.AsyncClient() as client:
                # Upload
                upload = await client.post(
                    "https://api.assemblyai.com/v2/upload",
                    headers={"authorization": self.assemblyai_key},
                    content=audio_bytes,
                    timeout=60.0,
                )
                upload_url = upload.json().get("upload_url")

                # Transcribe
                transcribe = await client.post(
                    "https://api.assemblyai.com/v2/transcript",
                    headers={"authorization": self.assemblyai_key},
                    json={"audio_url": upload_url, "language_code": language or "en"},
                    timeout=30.0,
                )
                transcript_id = transcribe.json().get("id")

                # Poll
                for _ in range(60):
                    await asyncio.sleep(2)
                    poll = await client.get(
                        f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                        headers={"authorization": self.assemblyai_key},
                        timeout=10.0,
                    )
                    data = poll.json()
                    if data.get("status") == "completed":
                        return AudioResult(
                            "assemblyai",
                            data.get("text", ""),
                            data.get("language_code", "en"),
                            data.get("confidence", 0.9),
                            data.get("audio_duration", 0),
                        )
                    elif data.get("status") == "error":
                        break
        except Exception:
            pass
        return None

    async def _deepgram_transcribe(
        self, audio_bytes: bytes, language: str | None
    ) -> AudioResult | None:
        """Deepgram transcription."""
        if not self.deepgram_key:
            return None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://api.deepgram.com/v1/listen?model=nova-2&language={language or 'en'}",
                    headers={
                        "Authorization": f"Token {self.deepgram_key}",
                        "Content-Type": "audio/wav",
                    },
                    content=audio_bytes,
                    timeout=30.0,
                )
                data = resp.json()
                transcript = (
                    data.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                )
                confidence = (
                    data.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("confidence", 0)
                )
                return AudioResult("deepgram", transcript, language or "en", confidence, 0)
        except Exception:
            pass
        return None

    async def _local_whisper(self, audio_bytes: bytes, language: str | None) -> AudioResult:
        """Local Whisper fallback. Never raises: failure -> AudioResult("error", ...).

        Transcriber.transcribe_file is synchronous and CPU-bound, so it
        runs in a thread to keep the event loop responsive. The temp wav
        is always cleaned up.
        """
        import os
        import tempfile

        temp_path = None
        try:
            from voice.stt import Transcriber

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_bytes)
                temp_path = f.name

            transcriber = Transcriber()
            text = await asyncio.to_thread(transcriber.transcribe_file, temp_path, language)
            return AudioResult("local_whisper", text, language or "en", 0.7, 0.0)
        except Exception as e:
            log.warning("local whisper fallback failed: %s", e)
            return AudioResult("error", str(e), "", 0.0, 0.0)
        finally:
            if temp_path:
                with contextlib.suppress(OSError):
                    os.unlink(temp_path)

    async def _get_audio_bytes(self, input: MultiModalInput) -> bytes | None:
        if input.audio_bytes:
            return input.audio_bytes
        if input.audio_b64:
            return base64.b64decode(input.audio_b64)
        if input.audio_path:
            try:
                with open(input.audio_path, "rb") as f:
                    return f.read()
            except OSError as e:
                log.warning("failed to read audio file %s: %s", input.audio_path, e)
        return None

    # ===== TTS =====

    async def text_to_speech(
        self,
        text: str,
        voice: str = "en-GB-RyanNeural",
        language: str = "en",
    ) -> bytes | None:
        """Generate speech from text -> MP3 bytes, or None on failure.

        Uses the voice stack's edge-tts engine (network synth, no local
        playback). Failures are logged, not swallowed: the tool layer
        reports "TTS failed" and the log says why.
        """
        try:
            from voice.tts import Speaker2

            speaker = Speaker2(edge_voice=voice, enabled=True, engine="edge")
            audio = await speaker.synth_edge(text)
            if not audio:
                log.warning("text_to_speech: edge-tts returned no audio")
                return None
            return audio
        except Exception as e:
            log.warning("text_to_speech failed: %s", e)
            return None

    # ===== Unified Multi-Modal Reasoning =====

    async def reason_multimodal(
        self,
        input: MultiModalInput,
        task: str = "analyze",
    ) -> dict:
        """Unified reasoning across text, vision, audio."""

        results = {"text": None, "vision": None, "audio": None}

        # Process text
        if input.text:
            results["text"] = input.text

        # Process vision
        if input.image_b64 or input.image_path:
            vision_result = await self.analyze_image(input, f"{task}: {{input.text}}")
            results["vision"] = {
                "analysis": vision_result.analysis,
                "provider": vision_result.provider,
            }

        # Process audio
        if input.audio_b64 or input.audio_path:
            audio_result = await self.transcribe_audio(input)
            results["audio"] = {
                "transcript": audio_result.transcript,
                "provider": audio_result.provider,
            }

        # Unified reasoning
        combined_prompt = self._build_multimodal_prompt(results, task)

        final_response = ""
        async for _, chunk in self.router.chat(
            [{"role": "user", "content": combined_prompt}], None, "reasoning"
        ):
            if isinstance(chunk, str):
                final_response += chunk

        return {
            "response": final_response,
            "modalities_used": [k for k, v in results.items() if v],
            "details": results,
        }

    def _build_multimodal_prompt(self, results: dict, task: str) -> str:
        """Build prompt from multi-modal results."""
        parts = [f"Task: {task}\n"]

        if results.get("text"):
            parts.append(f"Text input: {results['text']}")
        if results.get("vision"):
            parts.append(
                f"Visual analysis ({results['vision']['provider']}): {results['vision']['analysis']}"
            )
        if results.get("audio"):
            parts.append(
                f"Audio transcript ({results['audio']['provider']}): {results['audio']['transcript']}"
            )

        parts.append("\nProvide a comprehensive response integrating all modalities.")
        return "\n".join(parts)

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from typing import Any

from rich.console import Console

from core.orchestrator import Orchestrator
from voice.distiller import SpeechDistiller
from voice.stt import Transcriber
from voice.tts import (
    PlaybackController,
    Speaker2,
    is_confirmation,
)
from voice.wake import WakeListener

VOICE_COMMANDS = {
    "stop": "stop",
    "quiet": "stop",
    "shut up": "stop",
    "cancel": "stop",
    "repeat": "repeat",
    "say that again": "repeat",
    "louder": "volume_up",
    "volume up": "volume_up",
    "quieter": "volume_down",
    "volume down": "volume_down",
}


class VoiceSession:
    """Voice 2.0: streaming speech, barge-in, voice commands, VAD recording."""

    def __init__(
        self,
        orch: Orchestrator,
        console: Console,
        whisper_model: str = "tiny",
        tts_voice: str = "en_GB-alan-medium",
        edge_voice: str = "en-GB-RyanNeural",
        tts_enabled: bool = True,
        tts_engine: str = "auto",
        silence_s: float = 0.9,
        max_record_s: float = 15.0,
        speak_max_chars: int = 300,
        wake_threshold: float = 0.5,
        stt_language: str | None = None,
        input_device: int | None = None,
        tts_persona: str = "",
        gemini_voice: str = "Kore",
    ):
        self.orch = orch
        self.console = console
        self.playback = PlaybackController()
        self.speaker = Speaker2(
            voice=tts_voice,
            edge_voice=edge_voice,
            enabled=tts_enabled,
            engine=tts_engine,
            playback=self.playback,
            persona=tts_persona,
            gemini_voice=gemini_voice,
        )
        self.transcriber = Transcriber(model_size=whisper_model)
        # Movie-style concise speech: long replies distilled to <=2 sentences
        self.distiller = SpeechDistiller(orch.router, orch.cfg)
        if not getattr(orch.cfg.voice, "distill_speech", True):
            self.distiller.enabled = False
        self.distiller.threshold = int(getattr(orch.cfg.voice, "distill_threshold", 200))
        self.silence_s = silence_s
        self.max_record_s = max_record_s
        self.speak_max_chars = speak_max_chars
        self.wake_threshold = wake_threshold
        self.stt_language = stt_language
        self.input_device = input_device
        self.pending_full: str | None = None
        self.last_reply: str = ""
        self.active = True
        self._last_level = ""
        self._listening_for_barge_in = False

    def _listen_and_transcribe(self) -> str:
        from voice.mic import record_vad

        def on_level(level: str):
            self._last_level = level

        pcm = record_vad(
            silence_ms=int(self.silence_s * 1000),
            max_s=self.max_record_s,
            on_level=on_level,
            input_device=self.input_device,
        )
        if pcm.size == 0:
            return ""
        return self.transcriber.transcribe(pcm, language=self.stt_language)

    def _match_voice_command(self, text: str) -> str | None:
        t = text.strip().lower().rstrip(".!?")
        if t in VOICE_COMMANDS:
            return VOICE_COMMANDS[t]
        return None

    async def _handle_voice_command(self, command: str) -> bool:
        if command == "stop":
            self.playback.barge_in()
            self.console.print("[dim]⏹ stopped[/]")
            return True
        if command == "repeat":
            if self.last_reply:
                self.console.print("[dim]↻ repeating[/]")
                # Repeat the spoken (condensed) version, not the full text
                if len(self.last_reply) > self.distiller.threshold:
                    spoken = await self.distiller.spoken_version(self.last_reply)
                    await self.speaker.say(spoken)
                else:
                    await self.speaker.say(self.last_reply)
            else:
                await self.speaker.say("Nothing to repeat, sir.")
            return True
        if command == "volume_up":
            try:
                await self.orch.toolbox.execute("set_volume", '{"percent": 80}')
                await self.speaker.say("Volume raised, sir.")
            except Exception as exc:
                # fail-visible: speech still works, so note it on the console
                self.console.print(f"[dim]volume command failed: {type(exc).__name__}[/]")
            return True
        if command == "volume_down":
            try:
                await self.orch.toolbox.execute("set_volume", '{"percent": 30}')
                await self.speaker.say("Volume lowered, sir.")
            except Exception as exc:
                # fail-visible: speech still works, so note it on the console
                self.console.print(f"[dim]volume command failed: {type(exc).__name__}[/]")
            return True
        return False

    async def _say_logged(self, text: str, on_first_audio: Any = None) -> None:
        """speaker.say_pipelined + surface the engine used when it is NOT edge, so a
        silent fallback to piper/sapi (the "robot voice") is always visible
        in the terminal and the log."""
        if hasattr(self.speaker, "say_pipelined"):
            await self.speaker.say_pipelined(text, on_first_audio=on_first_audio)
        else:
            if on_first_audio is not None:
                try:
                    if asyncio.iscoroutinefunction(on_first_audio):
                        await on_first_audio()
                    else:
                        on_first_audio()
                except Exception:
                    pass
            await self.speaker.say(text)
        used = getattr(self.speaker, "_engine_used", "")
        if used and used != "edge":
            err = getattr(self.speaker, "last_error", "")
            msg = f"voice: {used}{' — ' + err if err else ''}"
            self.console.print(f"[yellow]{msg}[/]")
            try:
                from core.logging_config import get_logger

                get_logger("voice").warning("non-edge engine used: %s | %s", used, err)
            except Exception:
                pass

    async def _speak_reply(self, text: str, has_screen: bool = True, user_text: str = "") -> None:
        """Speak a reply: distilled short line if long, verbatim if short."""
        self.last_reply = text
        spoken_tag = None
        match = re.search(r"<spoken>(.*?)</spoken>", text, re.DOTALL | re.IGNORECASE)
        if match:
            spoken_tag = match.group(1).strip()

        if spoken_tag:
            spoken = spoken_tag
            clean_display = re.sub(
                r"\s*<spoken>.*?</spoken>", "", text, flags=re.DOTALL | re.IGNORECASE
            ).strip()
            if len(" ".join(clean_display.split())) > self.speak_max_chars:
                self.pending_full = clean_display
            else:
                self.pending_full = None
            await self._say_logged(spoken)
            return

        if len(text) <= self.distiller.threshold:
            self.pending_full = None
            await self._say_logged(text)
            return
        try:
            spoken = await self.distiller.spoken_version(
                text, user_text=user_text, has_screen=has_screen
            )
        except Exception as exc:
            spoken = " ".join(text.split())[: self.speak_max_chars]
            with contextlib.suppress(Exception):
                self.console.print(
                    f"[dim]voice draft failed ({type(exc).__name__}) — speaking verbatim[/]"
                )
            self.pending_full = None
            await self._say_logged(spoken)
            return
        if len(" ".join(text.split())) > self.speak_max_chars:
            self.pending_full = text
        else:
            self.pending_full = None
        await self._say_logged(spoken)

    async def _stream_and_speak(self, text: str, has_screen: bool = True) -> None:
        """Stream orchestrator output to screen; speak ONE distilled butler
        line at the end (movie style) instead of narrating every sentence."""
        t_turn_start = time.perf_counter()
        t_gen_first: float | None = None
        t_gen_end: float | None = None
        t_distill_end: float | None = None
        t_tts_first_audio: float | None = None

        self.console.print("[dim]‹say 'stop' to interrupt›[/]")
        collected: list[str] = []
        try:
            from voice.distiller import SpokenTagFilter as _TagFilter

            tag_filter = _TagFilter()
        except Exception:
            tag_filter = None

        async def text_stream():
            nonlocal t_gen_first
            async for provider, chunk in self.orch.process(text, surface="voice"):
                if provider == "tool":
                    self.console.print(f"[dim]{chunk}[/]")
                    continue
                if t_gen_first is None and chunk:
                    t_gen_first = time.perf_counter()
                collected.append(chunk)
                # The single-shot <spoken> tag is for TTS, not eyes — keep it
                # off the screen even when split across stream chunks.
                if tag_filter is not None and isinstance(chunk, str):
                    visible = tag_filter.feed(chunk)
                else:
                    visible = chunk if isinstance(chunk, str) else ""
                print(visible, end="", flush=True)
                await asyncio.sleep(0)
                if self.playback._stop.is_set():
                    return
            if tag_filter is not None:
                print(tag_filter.flush(), end="", flush=True)

        await text_stream()
        t_gen_end = time.perf_counter()
        print()

        full = "".join(collected)
        self.last_reply = full

        if self.playback._stop.is_set():
            self.console.print("[dim]⏹ speech interrupted[/]")
            self.pending_full = None
            return

        spoken_tag = None
        match = re.search(r"<spoken>(.*?)</spoken>", full, re.DOTALL | re.IGNORECASE)
        if match:
            spoken_tag = match.group(1).strip()

        spoken = ""
        if spoken_tag:
            spoken = spoken_tag
            t_distill_end = time.perf_counter()
            clean_display = re.sub(
                r"\s*<spoken>.*?</spoken>", "", full, flags=re.DOTALL | re.IGNORECASE
            ).strip()
            if len(" ".join(clean_display.split())) > self.speak_max_chars:
                self.pending_full = clean_display
            else:
                self.pending_full = None
        elif len(full) <= self.distiller.threshold:
            self.pending_full = None
            spoken = full
            t_distill_end = time.perf_counter()
        else:
            self.console.print("[dim]‹condensing for speech›[/]")
            try:
                spoken = await self.distiller.spoken_version(
                    full, user_text=text, has_screen=has_screen
                )
                t_distill_end = time.perf_counter()
            except Exception as exc:
                t_distill_end = time.perf_counter()
                spoken = " ".join(full.split())[: self.speak_max_chars]
                with contextlib.suppress(Exception):
                    self.console.print(
                        f"[dim]voice draft failed ({type(exc).__name__}) — speaking verbatim[/]"
                    )
                self.pending_full = None

                def on_audio_first():
                    nonlocal t_tts_first_audio
                    t_tts_first_audio = time.perf_counter()

                await self._say_logged(spoken, on_first_audio=on_audio_first)
                self._record_timings(
                    t_turn_start, t_gen_first, t_gen_end, t_distill_end, t_tts_first_audio
                )
                return
            if len(" ".join(full.split())) > self.speak_max_chars:
                self.pending_full = full
            else:
                self.pending_full = None

        def on_first_audio_cb():
            nonlocal t_tts_first_audio
            t_tts_first_audio = time.perf_counter()

        await self._say_logged(spoken, on_first_audio=on_first_audio_cb)
        self._record_timings(t_turn_start, t_gen_first, t_gen_end, t_distill_end, t_tts_first_audio)

    def _record_timings(
        self,
        t_start: float,
        t_gen_first: float | None,
        t_gen_end: float | None,
        t_distill_end: float | None,
        t_tts_first: float | None,
    ) -> dict[str, float]:
        """Record and log wall-clock turn timing metrics."""
        now = time.perf_counter()
        ttft_s = round(t_gen_first - t_start, 3) if t_gen_first else 0.0
        gen_s = round((t_gen_end or now) - (t_gen_first or t_start), 3)
        distill_s = round((t_distill_end or now) - (t_gen_end or now), 3)
        audio_delay_s = round((t_tts_first or now) - (t_distill_end or now), 3)
        total_to_audio_s = round((t_tts_first or now) - t_start, 3)

        metrics = {
            "ttft_s": ttft_s,
            "gen_s": gen_s,
            "distill_s": distill_s,
            "audio_delay_s": audio_delay_s,
            "total_to_audio_s": total_to_audio_s,
        }
        self.last_turn_timings = metrics
        with contextlib.suppress(Exception):
            from core.logging_config import get_logger

            get_logger("voice").info(
                "voice turn timing: ttft=%.2fs gen=%.2fs distill=%.2fs audio_delay=%.2fs total=%.2fs",
                ttft_s,
                gen_s,
                distill_s,
                audio_delay_s,
                total_to_audio_s,
            )
        return metrics

    async def handle(self, text: str) -> None:
        if not text:
            return

        command = self._match_voice_command(text)
        if command and await self._handle_voice_command(command):
            return

        # Taste corrections ("don't call me sir", "too long") are consumed
        # here: saved to voice prefs, acknowledged aloud, never sent to the
        # brain as a question.
        try:
            prefs = getattr(self.distiller, "prefs", None)
            ack = prefs.note_correction(text) if prefs is not None else None
        except Exception:
            ack = None
        if ack:
            self.console.print(f"[dim]you › {text}[/]")
            await self._say_logged(ack)
            return

        if self.pending_full is not None and is_confirmation(text):
            remainder = self.pending_full
            self.pending_full = None
            self.console.print(f"[dim]you › {text}[/]")
            self.console.print("[dim]‹reading remainder›[/]")
            await self.speaker.say(remainder)
            return

        self.pending_full = None
        self.console.print(f"[dim]you › {text}[/]")
        try:
            await self._stream_and_speak(text)
        except Exception as exc:
            self.console.print(f"[red]voice turn failed:[/] {exc}")

    async def run_push_to_talk(self) -> None:
        self.console.print(
            "[cyan]voice 2.0 — VAD recording, streaming speech, say 'stop' to interrupt[/]"
        )
        # Whisper first-load can take a while (download+convert) — do it
        # up-front with feedback so the mode never looks frozen.
        if self.transcriber._model is None:
            self.console.print("[dim]loading speech model (first run can take a minute)...[/]")
            await asyncio.to_thread(self.transcriber._load)
            self.console.print("[dim]speech model ready[/]")
        await self.speaker.say("Voice mode engaged, sir.")
        while self.active:
            try:
                self.console.print("[dim]listening... (say 'exit voice' to leave)[/]")
                text = await asyncio.to_thread(self._listen_and_transcribe)
            except KeyboardInterrupt:
                break
            if not text:
                self.console.print("[dim](no speech detected)[/]")
                continue
            if text.strip().lower().rstrip(".!") in (
                "exit voice",
                "stop voice",
                "stop listening",
                "voice off",
            ):
                break
            try:
                await self.handle(text)
            except KeyboardInterrupt:
                self.playback.barge_in()
                break
        await self.speaker.say("Going quiet, sir. Type when you need me.")
        self.console.print("[cyan]back to text mode[/]")

    async def run_wake_mode(self, should_stop=None) -> None:
        self.console.print("[cyan]wake mode 2.0 — say 'Hey Jarvis'[/]")
        loop = asyncio.get_running_loop()
        triggered = asyncio.Event()

        def on_wake() -> None:
            loop.call_soon_threadsafe(triggered.set)

        listener_task = asyncio.create_task(
            asyncio.to_thread(
                WakeListener(threshold=self.wake_threshold).listen, on_wake, should_stop
            )
        )
        try:
            while not (should_stop and should_stop()):
                try:
                    await asyncio.wait_for(triggered.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                triggered.clear()
                self.console.print("[cyan]listening...[/]")
                await self.speaker.say("Yes, sir?")
                text = await asyncio.to_thread(self._listen_and_transcribe)
                await self.handle(text)
        finally:
            listener_task.cancel()

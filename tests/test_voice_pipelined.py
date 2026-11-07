import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from core.config import Config, ProviderConfig, WatchdogConfig
from core.orchestrator import VOICE_SPOKEN_INSTRUCTION, Orchestrator
from voice.session import VoiceSession
from voice.tts import Speaker2


def make_cfg(tmp_path) -> Config:
    p = ProviderConfig(
        name="brain",
        base_url="https://brain.test/v1",
        model="brain-model",
        api_key="k",
        priority=0,
        supports_tools=True,
    )
    return Config(
        jarvis_name="JARVIS TEST",
        system_prompt="test",
        watchdog=WatchdogConfig(
            first_token_timeout_s=0.5, connect_timeout_s=2.0, read_timeout_s=5.0
        ),
        providers=[p],
        db_path=tmp_path / "usage.db",
    )


def test_voice_spoken_instruction_constant():
    assert "<spoken>" in VOICE_SPOKEN_INSTRUCTION
    assert "</spoken>" in VOICE_SPOKEN_INSTRUCTION


@pytest.mark.asyncio
async def test_orchestrator_injects_voice_spoken_instruction(tmp_path):
    cfg = make_cfg(tmp_path)
    orch = Orchestrator(cfg)

    captured_msgs = []

    async def fake_chat(msgs, **kw):
        captured_msgs.extend(msgs)
        yield "assistant", "Hello world"

    orch.router.chat = fake_chat

    # Call with surface="voice"
    async for _ in orch.process("ping", surface="voice"):
        pass

    assert any(
        "<spoken>" in (m.get("content") or "") and "</spoken>" in (m.get("content") or "")
        for m in captured_msgs
        if m.get("role") == "system"
    )


@pytest.mark.asyncio
async def test_session_extracts_spoken_tag_bypassing_distiller():
    mock_orch = MagicMock()

    async def mock_process(text, surface="voice"):
        yield "assistant", "Detailed explanation on screen.\n<spoken>Short spoken line.</spoken>"

    mock_orch.process = mock_process

    session = VoiceSession(orch=mock_orch, console=Console(quiet=True), input_device=None)
    session.speaker = MagicMock()
    session.speaker.say_pipelined = AsyncMock(return_value=True)
    session.distiller = MagicMock()
    session.distiller.prefs = None  # Ensure no prefs correction intercept
    session.distiller.threshold = 50
    session.distiller.spoken_version = AsyncMock()

    await session.handle("tell me about something")

    # Spoken version was extracted from tag, distiller was not called
    session.distiller.spoken_version.assert_not_called()
    session.speaker.say_pipelined.assert_called_once()
    assert session.speaker.say_pipelined.call_args[0][0] == "Short spoken line."
    assert session.last_turn_timings is not None
    assert "ttft_s" in session.last_turn_timings
    assert "total_to_audio_s" in session.last_turn_timings


@pytest.mark.asyncio
async def test_session_falls_back_to_distiller_without_tag():
    mock_orch = MagicMock()

    async def mock_process(text, surface="voice"):
        yield "assistant", "A " * 60  # Long reply without <spoken> tag

    mock_orch.process = mock_process

    session = VoiceSession(orch=mock_orch, console=Console(quiet=True), input_device=None)
    session.speaker = MagicMock()
    session.speaker.say_pipelined = AsyncMock(return_value=True)
    session.distiller = MagicMock()
    session.distiller.prefs = None  # Ensure no prefs correction intercept
    session.distiller.threshold = 50
    session.distiller.spoken_version = AsyncMock(return_value="Distilled fallback.")

    await session.handle("tell me about something long")

    # Distiller was invoked as fallback
    session.distiller.spoken_version.assert_called_once()
    session.speaker.say_pipelined.assert_called_once()
    assert session.speaker.say_pipelined.call_args[0][0] == "Distilled fallback."


@pytest.mark.asyncio
async def test_say_pipelined_prefetches_and_triggers_on_first_audio():
    speaker = Speaker2(engine="sapi")
    synth_calls = []
    play_calls = []
    first_audio_fired = False

    async def fake_synth(text):
        synth_calls.append(text)
        return "sapi", ("sapi", text)

    async def fake_play(engine, audio_data):
        play_calls.append(audio_data[1])
        await asyncio.sleep(0.01)
        return True

    speaker.synth_audio = fake_synth
    speaker.play_audio = fake_play

    def on_first():
        nonlocal first_audio_fired
        first_audio_fired = True

    text = "First sentence here. Second sentence follows. Third sentence ends."
    ok = await speaker.say_pipelined(text, on_first_audio=on_first)

    assert ok is True
    assert first_audio_fired is True
    assert len(synth_calls) == 3
    assert len(play_calls) == 3
    assert synth_calls[0] == "First sentence here."
    assert play_calls[0] == "First sentence here."

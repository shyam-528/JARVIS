from __future__ import annotations

from core.config import _voice_from
from voice.tts import Speaker2, is_confirmation, sanitize_for_speech, split_reply


def test_sanitize_strips_markdown_symbols():
    assert sanitize_for_speech("**bold** and _under_") == "bold and under"
    assert "#" not in sanitize_for_speech("# Heading")


def test_sanitize_strips_urls_and_emojis():
    out = sanitize_for_speech("see https://example.com/x now 🎉 done")
    assert "https" not in out
    assert "link" in out
    assert "🎉" not in out


def test_sanitize_handles_at_sign():
    assert " at " in sanitize_for_speech("mail me at user@host.com")
    out = sanitize_for_speech("@user hello")
    assert "@" not in out


def test_sanitize_code_block_replaced():
    out = sanitize_for_speech("Here:\n```python\nprint(1)\n```\ndone")
    assert "print" not in out
    assert "Code block omitted" in out
    assert "done" in out


def test_split_short_reply_unchanged():
    spoken, rest = split_reply("Hello sir, systems are online.", max_chars=300)
    assert spoken == "Hello sir, systems are online."
    assert rest == ""


def test_split_long_reply_offers_remainder():
    text = "First sentence is here. " * 20
    spoken, rest = split_reply(text, max_chars=120)
    assert len(spoken) <= 140
    assert rest
    assert spoken.endswith(".")
    assert rest in text


def test_split_exact_fit_no_remainder():
    text = "A" * 300
    spoken, rest = split_reply(text, max_chars=300)
    assert rest == "" and spoken == text


def test_confirmation_words():
    assert is_confirmation("yes")
    assert is_confirmation("Yeah!")
    assert is_confirmation("go ahead")
    assert is_confirmation("yes please read it")
    assert not is_confirmation("no")
    assert not is_confirmation("what is my favorite project")
    assert not is_confirmation("yes" + " word " * 20)


def test_voice_config_defaults_and_overrides():
    v = _voice_from({"voice": {}})
    assert v.whisper_model == "small"
    assert v.tts_voice == "en-GB-SoniaNeural"
    assert v.tts_enabled is True
    assert v.stt_language is None
    assert v.distill_speech is True
    assert v.distill_threshold == 200
    assert v.distill_persona == "butler"
    assert v.distill_address == "sir"
    v2 = _voice_from(
        {
            "voice": {
                "whisper_model": "base",
                "tts_voice": "en-GB-ThomasNeural",
                "tts_enabled": False,
                "stt_language": "en",
                "speak_max_chars": 200,
                "speech": {
                    "distill": False,
                    "distill_threshold": 350,
                    "persona": "plain",
                    "address": "none",
                },
            }
        }
    )
    assert v2.whisper_model == "base"
    assert v2.tts_voice == "en-GB-ThomasNeural"
    assert v2.tts_enabled is False
    assert v2.stt_language == "en"
    assert v2.speak_max_chars == 200
    assert v2.distill_speech is False
    assert v2.distill_threshold == 350
    assert v2.distill_persona == "plain"
    assert v2.distill_address == "none"


async def test_speaker_records_last_error():
    s = Speaker2(enabled=False)
    assert await s.speak_chunk("hello sir") is False
    assert "/tts" in s.last_error
    st = s.status()
    assert st["enabled"] is False and st["last_error"] == s.last_error


async def test_speaker_empty_text_last_error():
    s = Speaker2(enabled=True)
    assert await s.speak_chunk("   ") is False
    assert "nothing speakable" in s.last_error
    assert s.status()["engine_used"] == "none yet"


async def test_speak_reply_falls_back_when_distiller_dies():
    from types import SimpleNamespace

    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    said, printed = [], []

    async def boom(text, user_text=""):
        raise RuntimeError("all brains down")

    vs.distiller = SimpleNamespace(threshold=10, spoken_version=boom)

    async def _say(t):
        said.append(t)
        return True

    vs.speaker = SimpleNamespace(say=_say)
    vs.console = SimpleNamespace(print=lambda *a, **k: printed.append(str(a)))
    vs.speak_max_chars = 300
    long_text = "Word " * 200  # 1000 chars: forces the distill path
    await vs._speak_reply(long_text)
    assert said and said[0].startswith("Word Word")
    assert len(said[0]) <= 300  # verbatim head, not silence
    assert any("verbatim" in p for p in printed)


def test_chain_policy_edge_first_kokoro_explicit_only():
    # User directive: edge primary, gemini next, then others; kokoro must
    # never block the auto chain (15s+/utterance on CPU reads as silence).
    s = Speaker2(enabled=True, engine="auto")
    assert s._pick_engine() == "edge"
    chain = s._engine_chain("edge")
    assert chain[0] == "edge"
    assert "gemini" in chain and chain[-1] == "sapi"
    assert "kokoro" not in chain


def test_chain_policy_explicit_kokoro_pin_respected():
    s = Speaker2(enabled=True, engine="kokoro")
    assert s._pick_engine() == "kokoro"
    assert s._engine_chain("kokoro")[0] == "kokoro"


def test_chain_policy_dead_edge_falls_to_gemini():
    s = Speaker2(enabled=True, engine="auto")
    s._mark_engine_dead("edge")
    assert s._pick_engine() == "gemini"
    assert s._engine_chain("gemini")[0] == "gemini"


async def test_voice_doctor_reports_chain():
    from core.tools import ToolBox
    from skills.voice_doctor import register_voice_doctor

    box = ToolBox()
    register_voice_doctor(box)
    out = await box.execute("voice_doctor", "{}")
    assert "Config:" in out and "Edge" in out

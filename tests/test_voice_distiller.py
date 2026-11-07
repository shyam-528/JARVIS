from __future__ import annotations

from core.config import Config
from voice.distiller import _truncate_sentences
from voice.tts import sanitize_for_speech, split_sentences


def make_cfg(tmp_path):
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"

    class V:
        distill_speech = True
        distill_threshold = 200

    cfg.voice = V()
    return cfg


class FakeRouter:
    """Returns a fixed distilled answer for any prompt."""

    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0
        self.last_messages = None

    async def chat(self, messages, tools=None, task_type=None):
        self.calls += 1
        self.last_messages = messages
        yield "groq-test", self.answer


class BrokenRouter:
    async def chat(self, messages, tools=None, task_type=None):
        raise RuntimeError("all providers down")
        yield  # pragma: no cover


def make_distiller(router, tmp_path, **kw):
    """Distiller with taste memory in tmp (never touches the real prefs)."""
    from voice.distiller import SpeechDistiller

    return SpeechDistiller(router, make_cfg(tmp_path), prefs_path=tmp_path / "vp.json", **kw)


# ---------- short replies pass through verbatim ----------


async def test_short_reply_spoken_verbatim(tmp_path):
    d = make_distiller(FakeRouter("x"), tmp_path)
    short = "All systems nominal, sir."
    assert await d.spoken_version(short) == short


# ---------- long replies get distilled ----------


async def test_long_reply_distilled_to_butler_line(tmp_path):
    long_reply = (
        "The deployment completed successfully across all three environments. "
        "Staging passed all forty-two tests without failures. Production rolled out "
        "at fourteen hundred hours with zero downtime. The only warning was a "
        "memory usage spike on the cache server reaching eighty-five percent, which "
        "normalized after the garbage collector ran. Full logs are attached below "
        "with the complete diff of every configuration change made during this release."
    )
    d = make_distiller(
        FakeRouter(
            "Deployment succeeded across all three environments, sir. Only a brief memory spike on the cache server."
        ),
        tmp_path,
    )
    spoken = await d.spoken_version(long_reply, user_text="how did the deploy go")
    assert "Deployment succeeded" in spoken
    assert len(spoken) < 200
    assert d.router.calls == 1


# ---------- fallbacks ----------


async def test_fallback_truncation_when_llm_broken(tmp_path):
    long_reply = " ".join(f"Sentence number {i} is here." for i in range(30))
    d = make_distiller(BrokenRouter(), tmp_path)
    spoken = await d.spoken_version(long_reply)
    # No crash; truncated fallback under budget
    assert spoken and len(spoken) <= 300
    assert spoken.rstrip().endswith(".")


async def test_distiller_stops_retrying_after_three_failures(tmp_path):
    long_reply = " ".join(f"Sentence {i}." for i in range(40))
    d = make_distiller(BrokenRouter(), tmp_path)
    for _ in range(4):
        await d.spoken_version(long_reply)
    assert d._broken is True, "should give up after 3 failures"


async def test_disabled_distiller_returns_verbatim_long_text(tmp_path):
    d = make_distiller(FakeRouter("never called"), tmp_path)
    d.enabled = False
    long_reply = "A very long reply. " * 50
    out = await d.spoken_version(long_reply)
    assert out == " ".join(long_reply.split())
    assert d.router.calls == 0


# ---------- helpers ----------


def test_truncate_sentences_respects_budget():
    text = "One two three. Four five six. Seven eight nine."
    head, rest = _truncate_sentences(text, 20)
    assert len(head) <= 25
    assert head.endswith(".")
    assert rest


def test_sanitize_strips_markdown_for_speech():
    assert "**bold**" not in sanitize_for_speech("**bold** text")
    assert "```" not in sanitize_for_speech("```py\nx=1\n```")


def test_split_sentences():
    parts = split_sentences("Hello there. How are you? Fine!")
    assert len(parts) == 3


# ---------- defect #3: varied, sir-aware pointer ----------


def test_detail_offer_varies():
    import random

    from voice.distiller import OFFERS_WITH_SIR, detail_offer

    seen = {detail_offer("all systems nominal", random.Random(n)) for n in range(30)}
    assert len(seen) > 1, "pointer never varies"
    assert seen <= set(OFFERS_WITH_SIR)


def test_detail_offer_skips_second_sir():
    from voice.distiller import OFFERS_PLAIN, detail_offer

    for _ in range(20):
        offer = detail_offer("All systems nominal, sir")
        assert offer in OFFERS_PLAIN
    combined = "All systems nominal, sir" + detail_offer("All systems nominal, sir")
    assert combined.lower().count("sir") == 1


def test_detail_offer_never_raises():
    from voice.distiller import detail_offer

    assert isinstance(detail_offer(""), str)
    assert isinstance(detail_offer(None), str)

    class Boom:
        def choice(self, _):
            raise RuntimeError("nope")

    assert isinstance(detail_offer("hello", Boom()), str)


async def test_speak_reply_speaks_distiller_line_verbatim(tmp_path):
    """The distiller's output is final: session speaks it as-is (the model
    writes its own fresh pointer now; no fixed-pool append in session)."""
    from types import SimpleNamespace

    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    said = []

    async def fixed_distill(text, user_text="", has_screen=True):
        return "The backup finished cleanly. The log tail is on screen."

    vs.distiller = SimpleNamespace(threshold=10, spoken_version=fixed_distill)

    async def _say(t):
        said.append(t)
        return True

    vs.speaker = SimpleNamespace(say=_say)
    vs.console = SimpleNamespace(print=lambda *a, **k: None)
    vs.speak_max_chars = 30
    await vs._speak_reply("Word " * 200)
    assert said == ["The backup finished cleanly. The log tail is on screen."]
    assert vs.pending_full is not None  # full text still kept for "tell me more"


async def test_speak_reply_no_screen_skips_pointer(tmp_path):
    from types import SimpleNamespace

    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    said = []

    async def fixed_distill(text, user_text="", has_screen=True):
        return "The backup finished cleanly"

    vs.distiller = SimpleNamespace(threshold=10, spoken_version=fixed_distill)

    async def _say(t):
        said.append(t)
        return True

    vs.speaker = SimpleNamespace(say=_say)
    vs.console = SimpleNamespace(print=lambda *a, **k: None)
    vs.speak_max_chars = 30
    await vs._speak_reply("Word " * 200, has_screen=False)
    assert said == ["The backup finished cleanly"]
    assert "screen" not in said[0]
    assert vs.pending_full is not None  # full text still kept for "tell me more"


async def test_speak_reply_no_double_sir(tmp_path):
    from types import SimpleNamespace

    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    said = []

    async def sir_distill(text, user_text="", has_screen=True):
        return "Done, sir"

    vs.distiller = SimpleNamespace(threshold=10, spoken_version=sir_distill)

    async def _say(t):
        said.append(t)
        return True

    vs.speaker = SimpleNamespace(say=_say)
    vs.console = SimpleNamespace(print=lambda *a, **k: None)
    vs.speak_max_chars = 5
    await vs._speak_reply("Word " * 200)
    assert said[0].lower().count("sir") == 1


# ---------- adaptive sizing: speech follows the question ----------


def test_speech_level_follows_question(tmp_path):
    d = make_distiller(FakeRouter("x"), tmp_path)
    assert d.speech_level("what time is it") == "short"
    assert (
        d.speech_level("how does the deployment pipeline handle database migrations in production")
        == "detailed"
    )


def test_brevity_taste_caps_and_floors(tmp_path):
    d = make_distiller(FakeRouter("x"), tmp_path)
    d.prefs.brevity = "brief"
    assert (
        d.speech_level(
            "explain the full tradeoffs of SQL versus NoSQL for our production workload in detail"
        )
        == "short"
    )
    d.prefs.brevity = "full"
    assert d.speech_level("what time is it") == "normal"


async def test_simple_question_gets_headline_budget(tmp_path):
    long_answer = (
        "First the service restarted cleanly across every node in the fleet. "
        "Second the queue drained within ninety seconds without losing a message. "
        "Third latency returned to baseline before the health check finished. "
        "Fourth the on-call engineer signed off on the incident report."
    )
    assert len(long_answer) > 200
    router = FakeRouter(long_answer)  # model ignores the budget: cap must enforce
    d = make_distiller(router, tmp_path)
    spoken = await d.spoken_version(long_answer, user_text="is it fixed")
    assert len(spoken) <= 160, "simple question must cap speech at the headline budget"
    assert router.calls == 1


async def test_prompt_carries_budget_and_facts_rule(tmp_path):
    router = FakeRouter("Done.")
    d = make_distiller(router, tmp_path)
    long_reply = "The migration finished. " * 30
    await d.spoken_version(
        long_reply, user_text="walk me through the production database migration plan in detail"
    )
    msgs = router.last_messages
    assert msgs
    system = msgs[0]["content"]
    assert "3 sentence" in system
    assert "70 words" in system
    assert "Never drop numbers, names, or times" in system


async def test_no_screen_forbids_pointer(tmp_path):
    router = FakeRouter("Done.")
    d = make_distiller(router, tmp_path)
    await d.spoken_version("The migration finished. " * 30, has_screen=False)
    msgs = router.last_messages
    assert msgs
    system = msgs[0]["content"]
    assert "There is no screen" in system


async def test_no_repeat_guard_references_last_line(tmp_path):
    router = FakeRouter("Fresh line.")
    d = make_distiller(router, tmp_path)
    d.prefs.last_spoken = "The full detail is on screen, sir."
    await d.spoken_version("The migration finished. " * 30, user_text="status update please")
    msgs = router.last_messages
    assert msgs
    system = msgs[0]["content"]
    assert "Never reuse this phrasing" in system
    assert "The full detail is on screen, sir." in system


async def test_last_spoken_remembered(tmp_path):
    d = make_distiller(FakeRouter("All green."), tmp_path)
    await d.spoken_version("The migration finished. " * 30, user_text="status")
    assert d.prefs.last_spoken == "All green."


async def test_address_none_reaches_prompt(tmp_path):
    router = FakeRouter("Done.")
    d = make_distiller(router, tmp_path)
    d.prefs.address = "none"
    await d.spoken_version("The migration finished. " * 30)
    msgs = router.last_messages
    assert msgs
    system = msgs[0]["content"]
    assert 'Never say "sir"' in system


async def test_fallback_pointer_only_on_outage_with_screen(tmp_path):
    from voice.distiller import OFFERS_PLAIN, OFFERS_WITH_SIR

    long_reply = " ".join(f"Sentence number {i} is here." for i in range(30))
    d = make_distiller(BrokenRouter(), tmp_path)
    spoken = await d.spoken_version(long_reply, has_screen=True)
    assert any(spoken.endswith(o) for o in OFFERS_WITH_SIR + OFFERS_PLAIN)
    d2 = make_distiller(BrokenRouter(), tmp_path)
    spoken2 = await d2.spoken_version(long_reply, has_screen=False)
    assert "screen" not in spoken2.lower()


async def test_handle_consumes_taste_correction(tmp_path):
    from types import SimpleNamespace

    from voice.distiller import SpeechDistiller
    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    said, printed = [], []
    d = SpeechDistiller(FakeRouter("never"), make_cfg(tmp_path), prefs_path=tmp_path / "vp.json")
    vs.distiller = d
    vs.pending_full = None

    async def _say(t):
        said.append(t)
        return True

    vs.speaker = SimpleNamespace(say=_say, _engine_used="edge", last_error="")
    vs.console = SimpleNamespace(print=lambda *a, **k: printed.append(str(a)))
    await vs.handle("don't call me sir")
    assert said and "sir" in said[0].lower()
    assert d.prefs.address == "none"


# ---------- single-shot tag: filter + instruction ----------


def test_tag_filter_hides_one_chunk():
    from voice.distiller import SpokenTagFilter

    f = SpokenTagFilter()
    out = f.feed("Full answer here. <spoken>Short line.</spoken> Done.")
    assert out == "Full answer here.  Done."
    assert f.flush() == ""


def test_tag_filter_split_across_chunks():
    from voice.distiller import SpokenTagFilter

    f = SpokenTagFilter()
    parts = ["Full ans", "wer. <spo", "ken>Short li", "ne.</spoken> Tail."]
    shown = "".join(f.feed(p) for p in parts) + f.flush()
    assert shown == "Full answer.  Tail."
    assert "spoken" not in shown.lower().replace("answer", "")


def test_tag_filter_unclosed_flushes_nothing_lost():
    from voice.distiller import SpokenTagFilter

    f = SpokenTagFilter()
    shown = f.feed("Visible. <spoken>never closed") + f.flush()
    assert shown == "Visible. never closed"


def test_tag_filter_no_tag_passes_through():
    from voice.distiller import SpokenTagFilter

    f = SpokenTagFilter()
    assert f.feed("Just <b>text</b>.") == "Just <b>text</b>."
    assert f.flush() == ""


def test_spoken_instruction_carries_budget_and_taste():
    from voice.distiller import spoken_instruction

    simple = spoken_instruction("what time is it", address="none", brevity="auto")
    assert "1 sentence" in simple
    assert 'Never say "sir"' in simple
    complex_q = spoken_instruction(
        "walk me through the production database migration plan in detail",
        address="sir",
        brevity="auto",
    )
    assert "3 sentence" in complex_q
    assert "70 words" in complex_q
    brief = spoken_instruction(
        "explain the full tradeoffs of SQL versus NoSQL for production in detail",
        brevity="brief",
    )
    assert "1 sentence" in brief


def test_spoken_instruction_never_raises():
    from voice.distiller import spoken_instruction

    assert isinstance(spoken_instruction(None, address=None, brevity=None), str)  # type: ignore[arg-type]


async def test_speak_reply_tag_path_speaks_tag_and_keeps_clean_full(tmp_path):
    from types import SimpleNamespace

    from voice.session import VoiceSession

    vs = VoiceSession.__new__(VoiceSession)
    said = []
    vs.distiller = SimpleNamespace(threshold=10)  # unused on the tag path

    async def _say(t):
        said.append(t)
        return True

    vs.speaker = SimpleNamespace(say=_say)
    vs.console = SimpleNamespace(print=lambda *a, **k: None)
    vs.speak_max_chars = 30
    text = "Word " * 200 + " <spoken>Backup finished cleanly.</spoken>"
    await vs._speak_reply(text)
    assert said == ["Backup finished cleanly."]
    assert vs.pending_full is not None
    assert "<spoken>" not in vs.pending_full

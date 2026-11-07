"""Regression tests for core/commands.py (extracted from jarvis.py).

Covers the data-driven slash-command table: match modes (exact/word/prefix),
ordering (first match wins, e.g. /agents beats /agent), dispatch_slash
fall-through semantics, the shared morning/evening routine runner, and the
voice-session injection used by /voice and /tts.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

import core.commands as cmds
from core.commands import SlashCommand, dispatch_slash, get_help, resolve_alias


class FakeConsole:
    def __init__(self):
        self.lines = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    @property
    def text(self):
        return "\n".join(self.lines)


class FakeToolbox:
    def __init__(self):
        self.executed = []

    async def execute(self, name, payload):
        self.executed.append((name, json.loads(payload)))
        return f"out:{name}"

    def names(self):
        return ["alpha", "beta"]


class FakeOrch:
    def __init__(self):
        self.toolbox = FakeToolbox()
        self.reset_called = False
        self.voice_alerts = None

    def reset(self):
        self.reset_called = True

    def start_new_session(self):
        return "sid-1"

    def resume_session(self, sid):
        return ["m1", "m2"]


@pytest.fixture
def fake_console(monkeypatch):
    con = FakeConsole()
    monkeypatch.setattr(cmds, "console", con)
    return con


@pytest.fixture
def orch():
    return FakeOrch()


def first_match(cmd):
    """Name of the first registry entry matching cmd (REPL semantics)."""
    return next((e.name for e in cmds.SLASH_COMMANDS if e.matches(cmd)), None)


# ── match modes ──


def test_match_modes():
    assert SlashCommand("/chain", None, "exact").matches("/chain")
    assert not SlashCommand("/chain", None, "exact").matches("/chain x")
    word = SlashCommand("/rule", None, "word")
    assert word.matches("/rule") and word.matches("/rule list")
    assert not word.matches("/rulebook")
    assert SlashCommand("/bg", None, "prefix").matches("/bgmode on")
    assert SlashCommand("/forget ", None, "prefix").matches("/forget stuff")
    assert not SlashCommand("/forget ", None, "prefix").matches("/forget")


# ── ordering: first match wins, mirroring the old if/elif chain ──


def test_order_agents_beats_agent_prefix():
    assert first_match("/agents") == "/agents"
    assert first_match("/agents extra") == "/agent"  # same as the old chain


def test_order_plugins_beats_plugin_prefix():
    assert first_match("/plugins") == "/plugins"
    assert first_match("/plugin install x") == "/plugin "


def test_order_bg_shadows_bgmode_preexisting():
    # /bgmode was already unreachable (the /bg prefix elif came first).
    # The registry preserves that order (and the shadow) verbatim.
    assert first_match("/bgmode on") == "/bg"
    assert first_match("/bg do the thing") == "/bg"


def test_morning_evening_entries_present():
    assert first_match("/morning") == "/morning"
    assert first_match("/evening") == "/evening"


# ── dispatch fall-through semantics ──


async def test_plain_text_falls_through(orch, fake_console):
    assert await dispatch_slash(orch, "hello there") is False
    assert fake_console.lines == []


async def test_unknown_slash_prints_help_and_is_consumed(orch, fake_console):
    assert await dispatch_slash(orch, "/nosuch") is True
    assert "Available commands" in fake_console.text


async def test_bare_forget_falls_to_help(orch, fake_console):
    # Old predicate was startswith("/forget ") — bare "/forget" printed help.
    assert await dispatch_slash(orch, "/forget") is True
    assert "Available commands" in fake_console.text
    assert orch.toolbox.executed == []


# ── simple handlers ──


async def test_reset(orch, fake_console):
    assert await dispatch_slash(orch, "/reset") is True
    assert orch.reset_called
    assert "session memory wiped" in fake_console.text


async def test_new_and_resume(orch, fake_console):
    assert await dispatch_slash(orch, "/new") is True
    assert "sid-1" in fake_console.text
    assert await dispatch_slash(orch, "/resume abc123") is True
    assert "Resumed 2 messages from session abc123" in fake_console.text


async def test_tools(orch, fake_console):
    assert await dispatch_slash(orch, "/tools") is True
    assert "alpha, beta" in fake_console.text


# ── toolbox-backed handlers ──


async def test_forget_payload(orch, fake_console):
    assert await dispatch_slash(orch, "/forget doctor who") is True
    assert orch.toolbox.executed == [("forget", {"query": "doctor who"})]


async def test_consolidate(orch, fake_console):
    assert await dispatch_slash(orch, "/consolidate") is True
    assert orch.toolbox.executed == [("memory_consolidate", {})]
    assert "out:memory_consolidate" in fake_console.text


async def test_init_defaults_to_cwd(orch, fake_console):
    assert await dispatch_slash(orch, "/init") is True
    assert orch.toolbox.executed == [("dev_init", {"path": "."})]


async def test_test_payload_parsing(orch, fake_console):
    await dispatch_slash(orch, "/test")
    await dispatch_slash(orch, "/test tests/")
    await dispatch_slash(orch, "/test tests/ pytest -x")
    assert orch.toolbox.executed == [
        ("dev_test", {}),
        ("dev_test", {"path": "tests/"}),
        ("dev_test", {"path": "tests/", "cmd": "pytest -x"}),
    ]


async def test_pr_partition(orch, fake_console):
    await dispatch_slash(orch, "/pr my title | my body")
    assert orch.toolbox.executed == [("dev_pr", {"title": "my title", "body": "my body"})]
    orch.toolbox.executed.clear()
    await dispatch_slash(orch, "/pr")
    assert orch.toolbox.executed == []
    assert "Usage: /pr" in fake_console.text


async def test_triage_sort_vs_report(orch, fake_console):
    await dispatch_slash(orch, "/triage")
    await dispatch_slash(orch, "/triage sort")
    assert orch.toolbox.executed == [
        ("inbox_report", {}),
        ("inbox_sort", {"dry_run": False}),
    ]


async def test_wa_sendto_parses_number_and_text(orch, fake_console):
    await dispatch_slash(orch, "/wa sendto +49 151 23456789 hello there")
    assert orch.toolbox.executed == [
        ("wa_send", {"chat": "+49 151 23456789", "text": "hello there"})
    ]


async def test_doctor(orch, fake_console):
    assert await dispatch_slash(orch, "/doctor") is True
    assert orch.toolbox.executed == [("voice_doctor", {})]


# ── routine dedup: /morning + /evening share run_routine_tool ──


async def test_morning_evening_map_to_routine_tools(orch, fake_console):
    assert await dispatch_slash(orch, "/morning") is True
    assert await dispatch_slash(orch, "/evening") is True
    assert orch.toolbox.executed == [("morning_start", {}), ("evening_wrap", {})]


async def test_routine_error_is_wrapped(orch, fake_console):
    class BoomToolbox(FakeToolbox):
        async def execute(self, name, payload):
            raise RuntimeError("kaput")

    orch.toolbox = BoomToolbox()
    assert await dispatch_slash(orch, "/morning") is True
    assert "Routine error: kaput" in fake_console.text


async def test_nl_shortcut_uses_shared_runner(orch, fake_console, monkeypatch):
    from skills.nl_shortcuts import try_routine_shortcut

    calls = []

    async def spy(o, con, tool):
        calls.append((o, tool))

    monkeypatch.setattr(cmds, "run_routine_tool", spy)
    assert await try_routine_shortcut(orch, "starting work", fake_console) is True
    assert calls == [(orch, "morning_start")]


# ── voice-session injection ──


@pytest.fixture
def voice_getter():
    saved = cmds._VOICE_SESSION_GETTER
    yield lambda fn: cmds.set_voice_session_getter(fn)
    cmds.set_voice_session_getter(saved)


class FakeSpeaker:
    def __init__(self):
        self.enabled = False
        self.kokoro_voice = "kf1"

    def _pick_engine(self):
        return "kokoro"


class FakeVoiceSession:
    def __init__(self):
        self.speaker = FakeSpeaker()
        self.ptt_ran = False

    async def run_push_to_talk(self):
        self.ptt_ran = True


async def test_voice_success_is_consumed(orch, fake_console, voice_getter):
    vs = FakeVoiceSession()
    voice_getter(lambda: vs)
    assert await dispatch_slash(orch, "/voice") is True
    assert vs.ptt_ran


async def test_voice_missing_deps_falls_through(orch, fake_console, voice_getter):
    def boom():
        raise ImportError("no sounddevice")

    voice_getter(boom)
    assert await dispatch_slash(orch, "/voice") is False
    assert "voice deps missing" in fake_console.text


async def test_tts_toggle(orch, fake_console, voice_getter):
    vs = FakeVoiceSession()
    voice_getter(lambda: vs)
    assert await dispatch_slash(orch, "/tts on") is True
    assert vs.speaker.enabled is True
    assert "tts is ON" in fake_console.text
    assert "kokoro/kf1" in fake_console.text


# ── metadata helpers ──


def test_resolve_alias():
    assert resolve_alias("/s") == "/stats"
    assert resolve_alias("/x") == "/exit"
    assert resolve_alias("/unchanged") == "/unchanged"


def test_get_help_lists_core_commands():
    text = get_help()
    assert "Available commands" in text
    assert "/chain" in text and "/memory" in text


def test_jarvis_reexports_and_no_leftover_handlers():
    import os

    os.environ["JARVIS_VENV_RELAUNCHED"] = "1"
    import jarvis

    assert jarvis.dispatch_slash is cmds.dispatch_slash
    assert jarvis.resolve_alias is cmds.resolve_alias
    assert not hasattr(jarvis, "_do_rule")
    assert not hasattr(jarvis, "_print_memory")


# ── handler arity tolerance (every slash command must survive dispatch) ──


def test_call_handler_zero_one_two_args():
    from core.commands import _call_handler

    seen = []

    def h0():
        seen.append(())
        return "zero"

    def h1(orch):
        seen.append((orch,))
        return "one"

    def h2(orch, cmd):
        seen.append((orch, cmd))
        return "two"

    async def h2a(orch, cmd):
        return ("two-async", orch, cmd)

    def hstar(*args):
        return ("star", len(args))

    orch, cmd = object(), "/x"
    assert _call_handler(h0, orch, cmd) == "zero"
    assert _call_handler(h1, orch, cmd) == "one"
    assert _call_handler(h2, orch, cmd) == "two"
    import inspect

    assert inspect.isawaitable(_call_handler(h2a, orch, cmd))
    assert _call_handler(hstar, orch, cmd) == ("star", 2)
    assert seen == [(), (orch,), (orch, cmd)]


async def test_legacy_zero_arg_handler_dispatched(orch, fake_console, monkeypatch):
    """print_keys() takes no args — dispatch must still deliver (orch, cmd)
    gracefully instead of TypeErroring the session to death."""
    calls = []

    def legacy():
        calls.append(True)
        return True

    monkeypatch.setattr(
        cmds, "SLASH_COMMANDS", [SlashCommand("/legacy", legacy)] + list(cmds.SLASH_COMMANDS)
    )
    assert await dispatch_slash(orch, "/legacy") is True
    assert calls == [True]


async def test_unknown_slash_help_printed_without_markup(monkeypatch):
    """get_help() contains literal [/...] fragments — printing it WITH markup
    raised MarkupError and killed the session. Must print markup-free."""
    import core.commands as cmds_mod

    printed = []

    class StrictConsole:
        def print(self, *args, **kwargs):
            text = " ".join(str(a) for a in args)
            if kwargs.get("markup", True) and "[/" in text:
                raise ValueError("rich MarkupError simulation: closing tag with markup on")
            printed.append((text, kwargs))

    monkeypatch.setattr(cmds_mod, "console", StrictConsole())
    assert await dispatch_slash(object(), "/definitely-not-a-command") is True
    assert printed and printed[0][1].get("markup") is False

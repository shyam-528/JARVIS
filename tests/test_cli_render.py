"""CLI stage-1 polish: markdown/diff detection, renderers, buffered chat_turn."""

from __future__ import annotations

from rich.console import Console

from cli_render import (
    looks_like_diff,
    looks_like_markdown,
    print_help_table,
    print_services,
    render_reply,
    render_tool_chunk,
)


def test_markdown_detected():
    assert looks_like_markdown("# Title\nbody") is True
    assert looks_like_markdown("Hello **world**") is True
    assert looks_like_markdown("```py\nx = 1\n```") is True
    assert looks_like_markdown("- one\n- two") is True
    assert looks_like_markdown("1. one\n2. two") is True
    assert looks_like_markdown("| a | b |\n| c | d |") is True


def test_markdown_rejected():
    assert looks_like_markdown("All systems nominal, sir.") is False
    assert looks_like_markdown("") is False
    assert looks_like_markdown(None) is False
    assert looks_like_markdown("a*b") is False


def test_diff_detected():
    diff = "--- a.py\n+++ b.py\n@@ -1 +1 @@\n-old\n+new"
    assert looks_like_diff(diff) is True
    assert looks_like_diff("diff --git a/x b/x") is True
    assert looks_like_diff("just a log line") is False
    assert looks_like_diff(None) is False


def test_render_reply_markdown_strips_markup():
    console = Console(record=True, width=100)
    render_reply(console, "Hello **world**")
    out = console.export_text()
    assert "Hello world" in out
    assert "**" not in out


def test_render_reply_plain_verbatim():
    console = Console(record=True, width=100)
    render_reply(console, "All systems nominal, sir.")
    assert "All systems nominal, sir." in console.export_text()


def test_render_tool_chunk_diff_and_plain():
    console = Console(record=True, width=100)
    render_tool_chunk(console, "--- a\n+++ b\n-old\n+new")
    out = console.export_text()  # single read: export_text() clears the buffer
    assert "old" in out and "new" in out
    console2 = Console(record=True, width=100)
    render_tool_chunk(console2, "ran 5 tests ok")
    assert "ran 5 tests ok" in console2.export_text()


async def test_chat_turn_buffers_renders_returns_raw(monkeypatch):
    import jarvis as J

    async def fake_process(text, surface="cli"):
        yield "groq", "Hello **world**. "
        yield "groq", "Done."

    orch = type("O", (), {"process": staticmethod(fake_process)})()
    rec = Console(record=True, width=100)
    monkeypatch.setattr(J, "console", rec)
    out = await J.chat_turn(orch, "hi")
    assert out == "Hello **world**. Done."
    shown = rec.export_text()
    assert "Hello world" in shown
    assert "**" not in shown


async def test_chat_turn_voice_hides_tag_but_returns_it(monkeypatch):
    import jarvis as J

    async def fake_process(text, surface="cli"):
        yield "groq", "Long answer here. <spoken>Short line.</spoken>"

    orch = type("O", (), {"process": staticmethod(fake_process)})()
    rec = Console(record=True, width=100)
    monkeypatch.setattr(J, "console", rec)
    out = await J.chat_turn(orch, "hi", surface="voice")
    assert "<spoken>Short line.</spoken>" in out  # speech path keeps the tag
    shown = rec.export_text()  # single read: export_text() clears the buffer
    assert "<spoken>" not in shown  # screen never shows it
    assert "Long answer here." in shown


def test_help_table_groups_without_drift():
    from core.commands import HELP

    console = Console(record=True, width=100)
    print_help_table(console, HELP)
    out = console.export_text()
    for token in ("Core", "/chain", "Agents", "/bg", "Voice", "/tts", "Aliases", "/c=/chain"):
        assert token in out, f"missing {token}"


def test_help_table_falls_back_to_raw():
    console = Console(record=True, width=100)
    print_help_table(console, "")
    print_help_table(console, None)  # type: ignore[arg-type]
    assert isinstance(console.export_text(), str)


def test_services_table_statuses():
    console = Console(record=True, width=100)
    print_services(
        console,
        [
            ("API + dashboard", True, "http://127.0.0.1:8080"),
            ("WhatsApp bridge", None, "external"),
            ("Telegram", False, "down"),
        ],
    )
    out = console.export_text()
    assert "API + dashboard" in out and "8080" in out
    assert "WhatsApp bridge" in out and "Telegram" in out


def test_services_empty_is_silent():
    console = Console(record=True, width=100)
    print_services(console, [])
    assert console.export_text() == ""

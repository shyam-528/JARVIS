"""Wave 7 contract tests for core.notify.

Contract: delivery functions NEVER raise and NEVER swallow silently — they
return {"success": bool, "error": str} naming the channel and cause, and
send() falls back through configured channels so a single channel outage
cannot silently kill a reminder.
"""

from __future__ import annotations

from core.notify import (
    Channel,
    NotificationConfig,
    NotificationManager,
)


def make_manager(**cfg_kw) -> NotificationManager:
    return NotificationManager(NotificationConfig(**cfg_kw))


async def test_send_with_no_channels_configured_returns_error_dict():
    mgr = make_manager()  # nothing configured
    try:
        result = await mgr.notify(message="hello")
        assert result["success"] is False
        assert "no channels configured" in result["error"]
    finally:
        await mgr.aclose()


async def test_channel_failure_falls_back_and_marks_fallback():
    mgr = make_manager(telegram_bot_token="t", telegram_chat_id="c", ntfy_topic="topic")
    try:

        async def dead_telegram(*a, **k):
            return {"success": False, "error": "telegram error: 502 bad gateway"}

        async def ok_ntfy(*a, **k):
            return {"success": True}

        mgr.sender._send_telegram = dead_telegram
        mgr.sender._send_ntfy = ok_ntfy

        result = await mgr.notify(message="hello", channel=Channel.TELEGRAM)
        assert result["success"] is True
        assert result["fallback_used"] is True
    finally:
        await mgr.aclose()


async def test_all_channels_failing_returns_last_error_naming_channel():
    mgr = make_manager(telegram_bot_token="t", telegram_chat_id="c", ntfy_topic="topic")
    try:

        async def dead_telegram(*a, **k):
            return {"success": False, "error": "telegram error: boom"}

        async def dead_ntfy(*a, **k):
            return {"success": False, "error": "ntfy error: boom"}

        mgr.sender._send_telegram = dead_telegram
        mgr.sender._send_ntfy = dead_ntfy

        result = await mgr.notify(message="hello")
        assert result["success"] is False
        assert "ntfy" in result["error"]  # last channel tried
    finally:
        await mgr.aclose()


def test_render_template_missing_key_returns_error_string_not_raise():
    mgr = make_manager()
    mgr.add_template("greet", "Hello {name}, you have {count} mails")
    result = mgr.render_template("greet", name="sir")  # missing 'count'
    assert "Template error" in result and "count" in result


async def test_notify_with_missing_image_returns_error_dict(tmp_path):
    mgr = make_manager(telegram_bot_token="t", telegram_chat_id="c")
    try:
        result = await mgr.notify_with_image("look", str(tmp_path / "nope.png"))
        assert result["success"] is False
        assert "image not found" in result["error"]
    finally:
        await mgr.aclose()

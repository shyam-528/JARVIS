"""Notification tools: alerts, reminders, briefings, photos, test ping via the
notify manager. Registered only when notifications are configured.
"""

from __future__ import annotations

from core.notify import Channel, NotificationManager, Priority
from core.tools import Tool, ToolBox


def _channel(name: str) -> Channel:
    try:
        return Channel[name.upper()]
    except KeyError:
        return Channel.TELEGRAM


def _dedup_hit(key: str, ttl_h: float = 20.0, dry: bool = False) -> bool:
    """Phase 1 dedup: one briefing/nudge per key per window. File-backed.
    dry=True peeks without recording (caller records on success)."""
    import json as _j
    import time as _t
    from pathlib import Path as _P

    try:
        p = _P("data/notify_dedup.jsonl")
        now = _t.time()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines()[-200:]:
                try:
                    e = _j.loads(line)
                    if e.get("key") == key and (now - float(e.get("ts", 0))) < ttl_h * 3600:
                        return True
                except Exception:
                    continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if not dry:
            with p.open("a", encoding="utf-8") as f:
                f.write(_j.dumps({"key": key, "ts": now}) + "\n")
        return False
    except Exception:
        return False


def register_notify(box: ToolBox, notify_manager: NotificationManager):
    async def notify(
        message: str,
        title: str = "",
        priority: str = "default",
        tags: str = "",
        channel: str = "telegram",
        template: str = "",
    ) -> str:
        """Send a notification. priority: min/low/default/high/max. channel: telegram/ntfy/pushover/webhook."""
        try:
            pri = Priority[priority.upper()]
        except KeyError:
            pri = Priority.DEFAULT
        tags_list = [t.strip() for t in tags.split(",")] if tags else []
        result = await notify_manager.notify(
            message=message,
            title=title,
            priority=pri,
            tags=tags_list,
            channel=_channel(channel),
            template=template,
        )
        if result.get("success"):
            used = result.get("channel", channel)
            note = " (fallback)" if result.get("fallback_used") else ""
            return f"Notification delivered via {used}{note}"
        return f"Failed: {result.get('error', 'Unknown error')}"

    async def notify_alert(message: str, title: str = "ALERT", channel: str = "telegram") -> str:
        """Send a high-priority alert notification."""
        result = await notify_manager.notify(
            message=message,
            title=title,
            priority=Priority.HIGH,
            channel=_channel(channel),
        )
        return "Alert sent" if result.get("success") else f"Failed: {result.get('error')}"

    async def notify_reminder(
        message: str, title: str = "Reminder", channel: str = "telegram"
    ) -> str:
        """Send a reminder to the owner (Telegram by default). Used by scheduled tasks."""
        result = await notify_manager.notify(
            message=message,
            title=title,
            priority=Priority.DEFAULT,
            channel=_channel(channel),
        )
        return "Reminder sent" if result.get("success") else f"Failed: {result.get('error')}"

    async def notify_briefing(kind: str = "morning", channel: str = "telegram") -> str:
        """Phase 1: build + push morning/evening briefing. Deduped per day."""
        from datetime import date as _d

        key = f"briefing:{_d.today().isoformat()}:{kind}"
        if _dedup_hit(key):
            return f"Briefing {kind} already sent today (dedup)."
        try:
            from skills.briefing import generate_briefing

            text = await generate_briefing(kind if kind in ("morning", "evening") else "morning")
        except Exception as exc:
            return f"Failed to build briefing: {exc}"
        result = await notify_manager.notify(
            message=text,
            title="Morning Briefing" if kind != "evening" else "Evening Review",
            priority=Priority.DEFAULT,
            channel=_channel(channel),
        )
        return "Briefing sent" if result.get("success") else f"Failed: {result.get('error')}"

    async def notify_photo(
        image_path: str,
        message: str = "",
        title: str = "JARVIS",
        channel: str = "telegram",
    ) -> str:
        """Send a notification with an image attachment (screenshot, chart, photo)."""
        result = await notify_manager.notify_with_image(
            message=message,
            image_path=image_path,
            title=title,
            priority=Priority.DEFAULT,
            channel=_channel(channel),
        )
        return (
            "Photo notification sent" if result.get("success") else f"Failed: {result.get('error')}"
        )

    async def test_notifications() -> str:
        """Test all configured notification channels."""
        results = []
        for ch in Channel:
            try:
                result = await notify_manager.notify(
                    message="Test notification from JARVIS",
                    title="Test",
                    priority=Priority.DEFAULT,
                    channel=ch,
                )
                status = "✅" if result.get("success") else f"❌ {result.get('error')}"
                results.append(f"{ch.value}: {status}")
            except Exception as e:
                results.append(f"{ch.value}: ❌ {e}")
        return "Notification test results:\n" + "\n".join(results)

    box.register(
        Tool(
            name="notify",
            description="Send a notification to the owner (Telegram by default; ntfy/pushover/webhook as fallback). Supports templates.",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "title": {"type": "string", "default": ""},
                    "priority": {
                        "type": "string",
                        "enum": ["min", "low", "default", "high", "max"],
                        "default": "default",
                    },
                    "tags": {"type": "string", "default": ""},
                    "channel": {
                        "type": "string",
                        "enum": ["telegram", "ntfy", "pushover", "webhook", "whatsapp"],
                        "default": "telegram",
                    },
                    "template": {"type": "string", "default": ""},
                },
                "required": ["message"],
            },
            handler=notify,
        )
    )
    box.register(
        Tool(
            name="notify_alert",
            description="Send a high-priority alert to the owner (Telegram by default)",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "title": {"type": "string", "default": "ALERT"},
                    "channel": {
                        "type": "string",
                        "enum": ["telegram", "ntfy", "pushover", "webhook", "whatsapp"],
                        "default": "telegram",
                    },
                },
                "required": ["message"],
            },
            handler=notify_alert,
        )
    )
    box.register(
        Tool(
            name="notify_reminder",
            description="Send a reminder to the owner (Telegram by default). Used by scheduled tasks.",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "title": {"type": "string", "default": "Reminder"},
                    "channel": {
                        "type": "string",
                        "enum": ["telegram", "ntfy", "pushover", "webhook", "whatsapp"],
                        "default": "telegram",
                    },
                },
                "required": ["message"],
            },
            handler=notify_reminder,
        )
    )
    box.register(
        Tool(
            name="notify_briefing",
            description="Build and push morning/evening briefing (scheduler action). kind: morning|evening.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["morning", "evening"],
                        "default": "morning",
                    },
                    "channel": {
                        "type": "string",
                        "enum": ["telegram", "ntfy", "pushover", "webhook", "whatsapp"],
                        "default": "telegram",
                    },
                },
            },
            handler=notify_briefing,
        )
    )
    box.register(
        Tool(
            name="notify_photo",
            description="Send an image (screenshot/chart/photo file path) to the owner with a caption, via Telegram",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Path to the image file"},
                    "message": {"type": "string", "default": "", "description": "Caption"},
                    "title": {"type": "string", "default": "JARVIS"},
                    "channel": {
                        "type": "string",
                        "enum": ["telegram", "ntfy", "pushover", "webhook", "whatsapp"],
                        "default": "telegram",
                    },
                },
                "required": ["image_path"],
            },
            handler=notify_photo,
        )
    )
    box.register(
        Tool(
            name="notify_test",
            description="Test all configured notification channels",
            parameters={"type": "object", "properties": {}},
            handler=test_notifications,
        )
    )

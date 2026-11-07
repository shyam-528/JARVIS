"""Multi-channel notification manager (Telegram, ntfy, Pushover, WhatsApp,
webhooks) with priorities and templates.

Failure policy (Wave 7): every delivery function returns
``{"success": bool, "error": str}`` — channel failures are NEVER raised or
swallowed silently; the error string always names the channel and cause so
callers (proactive_push, scheduler, skills) can surface it. This module is
the delivery boundary: it must never crash the caller on a channel outage.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import httpx


class Priority(Enum):
    MIN = -2
    LOW = -1
    DEFAULT = 0
    HIGH = 1
    MAX = 2


class Channel(Enum):
    TELEGRAM = "telegram"
    NTFY = "ntfy"
    PUSHOVER = "pushover"
    WEBHOOK = "webhook"
    WHATSAPP = "whatsapp"


@dataclass
class NotificationConfig:
    # ntfy
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_auth_token: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Pushover
    pushover_user_key: str = ""
    pushover_app_token: str = ""

    # Webhook
    webhook_url: str = ""
    webhook_headers: dict = field(default_factory=dict)
    webhook_method: str = "POST"

    # WhatsApp via CallMeBot (send-only gateway, free)
    whatsapp_phone: str = ""
    whatsapp_apikey: str = ""

    # Defaults
    default_priority: Priority = Priority.DEFAULT
    default_channel: Channel = Channel.TELEGRAM
    default_tags: list[str] = field(default_factory=list)


class NotificationSender:
    def __init__(self, config: NotificationConfig):
        self.config = config
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self):
        await self._client.aclose()

    async def send(
        self,
        message: str,
        title: str = "",
        priority: Priority = None,
        tags: list[str] = None,
        channel: Channel = None,
        attachments: list[tuple[str, bytes]] = None,
        click_url: str = "",
    ) -> dict[str, Any]:
        priority = priority or self.config.default_priority
        channel = channel or self.config.default_channel
        tags = tags or self.config.default_tags

        # Try requested channel first, then fall back through all configured
        # channels so a reminder NEVER silently fails (e.g. telegram down -> ntfy).
        order = [channel]
        for fallback in (
            Channel.TELEGRAM,
            Channel.NTFY,
            Channel.PUSHOVER,
            Channel.WEBHOOK,
            Channel.WHATSAPP,
        ):
            if fallback not in order:
                order.append(fallback)

        last_error: dict[str, Any] = {"success": False, "error": "no channels configured"}
        for ch in order:
            if ch == Channel.TELEGRAM:
                if not (self.config.telegram_bot_token and self.config.telegram_chat_id):
                    continue
                last_error = await self._send_telegram(message, title, priority, attachments)
            elif ch == Channel.NTFY:
                if not self.config.ntfy_topic:
                    continue
                last_error = await self._send_ntfy(
                    message, title, priority, tags, attachments, click_url
                )
            elif ch == Channel.PUSHOVER:
                if not (self.config.pushover_user_key and self.config.pushover_app_token):
                    continue
                last_error = await self._send_pushover(
                    message, title, priority, attachments, click_url
                )
            elif ch == Channel.WEBHOOK:
                if not self.config.webhook_url:
                    continue
                last_error = await self._send_webhook(message, title, priority, tags, attachments)
            elif ch == Channel.WHATSAPP:
                if not (self.config.whatsapp_phone and self.config.whatsapp_apikey):
                    continue
                last_error = await self._send_whatsapp(message, title)
            if last_error.get("success"):
                if ch != channel:
                    last_error["fallback_used"] = True
                return last_error
        return last_error

    async def _send_telegram(
        self,
        message: str,
        title: str,
        priority: Priority,
        attachments: list[tuple[str, bytes]] = None,
    ) -> dict[str, Any]:
        icon = {
            Priority.MAX: "\u2049",
            Priority.HIGH: "\u26a1",
            Priority.DEFAULT: "",
            Priority.LOW: "\u2139",
            Priority.MIN: "",
        }.get(priority, "")
        text = f"<b>{title}</b>\n{message}" if title else message
        if icon:
            text = f"{icon} {text}"
        api = f"https://api.telegram.org/bot{self.config.telegram_bot_token}"
        try:
            # Send image attachments via sendPhoto (first one), caption = text
            if attachments:
                fname, img_bytes = attachments[0]
                caption = text[:1024]  # Telegram caption limit
                resp = await self._client.post(
                    f"{api}/sendPhoto",
                    data={
                        "chat_id": self.config.telegram_chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    },
                    files={"photo": (fname or "image.jpg", img_bytes, "image/jpeg")},
                )
                data = resp.json()
                if not data.get("ok"):
                    return {
                        "success": False,
                        "error": f"telegram sendPhoto: {data.get('description', 'unknown')}",
                    }
                # Extra attachments beyond the first go as documents
                for extra_name, extra_bytes in (attachments or [])[1:5]:
                    await self._client.post(
                        f"{api}/sendDocument",
                        data={"chat_id": self.config.telegram_chat_id},
                        files={"document": (extra_name or "file.bin", extra_bytes)},
                    )
                return {"success": True, "channel": "telegram", "response": "delivered (photo)"}

            resp = await self._client.post(
                f"{api}/sendMessage",
                json={"chat_id": self.config.telegram_chat_id, "text": text, "parse_mode": "HTML"},
            )
            data = resp.json()
            if data.get("ok"):
                return {"success": True, "channel": "telegram", "response": "delivered"}
            return {"success": False, "error": f"telegram: {data.get('description', 'unknown')}"}
        except Exception as e:
            return {"success": False, "error": f"telegram error: {e}"}

    async def _send_ntfy(
        self,
        message: str,
        title: str,
        priority: Priority,
        tags: list[str],
        attachments: list[tuple[str, bytes]],
        click_url: str,
    ) -> dict[str, Any]:
        if not self.config.ntfy_topic:
            return {"success": False, "error": "ntfy topic not configured"}

        url = f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}"
        headers = {
            "Title": title or "JARVIS",
            "Priority": str(priority.value),
            "Tags": ",".join(tags) if tags else "",
        }
        if click_url:
            headers["Click"] = click_url
        if self.config.ntfy_auth_token:
            headers["Authorization"] = f"Bearer {self.config.ntfy_auth_token}"

        # ntfy file attachments: PUT the file to the topic URL
        if attachments:
            try:
                for fname, content in attachments[:3]:
                    resp = await self._client.put(
                        url,
                        content=content,
                        headers={**headers, "Filename": fname or "file.bin"},
                    )
                    if resp.status_code >= 300:
                        return {
                            "success": False,
                            "error": f"ntfy attachment: HTTP {resp.status_code}",
                        }
                if not message.strip():
                    return {"success": True, "channel": "ntfy", "response": "attachment delivered"}
            except Exception as e:
                return {"success": False, "error": f"ntfy attachment error: {e}"}

        try:
            response = await self._client.post(
                url,
                content=message.encode("utf-8"),
                headers=headers,
            )
            response.raise_for_status()
            return {"success": True, "channel": "ntfy", "response": response.text}
        except Exception as e:
            return {"success": False, "error": f"ntfy error: {e}"}

    async def _send_pushover(
        self,
        message: str,
        title: str,
        priority: Priority,
        attachments: list[tuple[str, bytes]],
        click_url: str,
    ) -> dict[str, Any]:
        if not self.config.pushover_user_key or not self.config.pushover_app_token:
            return {"success": False, "error": "Pushover credentials not configured"}

        # Pushover priority: -2 to 2
        pushover_priority = max(-2, min(2, priority.value))

        data = {
            "token": self.config.pushover_app_token,
            "user": self.config.pushover_user_key,
            "message": message,
            "title": title or "JARVIS",
            "priority": str(pushover_priority),
        }
        if click_url:
            data["url"] = click_url
            data["url_title"] = "Open"

        try:
            # Pushover attachment (first image only, multipart)
            files = None
            if attachments:
                fname, content = attachments[0]
                files = {"attachment": (fname or "image.jpg", content, "image/jpeg")}

            response = await self._client.post(
                "https://api.pushover.net/1/messages.json",
                data=data,
                files=files,
            )
            response.raise_for_status()
            return {"success": True, "channel": "pushover", "response": response.json()}
        except Exception as e:
            return {"success": False, "error": f"Pushover error: {e}"}

    async def _send_whatsapp(self, message: str, title: str) -> dict[str, Any]:
        """Send-only WhatsApp via CallMeBot free gateway. No attachments."""
        text = f"{title}\n{message}" if title else message
        url = (
            "https://api.callmebot.com/whatsapp.php?phone="
            f"{urllib.parse.quote(self.config.whatsapp_phone)}&text="
            f"{urllib.parse.quote(text[:4000])}&apikey="
            f"{urllib.parse.quote(self.config.whatsapp_apikey)}"
        )
        try:
            resp = await self._client.get(url)
            body = resp.text or ""
            if resp.status_code == 200 and "queued" in body.lower():
                return {"success": True, "channel": "whatsapp", "response": "queued"}
            return {"success": False, "error": f"callmebot {resp.status_code}: {body[:150]}"}
        except Exception as e:
            return {"success": False, "error": f"whatsapp error: {e}"}

    async def _send_webhook(
        self,
        message: str,
        title: str,
        priority: Priority,
        tags: list[str],
        attachments: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        if not self.config.webhook_url:
            return {"success": False, "error": "Webhook URL not configured"}

        payload = {
            "title": title or "JARVIS",
            "message": message,
            "priority": priority.name.lower(),
            "tags": tags,
            "timestamp": datetime.now().isoformat(),
        }

        headers = {"Content-Type": "application/json"}
        headers.update(self.config.webhook_headers)

        try:
            response = await self._client.request(
                method=self.config.webhook_method,
                url=self.config.webhook_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return {"success": True, "channel": "webhook", "response": response.text}
        except Exception as e:
            return {"success": False, "error": f"Webhook error: {e}"}


class NotificationManager:
    """High-level notification manager with templates and scheduling."""

    def __init__(self, config: NotificationConfig):
        self.config = config
        self.sender = NotificationSender(config)
        self.templates: dict[str, str] = dict(DEFAULT_TEMPLATES)

    def add_template(self, name: str, template: str):
        """Add a notification template. Use {key} for placeholders."""
        self.templates[name] = template

    def render_template(self, template_name: str, **kwargs) -> str:
        template = self.templates.get(template_name, "")
        if not template:
            return ""
        try:
            return template.format(**kwargs)
        except KeyError as e:
            return f"Template error: missing key {e}"

    async def notify(
        self,
        message: str,
        title: str = "",
        priority: Priority = None,
        tags: list[str] = None,
        channel: Channel = None,
        template: str = None,
        **template_vars,
    ) -> dict[str, Any]:
        if template:
            message = self.render_template(template, **template_vars)
        return await self.sender.send(
            message=message,
            title=title,
            priority=priority,
            tags=tags,
            channel=channel,
        )

    async def notify_with_image(
        self,
        message: str,
        image_path: str,
        title: str = "",
        priority: Priority = Priority.HIGH,
        channel: Channel = None,
    ) -> dict[str, Any]:
        """Send notification with image attachment (Telegram sendPhoto)."""
        from pathlib import Path as _P

        try:
            path = _P(image_path)
            if not path.exists():
                return {"success": False, "error": f"image not found: {image_path}"}
            img_bytes = path.read_bytes()
        except Exception as e:
            return {"success": False, "error": f"failed to read image: {e}"}
        return await self.sender.send(
            message=message,
            title=title,
            priority=priority,
            channel=channel,
            attachments=[(path.name, img_bytes)],
        )

    async def aclose(self):
        await self.sender.aclose()


# Default templates
DEFAULT_TEMPLATES = {
    "briefing": "Good morning, {name}! It's {date}. {weather}\n{schedule}",
    "alert": "⚠️ ALERT: {message}",
    "task_complete": "✅ Task completed: {task}",
    "error": "❌ Error: {error}",
    "reminder": "⏰ Reminder: {message}",
    "system_alert": "🖥️ System: {message}",
}

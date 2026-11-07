"""WhatsApp inbox via local wa-bridge (Baileys). Read + approval-gated send.

Bridge: node wa-bridge/server.js (127.0.0.1:3101, QR-paired), URL from
WA_BRIDGE_URL. All functions return data or {"error": ...} — never raise
into chat. available() is a bool probe: False on any error. Sends of bare
numbers are verified + LID-resolved first (PN sends can return ok yet
never arrive). No local state; drafts live in skills/wa_skills.py.
"""

from __future__ import annotations

import os


def _base() -> str:
    return os.getenv("WA_BRIDGE_URL", "http://127.0.0.1:3101").rstrip("/")


def available() -> bool:
    try:
        import httpx

        r = httpx.get(_base() + "/status", timeout=5.0)
        return bool(r.json().get("connected"))
    except Exception:
        return False


def status() -> dict:
    try:
        import httpx

        r = httpx.get(_base() + "/status", timeout=5.0)
        return r.json()
    except Exception as exc:
        return {
            "connected": False,
            "error": f"bridge down ({type(exc).__name__}): start node wa-bridge/server.js + scan QR",
        }


def chats(limit: int = 20) -> dict:
    try:
        import httpx

        r = httpx.get(_base() + "/chats", params={"limit": limit}, timeout=10.0)
        items = r.json()
        return {"chats": items if isinstance(items, list) else []}
    except Exception as exc:
        return {"error": f"wa chats failed: {type(exc).__name__}: {str(exc)[:100]}"}


def messages(chat: str, limit: int = 20) -> dict:
    try:
        import httpx

        r = httpx.get(_base() + "/messages", params={"chat": chat, "limit": limit}, timeout=10.0)
        items = r.json()
        return {"messages": items if isinstance(items, list) else []}
    except Exception as exc:
        return {"error": f"wa messages failed: {type(exc).__name__}: {str(exc)[:100]}"}


def recent_inbound(limit_chats: int = 15, per_chat: int = 10) -> list[dict]:
    """Flatten recent non-mine messages across chats, newest-first."""
    out = []
    res = chats(limit=limit_chats)
    if "error" in res:
        return out
    for c in res.get("chats", []):
        m = messages(c.get("id", ""), limit=per_chat)
        for item in m.get("messages", []):
            if not item.get("mine") and (item.get("text") or "").strip():
                out.append(
                    {
                        "id": item.get("id", ""),
                        "chat": c.get("id", ""),
                        "from": item.get("from", ""),
                        "text": item.get("text", ""),
                        "ts": item.get("ts", 0),
                    }
                )
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return out


def check(number: str) -> dict:
    """Verify a number is on WhatsApp + get LID. Call before first send."""
    import re

    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return {"exists": False, "error": "no digits in number"}
    try:
        import httpx

        r = httpx.get(_base() + "/check", params={"number": digits}, timeout=15.0)
        try:
            data = r.json()
        except Exception:
            # Old bridge (no /check route) answers HTML 404: upgrade needed,
            # NOT "number missing". Never misreport.
            return {
                "exists": False,
                "error": "bridge too old for /check — restart node wa-bridge/server.js",
            }
        if r.status_code == 404 or not data.get("exists"):
            return {"exists": False, "error": f"{digits} is not on WhatsApp"}
        return {"exists": True, "jid": data.get("jid"), "lid": data.get("lid")}
    except Exception as exc:
        return {
            "exists": False,
            "error": f"wa check failed: {type(exc).__name__}: {str(exc)[:100]}",
        }


def send(chat: str, text: str) -> dict:
    """Send via paired session. Bare numbers are verified + LID-resolved
    first (PN sends can return ok yet never arrive). Explicit drafts only."""
    if not chat or not text:
        return {"error": "chat and text required"}
    if "@" not in chat:
        verified = check(chat)
        if not verified.get("exists"):
            return {"error": verified.get("error", "number not on WhatsApp")}
        chat = verified.get("lid") or verified.get("jid") or chat
    elif chat.endswith("@s.whatsapp.net"):
        # Stored PN JIDs get the same treatment: re-resolve to LID when the
        # server offers one (PN sends can silently never arrive).
        verified = check(chat.split("@")[0])
        if verified.get("exists") and verified.get("lid"):
            chat = verified["lid"]
    try:
        import httpx

        r = httpx.post(_base() + "/send", json={"chat": chat, "text": text[:4000]}, timeout=20.0)
        data = r.json()
        if data.get("ok"):
            return {"success": True}
        return {"error": str(data.get("error", "send failed"))[:150]}
    except Exception as exc:
        return {"error": f"wa send failed: {type(exc).__name__}: {str(exc)[:120]}"}

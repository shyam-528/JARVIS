"""WhatsApp skills: triage, drafts, approval-gated send, arrival watcher.

Mirrors the mail automation contract: baseline-first watcher, tiered
summary, LLM drafts for most_important only, explicit-id send.
Drafts live in data/wa_drafts.json, seen ids in data/wa_seen.json,
contact book in data/wa_contacts.json. Tool handlers return strings
("error: ..." on failure) and never raise; state-write failures are
logged to the console and unsaved drafts are reported, not announced.
"""

from __future__ import annotations

from core import whatsapp_inbox as _wa
from core.tools import Tool, ToolBox


def _drafts_path():
    from pathlib import Path as _P

    return _P("data/wa_drafts.json")


def _load_drafts() -> list:
    import json as _j

    try:
        return _j.loads(_drafts_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _tier_of(item: dict) -> tuple[str, str]:
    from core import mail_triage as _tri

    return _tri.classify(
        {"from": item.get("from", ""), "subject": item.get("text", "")}, item.get("text", "")
    )


def _contacts_path():
    from pathlib import Path as _P

    return _P("data/wa_contacts.json")


def _load_contacts() -> dict:
    import json as _j

    try:
        data = _j.loads(_contacts_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_contacts(contacts: dict) -> None:
    import json as _j

    try:
        _contacts_path().parent.mkdir(parents=True, exist_ok=True)
        _contacts_path().write_text(_j.dumps(contacts, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[wa] contact book NOT saved: {exc}")


def resolve_chat(ref: str) -> str | None:
    """Name -> JID via contact book, digits -> JID, JID passthrough."""
    import re as _re

    ref = (ref or "").strip()
    if not ref:
        return None
    if "@" in ref:
        return ref
    digits = _re.sub(r"\D", "", ref)
    if digits and not _re.search(r"[A-Za-z]", ref):
        return f"{digits}@s.whatsapp.net"
    contacts = _load_contacts()
    hit = contacts.get(ref.lower())
    if hit:
        return hit
    # fuzzy: unique prefix match
    cands = [jid for name, jid in contacts.items() if name.startswith(ref.lower())]
    if len(cands) == 1:
        return cands[0]
    return None


def register_wa(box: ToolBox, router=None) -> None:
    async def wa_chats(limit: int = 15) -> str:
        res = _wa.chats(limit=int(limit or 15))
        if "error" in res:
            return f"error: {res['error']}"
        items = res.get("chats", [])
        if not items:
            return "no chats yet (bridge only sees messages arriving after pairing)."
        return "chats:\n" + "\n".join(
            f"- {c.get('id', '?')} ({c.get('unread', 0)} new)" for c in items[:15]
        )

    async def wa_triage(limit: int = 20) -> str:
        from core import mail_triage as _tri

        items = _wa.recent_inbound()[: max(1, int(limit or 20))]
        if not items:
            st = _wa.status()
            if not st.get("connected"):
                return f"error: {st.get('error', 'bridge not connected')}"
            return "no recent inbound WhatsApp messages."
        tiered: dict[str, list] = {}
        for m in items:
            tier, _ = _tier_of(m)
            tiered.setdefault(tier, []).append(
                {"id": m["id"], "from": m["from"], "subject": m["text"][:100]}
            )
        return _tri.summarize(tiered)

    async def wa_draft(chat: str = "", text: str = "") -> str:
        if router is None:
            return "error: draft engine unavailable"
        if not chat:
            return "error: chat id required (see wa_chats)"
        from core import mail_triage as _tri

        try:
            body = await _tri.draft_reply(
                router, chat, "WhatsApp chat", text or "(see recent messages)"
            )
        except Exception as exc:
            return f"error drafting: {exc}"
        import json as _j
        import time as _t

        drafts = _load_drafts()
        did = f"w{int(_t.time()) % 100000}"
        drafts.append(
            {"id": did, "chat": chat, "body": body, "created": _t.strftime("%Y-%m-%d %H:%M")}
        )
        try:
            _drafts_path().parent.mkdir(parents=True, exist_ok=True)
            _drafts_path().write_text(_j.dumps(drafts[-20:], indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[wa] draft NOT saved: {exc}")
            return (
                f"error: drafted a reply but could not save it "
                f"({type(exc).__name__}: {str(exc)[:80]}) — nothing sent, try again."
            )
        return f"draft {did} (NOT sent):\n{body[:800]}\n\nSay 'send it' or /wa send {did}."

    async def wa_send_draft(id: str = "") -> str:
        drafts = _load_drafts()
        if not id:
            if not drafts:
                return "no pending drafts."
            if len(drafts) == 1:
                id = drafts[0]["id"]
            else:
                return "multiple drafts pending:\n" + "\n".join(
                    f"- {d['id']}: {d['chat'][:40]} | {d['body'][:60]}" for d in drafts
                )
        d = next((x for x in drafts if x["id"] == id), None)
        if d is None:
            return f"error: no draft {id}"
        res = _wa.send(d["chat"], d["body"])
        if "error" in res:
            return f"error: {res['error']}"
        try:
            import json as _j

            _drafts_path().write_text(
                _j.dumps([x for x in drafts if x["id"] != id], indent=2), encoding="utf-8"
            )
        except Exception as exc:
            # Message IS sent; a lingering draft risks a double-send.
            print(f"[wa] sent draft {id} NOT removed from store: {exc}")
        return f"sent to {d['chat']}."

    box.register(
        Tool(
            name="wa_chats",
            description="List recent WhatsApp chats. Needs paired bridge.",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 15}},
            },
            handler=wa_chats,
        )
    )

    async def wa_read(chat: str, limit: int = 15) -> str:
        """Full thread incl. your own sent replies (marked me>>)."""
        res = _wa.messages(chat, limit=int(limit or 15))
        if "error" in res:
            return f"error: {res['error']}"
        items = res.get("messages", [])
        if not items:
            return "no cached messages for that chat (bridge only sees post-pairing traffic)."
        lines = []
        for m in items:
            who = "me>>" if m.get("mine") else m.get("from", "?")[:30]
            lines.append(f"{who}: {(m.get('text') or '')[:200]}")
        return "\n".join(lines)[:2500]

    box.register(
        Tool(
            name="wa_read",
            description="Read a WhatsApp thread incl. your sent replies (me>>). Needs paired bridge.",
            parameters={
                "type": "object",
                "properties": {
                    "chat": {"type": "string"},
                    "limit": {"type": "integer", "default": 15},
                },
                "required": ["chat"],
            },
            handler=wa_read,
        )
    )

    async def wa_send(chat: str = "", text: str = "") -> str:
        """Send immediately, no draft, no approval. Use ONLY on direct user order.
        chat accepts a JID, a plain number, or a saved contact name."""
        if not chat or not text.strip():
            return "error: chat and text required."
        jid = resolve_chat(chat)
        if not jid:
            return (
                f"error: unknown contact '{chat}'. Add with "
                f"/wa contact add {chat} <number>, or use the number directly."
            )
        res = _wa.send(jid, text.strip())
        if "error" in res:
            return f"error: {res['error']}"
        return f"sent to {chat}."

    box.register(
        Tool(
            name="wa_send",
            description="Send a WhatsApp message IMMEDIATELY with no approval. chat: JID, number, or saved contact name. Use only when the user explicitly ordered a send (e.g. 'send ... on whatsapp'). Needs paired bridge.",
            parameters={
                "type": "object",
                "properties": {"chat": {"type": "string"}, "text": {"type": "string"}},
                "required": ["chat", "text"],
            },
            handler=wa_send,
        )
    )

    async def wa_contact(action: str = "list", name: str = "", chat: str = "") -> str:
        """Contact book: add <name> <number> | del <name> | list."""
        contacts = _load_contacts()
        action = (action or "list").lower()
        if action == "add":
            import re as _re

            digits = _re.sub(r"\D", "", chat)
            if "@" in chat:
                jid = chat.strip()
            elif digits and len(digits) >= 8:
                jid = f"{digits}@s.whatsapp.net"
            else:
                return "error: give a number or JID: /wa contact add Aslam 91999xxxxxx"
            if not name.strip():
                return "error: name required."
            contacts[name.strip().lower()] = jid
            _save_contacts(contacts)
            return f"saved contact {name.strip()} -> {jid}."
        if action in ("del", "delete", "remove"):
            if contacts.pop(name.strip().lower(), None) is None:
                return f"error: no contact '{name}'."
            _save_contacts(contacts)
            return f"deleted contact {name.strip()}."
        if not contacts:
            return "no contacts. Add: /wa contact add Aslam 91999xxxxxx"
        return "contacts:\n" + "\n".join(f"- {n} -> {j}" for n, j in sorted(contacts.items()))

    box.register(
        Tool(
            name="wa_contact",
            description="WhatsApp contact book: add/del/list names mapped to numbers or JIDs.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "default": "list"},
                    "name": {"type": "string", "default": ""},
                    "chat": {"type": "string", "default": ""},
                },
            },
            handler=wa_contact,
        )
    )
    box.register(
        Tool(
            name="wa_triage",
            description="Categorize recent inbound WhatsApp: most_important/important/low. Read-only.",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
            handler=wa_triage,
        )
    )
    box.register(
        Tool(
            name="wa_draft",
            description="Draft a WhatsApp reply to a chat. Saves for approval, never sends.",
            parameters={
                "type": "object",
                "properties": {
                    "chat": {"type": "string"},
                    "text": {"type": "string", "default": ""},
                },
                "required": ["chat"],
            },
            handler=wa_draft,
        )
    )
    box.register(
        Tool(
            name="wa_send_draft",
            description="Send a saved WhatsApp draft by id (explicit approval only).",
            parameters={"type": "object", "properties": {"id": {"type": "string", "default": ""}}},
            handler=wa_send_draft,
        )
    )


def register_wawatch(box: ToolBox, notify_manager, router=None, speak=None) -> None:
    async def wa_check_new() -> str:
        import json as _j
        from pathlib import Path as _P

        st = _wa.status()
        if not st.get("connected"):
            return f"bridge not connected ({st.get('error', 'pair first')})."
        state = _P("data/wa_seen.json")
        try:
            seen = set(_j.loads(state.read_text(encoding="utf-8")))
        except Exception:
            seen = set()
        items = _wa.recent_inbound()
        ids = {m["id"] for m in items if m.get("id")}
        if not seen:
            try:
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(_j.dumps(sorted(ids)), encoding="utf-8")
            except Exception as exc:
                # Unsaved baseline = next run re-baselines (no spam, but loud).
                print(f"[wa] seen-baseline NOT saved: {exc}")
            return f"wa baseline recorded ({len(ids)} seen, no alerts)."
        new = [m for m in items if m.get("id") and m["id"] not in seen]
        try:
            state.write_text(_j.dumps(sorted(ids | seen)[-500:]), encoding="utf-8")
        except Exception as exc:
            # Unsaved state = same arrivals re-alerted next run. Loud, not silent.
            print(f"[wa] seen-state NOT saved (will re-alert next run): {exc}")
        if not new:
            return "no new WhatsApp."
        from core import mail_triage as _tri

        tiered: dict[str, list] = {}
        for m in new[:10]:
            tier, _ = _tier_of(m)
            tiered.setdefault(tier, []).append(
                {"id": m["id"], "from": m["from"], "subject": m["text"][:100]}
            )
        lines = [_tri.summarize(tiered)]
        drafted = 0
        if router is not None:
            for m in (tiered.get("most_important") or [])[:2]:
                try:
                    body = await _tri.draft_reply(router, m["from"], "WhatsApp", m["subject"])
                    if body:
                        import time as _t

                        drafts = _load_drafts()
                        did = f"w{int(_t.time()) % 100000}"
                        drafts.append(
                            {
                                "id": did,
                                "chat": next((x["chat"] for x in new if x["id"] == m["id"]), ""),
                                "body": body,
                                "created": _t.strftime("%Y-%m-%d %H:%M"),
                            }
                        )
                        try:
                            _drafts_path().parent.mkdir(parents=True, exist_ok=True)
                            _drafts_path().write_text(
                                _j.dumps(drafts[-20:], indent=2), encoding="utf-8"
                            )
                        except Exception as wexc:
                            # Unsaved drafts are reported, never announced.
                            print(f"[wa] triage draft NOT saved: {wexc}")
                            lines.append(
                                f"\n(draft for {m['from'][:40]} could not be saved: {str(wexc)[:60]})"
                            )
                        else:
                            drafted += 1
                            lines.append(f"\nDraft {did} (NOT sent):\n{body[:500]}")
                except Exception:
                    pass
        if drafted:
            lines.append("Say 'send it' or /wa send <id> — nothing sends without approval.")
        msg = "\n".join(lines)[:3500]
        try:
            result = await notify_manager.notify(message=msg, title="WhatsApp triaged")
            if result.get("success"):
                if callable(speak):
                    try:
                        top = (tiered.get("most_important") or [None])[0]
                        if top:
                            await speak(
                                f"Important WhatsApp from {top['from'][:40]}: {top['subject'][:70]}."
                            )
                        else:
                            await speak(f"{len(new)} new WhatsApp messages. Details on Telegram.")
                    except Exception:
                        pass
                return f"triaged {len(new)} new WhatsApp, {drafted} drafted."
            return f"triaged {len(new)}, notify failed: {result.get('error')}"
        except Exception as exc:
            return f"triaged {len(new)}, notify error: {exc}"

    box.register(
        Tool(
            name="wa_check_new",
            description="Check for new inbound WhatsApp since last check; triage + alert. Scheduler fodder.",
            parameters={"type": "object", "properties": {}},
            handler=wa_check_new,
        )
    )

"""Mail skills: read-only inbox tools + triage, drafts, approval-gated send.

register_mail tools never mutate the mailbox. Triage/watcher
(register_mailtriage/register_mailwatch) are baseline-first: the first
run only records data/mail_seen.json; later runs tier arrivals, LLM-draft
most_important only (max 2/run spend cap), and notify. Drafts live in
data/mail_drafts.json. Handlers return strings ("error: ..." on failure)
and never raise. State-write failures are logged to the console and
unsaved drafts are reported, never announced. Sends require an explicit
draft id; a draft with no resolvable recipient is never blind-sent —
repair fills the address and asks for confirmation first.
"""

from __future__ import annotations

from core import mail as _mail
from core.tools import Tool, ToolBox


def _fmt(res: dict) -> str:
    if "error" in res:
        return f"error: {res['error']}"
    mails = res.get("emails", [])
    if not mails:
        return "No matching mail."
    lines = [f"Mail ({res.get('total', len(mails))} matching, showing {len(mails)}):"]
    for m in mails:
        lines.append(f"  [{m['id']}] {m['date']} | {m['from']} | {m['subject']}")
    return "\n".join(lines)[:2500]


def register_mail(box: ToolBox):
    async def mail_unread(limit: int = 10) -> str:
        return _fmt(_mail.unread(limit=int(limit or 10)))

    async def mail_search(query: str, limit: int = 10) -> str:
        return _fmt(_mail.search(query, limit=int(limit or 10)))

    async def mail_read(uid: str) -> str:
        res = _mail.read(uid)
        if "error" in res:
            return f"error: {res['error']}"
        return f"[{res['id']}]\n{res['body'][:2000]}"

    box.register(
        Tool(
            name="mail_unread",
            description="List newest unread mail (read-only).",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 10}},
            },
            handler=mail_unread,
        )
    )
    box.register(
        Tool(
            name="mail_search",
            description="Search mail: plain text, from:<x>, or subject:<x>. Read-only.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
            handler=mail_search,
        )
    )
    box.register(
        Tool(
            name="mail_read",
            description="Read plain-text snippet of one mail by id (stays unread).",
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=mail_read,
        )
    )


def _drafts_path():
    from pathlib import Path as _P

    return _P("data/mail_drafts.json")


def _load_drafts() -> list:
    import json as _j

    try:
        return _j.loads(_drafts_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_draft(meta: dict, body: str) -> str:
    import json as _j
    import time as _t
    from email.utils import getaddresses

    addr = getaddresses([meta.get("from", "")])
    to = addr[0][1] if addr and addr[0][1] else meta.get("from", "")
    if not str(to or "").strip():
        return ""  # no recipient: caller must refuse, never save a broken draft
    drafts = _load_drafts()
    did = f"d{int(_t.time()) % 100000}"
    drafts.append(
        {
            "id": did,
            "to": to,
            "subject": "Re: " + meta.get("subject", ""),
            "body": body,
            "for_uid": meta.get("id", ""),
            "created": _t.strftime("%Y-%m-%d %H:%M"),
        }
    )
    try:
        _drafts_path().parent.mkdir(parents=True, exist_ok=True)
        _drafts_path().write_text(_j.dumps(drafts[-20:], indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[mail] draft NOT saved: {exc}")
        return ""  # never announce a draft that isn't on disk
    return did


def register_mailtriage(box: ToolBox, router=None) -> None:
    async def mail_triage(limit: int = 20) -> str:
        from core import mail_triage as _tri

        res = _mail.unread(limit=int(limit or 20))
        if "error" in res:
            return f"error: {res['error']}"
        tiered: dict[str, list] = {}
        for m in res.get("emails", []):
            tier, reason = _tri.classify(m)
            tiered.setdefault(tier, []).append(dict(m, tier=tier))
        return _tri.summarize(tiered)

    async def mail_draft(uid: str) -> str:
        from core import mail_triage as _tri

        if router is None:
            return "error: draft engine unavailable (no router)"
        res = _mail.read(uid)
        if "error" in res:
            return f"error: {res['error']}"
        meta = _mail.header(uid)
        if "error" in meta:
            return f"error: {meta['error']}"
        try:
            body = await _tri.draft_reply(
                router, meta.get("from", "?"), meta.get("subject", ""), res.get("body", "")
            )
        except Exception as exc:
            return f"error drafting: {exc}"
        did = _save_draft({**meta, "id": uid}, body)
        if not did:
            return (
                f"error: no draft saved for mail {uid} — either no From header "
                f"to address a reply to, or the draft store write failed (see console)."
            )
        # Gmail-native draft (account defect): the reply lands as a REAL draft
        # in Gmail Drafts — visible/editable/sendable in the app — while the
        # local record keeps the approval id. APPEND failure never blocks the
        # local draft (loud instead); the send path works from local data.
        _gmail_note = ""
        try:
            import json as _j2

            _rec = next((x for x in _load_drafts() if x["id"] == did), None)
            if _rec:
                _g = _mail.save_gmail_draft(_rec.get("to", ""), _rec.get("subject", ""), body)
                if "uid" in _g:
                    _rec["gmail_uid"] = _g.get("uid", "")
                    _rec["gmail_mailbox"] = _g.get("mailbox", "")
                    try:
                        _drafts_path().write_text(
                            _j2.dumps(_load_drafts(), indent=2), encoding="utf-8"
                        )
                    except Exception as exc:
                        print(f"[mail] gmail uid NOT recorded for {did}: {exc}")
                    _gmail_note = " It's also in your Gmail drafts."
                else:
                    print(f"[mail] gmail draft APPEND failed for {did}: {_g.get('error', '?')}")
                    _gmail_note = " (Gmail copy failed — local draft kept, send still works.)"
        except Exception as exc:
            print(f"[mail] gmail draft APPEND failed for {did}: {exc}")
            _gmail_note = " (Gmail copy failed — local draft kept, send still works.)"
        return (
            f"draft {did} (NOT sent) to {meta.get('from', '')[:60]}:\n{body[:800]}\n\n"
            f"Say 'send it' or /mail send {did}.{_gmail_note}"
        )

    async def mail_send_draft(id: str = "", to: str = "") -> str:
        drafts = _load_drafts()
        if not id:
            if not drafts:
                return "no pending drafts."
            if len(drafts) == 1:
                id = drafts[0]["id"]
            else:
                return "multiple drafts pending — say /mail send <id>:\n" + "\n".join(
                    f"- {d['id']}: to {(d.get('to') or '?')[:40]} | {d['subject'][:60]}"
                    for d in drafts
                )
        d = next((x for x in drafts if x["id"] == id), None)
        if d is None:
            return f"error: no draft {id}"
        if str(to or "").strip():
            d["to"] = to.strip()  # explicit address = confirmation, send below
        if not str(d.get("to") or "").strip():
            # Legacy broken draft (saved without a recipient): try to repair
            # from the original mail — but NEVER blind-send to a re-resolved
            # address (sequence ids shift). Fill it in and ask to confirm.
            repaired = ""
            try:
                h = _mail.header(str(d.get("for_uid", "")))
                if "error" not in h and h.get("from"):
                    from email.utils import getaddresses as _ga

                    addr = _ga([h["from"]])
                    if addr and addr[0][1]:
                        repaired = addr[0][1]
            except Exception:
                pass
            if repaired:
                d["to"] = repaired
                try:
                    import json as _j

                    _drafts_path().write_text(_j.dumps(drafts, indent=2), encoding="utf-8")
                except Exception as exc:
                    print(f"[mail] repaired recipient for draft {id} NOT saved: {exc}")
                return (
                    f"draft {id} repaired: recipient is {repaired}. "
                    f"Say 'send it' again to confirm the send."
                )
            return (
                f"error: draft {id} has no recipient and the original mail "
                f"(id {d.get('for_uid', '?')}) can't be resolved — it may have "
                f"moved. Re-draft from a fresh unread id, or give an address: "
                f"/mail send {id} <address>."
            )
        res = _mail.send(d["to"], d["subject"], d["body"])
        if "error" in res:
            return f"error: {res['error']}"
        # Post-send cleanup: drop the Gmail draft copy (best-effort — the SMTP
        # send already happened, so a leftover Gmail draft is untidy, not fatal)
        # plus the local record (a lingering record risks a double-send).
        if str(d.get("gmail_uid") or "").strip():
            try:
                _del = _mail.delete_gmail_draft(str(d.get("gmail_uid")))
                if "error" in _del:
                    print(f"[mail] gmail draft copy NOT cleaned for {id}: {_del['error']}")
            except Exception as exc:
                print(f"[mail] gmail draft copy NOT cleaned for {id}: {exc}")
        rest = [x for x in drafts if x["id"] != id]
        try:
            import json as _j

            _drafts_path().write_text(_j.dumps(rest, indent=2), encoding="utf-8")
        except Exception as exc:
            # Message IS sent; a lingering draft risks a double-send.
            print(f"[mail] sent draft {id} NOT removed from store: {exc}")
        return f"sent to {d['to']}."

    box.register(
        Tool(
            name="mail_triage",
            description="Categorize unread inbox: most_important/important/normal/low. Read-only.",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
            handler=mail_triage,
        )
    )
    box.register(
        Tool(
            name="mail_draft",
            description="Draft a reply to one mail by IMAP id. Saves for approval, never sends.",
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=mail_draft,
        )
    )
    box.register(
        Tool(
            name="mail_send_draft",
            description="Send a saved draft by id (explicit approval only). Empty id sends the single pending draft. Optional to= overrides/fills the recipient.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "default": ""},
                    "to": {"type": "string", "default": ""},
                },
            },
            handler=mail_send_draft,
        )
    )


def register_mailwatch(box: ToolBox, notify_manager, router=None, speak=None) -> None:
    """New-arrival watcher for the scheduler (every few minutes).

    First run only records the baseline (no 625-message spam). Later runs
    triage arrivals: most_important get an LLM draft + approval request,
    everything else a tiered summary. Drafts queue in data/mail_drafts.json.
    speak: optional async callable(text) for voice announcements (gated).
    """

    async def mail_check_new() -> str:
        import json as _j
        from pathlib import Path as _P

        if not _mail.configured():
            return "mail not configured"
        state = _P("data/mail_seen.json")
        try:
            seen = set(_j.loads(state.read_text(encoding="utf-8")))
        except Exception:
            seen = set()
        res = _mail.unread(limit=25)
        if "error" in res:
            return f"error: {res['error']}"
        ids = {m["id"] for m in res.get("emails", [])}
        if not seen:
            # Baseline: record without notifying.
            try:
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(_j.dumps(sorted(ids)), encoding="utf-8")
            except Exception as exc:
                # Unsaved baseline = next run re-baselines (no spam, but loud).
                print(f"[mail] seen-baseline NOT saved: {exc}")
            return f"mail baseline recorded ({len(ids)} seen, no alerts)."
        new = [m for m in res.get("emails", []) if m["id"] not in seen]
        try:
            state.write_text(_j.dumps(sorted(ids | seen)[-500:]), encoding="utf-8")
        except Exception as exc:
            # Unsaved state = same arrivals re-alerted next run. Loud, not silent.
            print(f"[mail] seen-state NOT saved (will re-alert next run): {exc}")
        if not new:
            return "no new mail."
        # Tier + draft most_important (max 2 drafts per run = spend cap).
        from core import mail_triage as _tri

        tiered: dict[str, list] = {}
        for m in new[:10]:
            snip = _mail.snippet(m["id"]) if len(tiered.get("most_important", [])) < 3 else ""
            tier, reason = _tri.classify(m, snip)
            m = dict(m, tier=tier, reason=reason)
            tiered.setdefault(tier, []).append(m)
        drafted = []
        if router is not None:
            for m in tiered.get("most_important", [])[:2]:
                try:
                    snip = _mail.snippet(m["id"])
                    body = await _tri.draft_reply(router, m["from"], m["subject"], snip)
                    if body:
                        did = _save_draft(m, body)
                        if did:
                            drafted.append((m, did, body))
                        # unsaved draft: logged in _save_draft, not announced
                except Exception:
                    pass
        lines = [_tri.summarize(tiered)]
        _top = (tiered.get("most_important") or [None])[0]
        if _top:
            lines.insert(
                0, f"🔴 Important mail regarding '{_top['subject'][:70]}' from {_top['from'][:40]}."
            )
        for m, did, body in drafted:
            lines.append(f"\n✍️ Draft for [{m['id']}] {m['subject'][:60]} (id {did}):\n{body[:600]}")
            lines.append(
                "Reply 'send it' or /mail send " + did + " — nothing sends without approval."
            )
        msg = "\n".join(lines)[:3500]
        try:
            result = await notify_manager.notify(message=msg, title="New mail triaged")
            if result.get("success"):
                if callable(speak):
                    try:
                        _top = (tiered.get("most_important") or [None])[0]
                        if _top:
                            await speak(
                                f"You have an important mail regarding {_top['subject'][:70]} from {_top['from'][:40]}."
                            )
                        else:
                            await speak(f"You have {len(new)} new mails. Details on Telegram.")
                    except Exception:
                        pass
                return f"triaged {len(new)} new mail, {len(drafted)} drafted."
            return f"triaged {len(new)} new mail, notify failed: {result.get('error')}"
        except Exception as exc:
            return f"triaged {len(new)} new mail, notify error: {exc}"

    box.register(
        Tool(
            name="mail_check_new",
            description="Check for genuinely new mail since last check; alerts on arrivals. Scheduler fodder.",
            parameters={"type": "object", "properties": {}},
            handler=mail_check_new,
        )
    )

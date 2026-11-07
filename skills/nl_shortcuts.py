"""Natural-language shortcuts: deterministic fast paths that bypass the LLM.

Extracted from jarvis.py (consolidation step 1). Each try_* function returns
True when it handled the input (printing via the passed console), False when
the input should fall through to the brain. REPL order: mail -> routine -> pc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator


def _pending_send(orch: Orchestrator) -> tuple | None:
    """Last send-to-number/name without a channel, from history.

    Lets a bare follow-up ("in whatsapp") complete the previous order.
    Returns (chat_ref, text) or None."""
    import re as _re

    try:
        history = getattr(orch, "history", []) or []
    except Exception:
        return None
    for m in reversed(history):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        text = str(m.get("content", ""))
        low = text.lower()
        if _re.search(r"whatsapp|telegram|\bmail\b|email|\bsms\b", low):
            continue  # already had a channel
        mt = _re.search(
            r"send\s+(.+?)\s+to\s+(\+?\d[\d\s\-]{7,})\s*$", text, _re.IGNORECASE | _re.DOTALL
        )
        if mt:
            return mt.group(2).strip(), mt.group(1).strip()
        mt = _re.search(
            r"send\s+(.+?)\s+to\s+([A-Za-z][\w\s.'\-]{0,29})\s*$", text, _re.IGNORECASE | _re.DOTALL
        )
        if mt:
            try:
                from skills.wa_skills import resolve_chat as _rs

                jid = _rs(mt.group(2).strip())
            except Exception:
                jid = None
            if jid:
                return jid, mt.group(1).strip()
    return None


async def try_routine_shortcut(orch: Orchestrator, cmd: str, console) -> bool:
    """One-tap routines: 'starting work' / 'winding down' run the bundle
    directly instead of hoping the brain chains four tools. Returns True
    when handled (including /morning and /evening)."""
    import re as _re

    low = cmd.lower().strip()
    tool = ""
    if low in ("/morning", "/evening"):
        tool = "morning_start" if low == "/morning" else "evening_wrap"
    elif _re.search(r"\bstarting work\b|\bstart my day\b|\bmorning routine\b", low):
        tool = "morning_start"
    elif _re.search(
        r"\bwinding down\b|\bwind down\b|\bevening routine\b|\bend my day\b|\bwrap up\b", low
    ):
        tool = "evening_wrap"
    if not tool:
        return False
    from core.commands import run_routine_tool  # shared with /morning|/evening

    await run_routine_tool(orch, console, tool)
    return True


async def try_pc_shortcut(orch: Orchestrator, cmd: str, console) -> bool:
    """PC deep control: app launching + flows run directly (free policy).
    Returns True when handled."""
    try:
        from skills.pc_flows import match_pc_command
    except Exception:
        return False
    hit = match_pc_command(cmd)
    if not hit:
        return False
    tool, args = hit
    try:
        import json as _pj

        out = await orch.toolbox.execute(tool, _pj.dumps(args))
    except Exception as exc:
        out = f"PC error: {exc}"
    console.print(out, markup=False, highlight=False)
    return True


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _parse_day(text: str) -> str:
    """Deterministic relative-date parser -> ISO YYYY-MM-DD or "".

    Handles today/tomorrow/day-after/weekday names/ISO/Mon-DD. Festival
    names and fuzzy dates return "" so the brain resolves them via the
    mark_day tool (it has the current date in context). Never raises.
    """
    import re as _re
    from datetime import date as _date
    from datetime import timedelta as _td

    try:
        low = str(text or "").lower().strip()
        today = _date.today()
        if not low:
            return ""
        if low in ("today", "tonight", "this morning", "this evening"):
            return today.isoformat()
        if low in ("tomorrow", "tomorrow morning", "tomorrow evening"):
            return (today + _td(days=1)).isoformat()
        if low in ("day after tomorrow", "day after"):
            return (today + _td(days=2)).isoformat()
        m = _re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", low)
        if m:
            return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = _re.fullmatch(r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", low)
        if m:
            delta = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
            return (today + _td(days=delta)).isoformat()
        m = _re.fullmatch(
            r"(?:this|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
            low,
        )
        if m:
            delta = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
            return (today + _td(days=delta)).isoformat()
        m = _re.fullmatch(
            r"(january|february|march|april|may|june|july|august|september|october|november|december)"
            r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?",
            low,
        )
        if m:
            year = int(m.group(3)) if m.group(3) else today.year
            day = _date(year, _MONTHS[m.group(1)], int(m.group(2)))
            if day < today and not m.group(3):
                day = _date(year + 1, _MONTHS[m.group(1)], int(m.group(2)))
            return day.isoformat()
        return ""
    except Exception:
        return ""


async def try_mail_shortcut(orch: Orchestrator, cmd: str, console) -> bool:
    """Directly answer mail questions via tools. Returns True if handled."""
    import json as _j
    import re as _re

    low = cmd.lower().strip()
    # Account words (account defect): "check my account", "anything in my
    # account", "gmail" behave like inbox words — but never for money
    # contexts ("bank account", "account balance" fall through to the brain).
    _money = bool(_re.search(r"\b(bank|balance|salary|pay|money|upi|wallet)\b", low))
    # Destructive account phrasings are NEVER mail ("delete my account" must
    # reach the brain, not the inbox).
    _destructive = bool(_re.search(r"\b(delete|remove|close|deactivate|hack)\b", low))
    _acct = (
        bool(
            _re.search(
                r"\bgmail\b|\bmy\s+mails?\b|\bmy\s+account\b|\baccount\s+(inbox|mails?|mail)\b"
                r"|\bcheck\s+my\s+account\b|\banything\s+.*\baccount\b",
                low,
            )
        )
        and not _money
        and not _destructive
    )
    # Channel-only follow-up ("in whatsapp") completes a pending send:
    # looks back for the last send-to-number/name without a channel.
    if _re.fullmatch(r"(in|on|via)\s+whatsapp|whatsapp", low):
        _pend = _pending_send(orch)
        if _pend:
            _chat, _text = _pend
            out = await orch.toolbox.execute("wa_send", _j.dumps({"chat": _chat, "text": _text}))
            console.print(out, markup=False, highlight=False)
            return True
        # else fall through to triage below
    # Contact-implies-channel: "send hi to aslam" with aslam in the book
    # means WhatsApp even without the word (unless another channel named).
    _cm = _re.search(
        r"send\s+(.+?)\s+to\s+([A-Za-z][\w\s.'\-]{0,29})$", cmd, _re.IGNORECASE | _re.DOTALL
    )
    if _cm and not _re.search(r"whatsapp|email|\bmail\b|telegram|\bsms\b", low):
        from skills.wa_skills import resolve_chat as _resolve2

        _jid2 = _resolve2(_cm.group(2).strip())
        if _jid2:
            out = await orch.toolbox.execute(
                "wa_send", _j.dumps({"chat": _jid2, "text": _cm.group(1).strip()})
            )
            console.print(out, markup=False, highlight=False)
            return True
    # WhatsApp NL (mirrors mail shortcut): triage / send-approval / chats.
    if _re.search(r"\bwhatsapp\b|\bwa chats\b", low):
        if _re.search(r"send\s+(it|the\s+draft|that)\b", low) and not _re.search(r"\d{8,}", low):
            m = _re.search(r"\bw(\d+)\b", low)
            if m:
                out = await orch.toolbox.execute(
                    "wa_send_draft", _j.dumps({"id": "w" + m.group(1)})
                )
            else:
                out = await orch.toolbox.execute("wa_send_draft", "{}")
            console.print(out, markup=False, highlight=False)
            return True
        # Direct order: number first ("send <text> to <number> in|on|via whatsapp")
        _m = _re.search(
            r"send\s+(.+?)\s+to\s+(\+?\d[\d\s\-]{7,})(?:\s+(?:on|in|via)\s+whatsapp)?$",
            cmd,
            _re.IGNORECASE | _re.DOTALL,
        )
        if _m:
            out = await orch.toolbox.execute(
                "wa_send", _j.dumps({"chat": _m.group(2), "text": _m.group(1).strip()})
            )
            console.print(out, markup=False, highlight=False)
            return True
        # Name order: "send hi to aslam in whatsapp" (contact book resolution).
        # Lazy name + explicit strip: greedy match would swallow the suffix.
        _m = _re.search(
            r"send\s+(.+?)\s+to\s+([A-Za-z][\w\s.'\-]{0,29}?)(?:\s+(?:in|on|via)\s+whatsapp)?$",
            cmd,
            _re.IGNORECASE | _re.DOTALL,
        )
        if _m and not _re.search(r"\d{8,}", _m.group(2)):
            from skills.wa_skills import resolve_chat as _resolve

            _name = _re.sub(
                r"\s+(?:in|on|via)\s+whatsapp$", "", _m.group(2).strip(), flags=_re.IGNORECASE
            )
            _jid = _resolve(_name)
            if _jid:
                out = await orch.toolbox.execute(
                    "wa_send", _j.dumps({"chat": _jid, "text": _m.group(1).strip()})
                )
            else:
                out = (
                    f"unknown contact '{_name}' — teach me: "
                    f"/wa contact add {_name} <number>, or use the number directly."
                )
            console.print(out, markup=False, highlight=False)
            return True
        _m = _re.search(r"whatsapp\s+(\+?\d[\d\s\-]{7,})\s+(.+)", cmd, _re.IGNORECASE | _re.DOTALL)
        if _m:
            out = await orch.toolbox.execute(
                "wa_send", _j.dumps({"chat": _m.group(1), "text": _m.group(2).strip()})
            )
            console.print(out, markup=False, highlight=False)
            return True
        if _re.search(r"\bchats\b", low):
            console.print(
                await orch.toolbox.execute("wa_chats", "{}"), markup=False, highlight=False
            )
            return True
        out = await orch.toolbox.execute("wa_triage", "{}")
        console.print(out, markup=False, highlight=False)
        return True
    # "read/open the X mail" (by name, not id): search first, then read the
    # top hit by its real UID. Bare "read emergent mail" used to send the
    # name as an IMAP UID -> BAD Could not parse command.
    _mr = _re.search(r"(?:read|open)\s+(?:the\s+)?([\w .''-]{2,40}?)\s+(?:mail|email)\b", low)
    if _mr:
        _term = _mr.group(1).strip()
        _hits = await orch.toolbox.execute("mail_search", _j.dumps({"query": _term, "limit": 3}))
        _mm = _re.search(r"\[(\d+)\]", _hits)
        if _mm:
            out = await orch.toolbox.execute("mail_read", _j.dumps({"uid": _mm.group(1)}))
            console.print(out, markup=False, highlight=False)
        else:
            console.print(_hits, markup=False, highlight=False)
        return True
    # "draft a reply to the X mail": find it, read it, draft — then show the
    # draft + its id so "send it" works immediately. Never interrogate the
    # user for content we can fetch ourselves.
    _dr = _re.search(
        r"draft.*?(?:reply\s+)?(?:to|for)\s+(?:the\s+)?([\w .''-]{2,40}?)(?:\s+mail|\s+email|$)",
        low,
    )
    if _dr and _re.search(r"mail|email", low):
        _term = _dr.group(1).strip()
        _hits = await orch.toolbox.execute("mail_search", _j.dumps({"query": _term, "limit": 3}))
        _mm = _re.search(r"\[(\d+)\]", _hits)
        if _mm:
            out = await orch.toolbox.execute("mail_draft", _j.dumps({"uid": _mm.group(1)}))
            console.print(out, markup=False, highlight=False)
        else:
            console.print(_hits, markup=False, highlight=False)
        return True

    # "draft a reply for <person>" (account defect): no mail word needed —
    # search mail involving them, draft to the top hit. Falls through when
    # nothing found (brain handles topic-drafts via tools).
    _dp = _re.search(
        r"\bdraft\b.*?\b(?:reply\s+)?(?:to|for)\s+([A-Za-z][\w\s.'@-]{1,40})",
        low,
    )
    if _dp and not _re.search(r"\bmail\b|\bemail\b", low):
        _term = _re.sub(r"\s+(about|regarding)\s+.*$", "", _dp.group(1).strip())
        if _term:
            _hits = await orch.toolbox.execute(
                "mail_search", _j.dumps({"query": _term, "limit": 3})
            )
            _mm = _re.search(r"\[(\d+)\]", _hits)
            if _mm:
                out = await orch.toolbox.execute("mail_draft", _j.dumps({"uid": _mm.group(1)}))
                console.print(out, markup=False, highlight=False)
                return True
    # Day events (account defect): "mark <day> as <event>", "mark <event> on
    # <day>", "what's on <day>". Deterministic relative dates handled here;
    # festival names etc. fall through to the brain (mark_day tool + context).
    if not _re.search(r"\bmail\b|\bemail\b|\binbox\b|\bread\b|\bunread\b", low):
        _mk = _re.search(r"\bmark\s+(.+?)\s+as\s+(.+)$", low) or _re.search(
            r"\bmark\s+(.+?)\s+on\s+(.+)$", low
        )
        if _mk:
            _a, _b = _mk.group(1).strip(), _mk.group(2).strip()
            _day = _parse_day(_a) or (_parse_day(_b) if " on " in _mk.group(0) else "")
            _title = _b if _parse_day(_a) else _a
            if _day and _title:
                out = await orch.toolbox.execute(
                    "mark_day", _j.dumps({"date": _day, "title": _title})
                )
                console.print(out, markup=False, highlight=False)
                return True
        _wo = _re.search(r"what'?s\s+(?:on|marked\s+on|happening\s+on)\s+(.+)$", low) or _re.search(
            r"anything\s+(?:on|marked\s+on)\s+(.+)$", low
        )
        if _wo:
            _day = _parse_day(_wo.group(1).strip())
            if _day:
                out = await orch.toolbox.execute("day_events", _j.dumps({"date": _day}))
                console.print(out, markup=False, highlight=False)
                return True
    # Bare approval ("send it") right after a draft notice has no mail word.
    if _re.fullmatch(r"(send it|send the draft|send that|yes[,\s]+send)[.!]*", low):
        out = await orch.toolbox.execute("mail_send_draft", "{}")
        console.print(out, markup=False, highlight=False)
        return True
    if not _re.search(r"\bmail\b|\bemail\b|\binbox\b", low) and not _acct:
        return False
    # Approval-gated send: "send it", "send the draft", "/mail send d123"
    if _re.search(r"send\s+(it|the\s+draft|that)\b", low) or _re.search(r"mail\s+send\b", low):
        m = _re.search(r"\bd(\d+)\b", low)
        em = _re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", cmd)
        _to = em.group(0) if em else ""
        if m:
            out = await orch.toolbox.execute(
                "mail_send_draft", _j.dumps({"id": "d" + m.group(1), "to": _to})
            )
        else:
            out = await orch.toolbox.execute("mail_send_draft", _j.dumps({"to": _to}))
        console.print(out, markup=False, highlight=False)
        return True
    if _re.search(r"\btriage\b|\bcategor|organize.*mail|sort.*mail", low):
        out = await orch.toolbox.execute("mail_triage", "{}")
        console.print(out, markup=False, highlight=False)
        return True
    # Importance questions ("any very important mail?") -> triage, not LLM lottery.
    if _re.search(r"important", low):
        out = await orch.toolbox.execute("mail_triage", "{}")
        console.print(out, markup=False, highlight=False)
        return True
    if _re.search(r"\bdraft\b", low):
        m = _re.search(r"\[?([\w\-/+=]+)\]?", cmd)
        out = (
            await orch.toolbox.execute("mail_draft", _j.dumps({"uid": m.group(1)}))
            if m
            else "Usage: draft a reply to mail <id>."
        )
        console.print(out, markup=False, highlight=False)
        return True
    try:
        if (
            _re.search(
                r"unread|new mail|any mail|inbox|check.*mail",
                low,
            )
            or (_re.search(r"my account|my gmail|anything.*account", low) and not _destructive)
            or (_acct and not _destructive)
        ):
            out = await orch.toolbox.execute("mail_unread", '{"limit": 8}')
            console.print(out, markup=False, highlight=False)
            return True
        m = _re.search(r"read\s+(?:mail\s+)?\[?([\w\-/+=]+)\]?", low)
        if m and _re.search(r"\bread\b", low):
            out = await orch.toolbox.execute("mail_read", _j.dumps({"uid": m.group(1)}))
            console.print(out, markup=False, highlight=False)
            return True
        m = _re.search(r"(?:search|find|from:|subject:)(.*)", cmd, _re.IGNORECASE)
        if m and m.group(1).strip():
            q = _re.sub(
                r"^(mails?|email)\s+(for|about)\s+", "", m.group(1).strip(), flags=_re.IGNORECASE
            )
            out = await orch.toolbox.execute("mail_search", _j.dumps({"query": q, "limit": 8}))
            console.print(out, markup=False, highlight=False)
            return True
    except Exception as exc:
        console.print(f"[red]Mail error:[/] {exc}")
        return True
    return False

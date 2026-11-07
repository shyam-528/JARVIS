"""Phase 6 mail: read-only IMAP (unread, search, read) + Gmail-native drafts.

Drafts are real Gmail drafts (IMAP APPEND to the Drafts mailbox), so "draft
a reply" leaves a draft sitting in Gmail — editable, sendable from the app.
Sends go via SMTP with explicit approval only. Deletes touch ONLY drafts the
bridge created (cleanup after send), never inbox mail. No flag changes.

Config via env (.env, never committed):
  MAIL_IMAP_HOST  e.g. imap.gmail.com | outlook.office365.com
  MAIL_IMAP_PORT  default 993
  MAIL_USER       full address
  MAIL_PASS       Gmail: App Password (myaccount.google.com/apppasswords).
                  Outlook: App Password (account.microsoft.com, 2FA on).
All functions return data or {"error": ...} — never raise into chat.

Failure policy (Wave 7): IMAP/SMTP failures are returned as {"error": ...}
naming the operation (unread/search/read/header/send/draft) — that is the loud
channel. Per-message decode/parse hiccups (one bad body part or date header)
degrade to "?" / replacement chars and are documented fail-soft; mailbox
close() during cleanup is best-effort.
"""

from __future__ import annotations

import contextlib
import email
import email.header
import imaplib
import os
from email.utils import parsedate_to_datetime


def configured() -> bool:
    return bool(
        os.getenv("MAIL_IMAP_HOST", "").strip()
        and os.getenv("MAIL_USER", "").strip()
        and os.getenv("MAIL_PASS", "").strip()
    )


def _open() -> imaplib.IMAP4_SSL:
    host = os.getenv("MAIL_IMAP_HOST", "").strip()
    try:
        port = int(os.getenv("MAIL_IMAP_PORT", "993"))
    except ValueError:
        port = 993
    box = imaplib.IMAP4_SSL(host, port)
    box.login(os.getenv("MAIL_USER", "").strip(), os.getenv("MAIL_PASS", ""))
    box.select(os.getenv("MAIL_FOLDER", "INBOX"))
    return box


def _decode(hdr) -> str:
    if not hdr:
        return ""
    parts = []
    for text, enc in email.header.decode_header(hdr):
        if isinstance(text, bytes):
            try:
                parts.append(text.decode(enc or "utf-8", errors="replace"))
            except Exception:
                # fail-soft: declared charset is bogus; utf-8 with replacement
                # chars keeps the header readable
                parts.append(text.decode("utf-8", errors="replace"))
        else:
            parts.append(text)
    return "".join(parts)


def _summary(num: bytes, box: imaplib.IMAP4_SSL) -> dict:
    _, data = box.fetch(
        num,
        "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE LIST-UNSUBSCRIBE PRECEDENCE X-PRIORITY)] INTERNALDATE)",
    )
    raw = data[0][1] if data and data[0] else b""
    msg = email.message_from_bytes(raw)
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        when = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        # fail-soft: malformed Date header → show the raw header prefix
        when = msg.get("Date", "?")[:16]
    bulk = bool(msg.get("List-Unsubscribe") or (msg.get("Precedence", "").lower() == "bulk"))
    return {
        "id": num.decode(),
        "from": _decode(msg.get("From"))[:80],
        "to": _decode(msg.get("To"))[:80],
        "subject": _decode(msg.get("Subject"))[:120],
        "date": when,
        "bulk": bulk,
        "priority": (msg.get("X-Priority", "") or "")[:10],
    }


def unread(limit: int = 10) -> dict:
    """Newest unread, newest-first. {"emails": [...], "total": N} or {"error": ...}."""
    if not configured():
        return {"error": "mail not configured — set MAIL_IMAP_HOST/MAIL_USER/MAIL_PASS in .env"}
    try:
        box = _open()
        try:
            _, data = box.search(None, "UNSEEN")
            ids = data[0].split()
            total = len(ids)
            out = [_summary(n, box) for n in reversed(ids[-max(1, limit) :])]
            return {"emails": out, "total": total}
        finally:
            with contextlib.suppress(Exception):
                box.close()  # best-effort cleanup; logout below is the real close
            box.logout()
    except Exception as exc:
        return {"error": f"mail unread failed: {type(exc).__name__}: {str(exc)[:120]}"}


def search(query: str, limit: int = 10) -> dict:
    """Gmail-style raw search is provider-specific; use IMAP SUBJECT/FROM/BODY keys.
    Prefix with 'from:', 'subject:' or plain text (searches subject+body)."""
    if not configured():
        return {"error": "mail not configured — set MAIL_IMAP_HOST/MAIL_USER/MAIL_PASS in .env"}
    query = (query or "").strip()
    if not query:
        return {"error": "empty query"}
    try:
        box = _open()
        try:
            if query.lower().startswith("from:"):
                crit = f'FROM "{query[5:].strip()}"'
            elif query.lower().startswith("subject:"):
                crit = f'SUBJECT "{query[8:].strip()}"'
            else:
                crit = f'TEXT "{query}"'
            _, data = box.search(None, crit)
            ids = data[0].split()
            out = [_summary(n, box) for n in reversed(ids[-max(1, limit) :])]
            return {"emails": out, "total": len(ids)}
        finally:
            with contextlib.suppress(Exception):
                box.close()  # best-effort cleanup; logout below is the real close
            box.logout()
    except Exception as exc:
        return {"error": f"mail search failed: {type(exc).__name__}: {str(exc)[:120]}"}


def read(uid: str, max_chars: int = 2000) -> dict:
    """Plain-text body snippet of one message (BODY.PEEK — stays unread)."""
    if not configured():
        return {"error": "mail not configured — set MAIL_IMAP_HOST/MAIL_USER/MAIL_PASS in .env"}
    try:
        box = _open()
        try:
            _, data = box.fetch(uid.encode(), "(BODY.PEEK[TEXT])")
            raw = data[0][1] if data and data[0] else b""
            text = raw.decode("utf-8", errors="replace")
            return {"id": uid, "body": text[:max_chars]}
        finally:
            with contextlib.suppress(Exception):
                box.close()  # best-effort cleanup; logout below is the real close
            box.logout()
    except Exception as exc:
        return {"error": f"mail read failed: {type(exc).__name__}: {str(exc)[:120]}"}


def header(uid: str) -> dict:
    """From/Subject/Date for one message id, direct fetch (BODY.PEEK — stays
    unread). Works for read mail too, unlike scanning the unread list whose
    sequence ids shift. {"from","subject","date"} or {"error": ...}."""
    if not configured():
        return {"error": "mail not configured — set MAIL_IMAP_HOST/MAIL_USER/MAIL_PASS in .env"}
    if not str(uid or "").strip():
        return {"error": "empty mail id"}
    try:
        box = _open()
        try:
            _, data = box.fetch(uid.encode(), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = data[0][1] if data and data[0] and len(data[0]) > 1 else b""
            if not raw:
                return {
                    "error": f"no mail {uid} (id may have shifted — check the unread list for a fresh id)"
                }
            msg = email.message_from_bytes(raw)
            frm, subj = _decode(msg.get("From"))[:80], _decode(msg.get("Subject"))[:120]
            if not frm:
                return {"error": f"mail {uid} has no From header"}
            try:
                when = parsedate_to_datetime(msg.get("Date", "")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                when = (msg.get("Date", "?") or "?")[:16]
            return {"id": uid, "from": frm, "subject": subj, "date": when}
        finally:
            with contextlib.suppress(Exception):
                box.close()  # best-effort cleanup; logout below is the real close
            box.logout()
    except Exception as exc:
        return {"error": f"mail header failed: {type(exc).__name__}: {str(exc)[:120]}"}


def snippet(uid: str, max_chars: int = 600) -> str:
    """First readable lines of a message for triage (stays unread)."""
    res = read(uid, max_chars * 2)
    if "error" in res:
        return ""
    lines = []
    for line in res["body"].splitlines():
        line = line.strip()
        if not line or line.startswith(">") or "unsubscribe" in line.lower():
            continue
        lines.append(line)
        if sum(len(x) for x in lines) > max_chars:
            break
    return " ".join(lines)[:max_chars]


def send(to: str, subject: str, body: str) -> dict:
    """Send via SMTP (same app password). Explicit drafts only — never bulk."""
    if not configured():
        return {"error": "mail not configured"}
    if not to or not body:
        return {"error": "to and body required"}
    import smtplib
    from email.message import EmailMessage

    host = os.getenv("MAIL_SMTP_HOST", "").strip() or _smtp_default()
    try:
        port = int(os.getenv("MAIL_SMTP_PORT", "587"))
    except ValueError:
        port = 587
    msg = EmailMessage()
    msg["From"] = os.getenv("MAIL_USER", "").strip()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(os.getenv("MAIL_USER", "").strip(), os.getenv("MAIL_PASS", ""))
            s.send_message(msg)
        return {"success": True}
    except Exception as exc:
        return {"error": f"smtp send failed: {type(exc).__name__}: {str(exc)[:150]}"}


def _smtp_default() -> str:
    host = os.getenv("MAIL_IMAP_HOST", "").lower()
    if "gmail" in host:
        return "smtp.gmail.com"
    if "outlook" in host or "office365" in host:
        return "smtp.office365.com"
    return host


def _drafts_mailbox(box: imaplib.IMAP4_SSL) -> str | None:
    """Find the Drafts mailbox name (Gmail/Outlook naming differs)."""
    try:
        _typ, data = box.list()
    except Exception:
        return None
    names: list[str] = []
    for row in data or []:
        try:
            text = row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row)
        except Exception:
            continue
        parts = text.split(' "/" ')
        name = parts[-1].strip().strip('"') if parts else ""
        if name:
            names.append(name)
    lowered = {n.lower(): n for n in names}
    for cand in ("[gmail]/drafts", "drafts", "inbox.drafts", "[gmail]/entwürfe"):
        if cand in lowered:
            return lowered[cand]
    for n in names:
        if "draft" in n.lower():
            return n
    return None


def save_gmail_draft(to: str, subject: str, body: str) -> dict:
    """APPEND a real draft to the Gmail Drafts folder. Returns {"uid"} or {"error"}.

    The draft appears in the Gmail app — editable and sendable there. UID
    comes from the APPENDUID response (UIDPLUS, which Gmail supports); when
    the server doesn't return one, uid is "" and cleanup is best-effort.
    """
    if not configured():
        return {"error": "mail not configured"}
    if not str(to or "").strip() or not str(body or "").strip():
        return {"error": "to and body required"}
    import re as _re
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = os.getenv("MAIL_USER", "").strip()
    msg["To"] = to.strip()
    msg["Subject"] = subject or ""
    msg.set_content(body)
    box = None
    try:
        box = _open()
        mailbox = _drafts_mailbox(box)
        if not mailbox:
            return {"error": "drafts mailbox not found (list failed)"}
        _typ, resp = box.append(mailbox, "\\Draft", None, msg.as_bytes())
        if _typ != "OK":
            return {"error": f"draft append rejected: {str(resp)[:120]}"}
        uid = ""
        try:
            m = _re.search(rb"APPENDUID\s+\S+\s+(\d+)", b" ".join(resp or []))
            if m:
                uid = m.group(1).decode("ascii")
        except Exception:
            uid = ""
        return {"uid": uid, "mailbox": mailbox}
    except Exception as exc:
        return {"error": f"gmail draft failed: {type(exc).__name__}: {str(exc)[:150]}"}
    finally:
        if box is not None:
            with contextlib.suppress(Exception):
                box.close()
            with contextlib.suppress(Exception):
                box.logout()


def delete_gmail_draft(uid: str) -> dict:
    """Delete one Gmail draft copy by UID (post-send cleanup). Best-effort.

    Only ever touches the Drafts mailbox. Empty uid = nothing to do (server
    never returned one) — still success, the SMTP send already happened.
    """
    if not str(uid or "").strip():
        return {"deleted": False, "reason": "no uid"}
    if not configured():
        return {"error": "mail not configured"}
    box = None
    try:
        box = _open()
        mailbox = _drafts_mailbox(box)
        if not mailbox:
            return {"error": "drafts mailbox not found (list failed)"}
        _typ, _ = box.select(mailbox)
        _typ, _ = box.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        with contextlib.suppress(Exception):
            box.expunge()
        return {"deleted": True}
    except Exception as exc:
        return {"error": f"gmail draft delete failed: {type(exc).__name__}: {str(exc)[:150]}"}
    finally:
        if box is not None:
            with contextlib.suppress(Exception):
                box.close()
            with contextlib.suppress(Exception):
                box.logout()

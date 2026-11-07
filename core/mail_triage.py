"""Autonomous mail triage: deterministic tiers, no LLM cost on the hot path.

Tiers: most_important / important / normal / low.
- VIP senders: MAIL_VIP env (comma-separated substrings of From).
- Urgent keywords in subject/snippet: invoice, payment, urgent, asap,
  action required, otp, interview, offer, deadline, failed, alert.
- Bulk markers (List-Unsubscribe / Precedence:bulk) -> low, always.
- Direct + non-bulk -> at least important.
LLM is used ONLY to draft replies for most_important arrivals.
"""

from __future__ import annotations

import os

URGENT_WORDS = (
    "urgent",
    "asap",
    "action required",
    "invoice",
    "payment due",
    "otp",
    "verification code",
    "interview",
    "offer",
    "deadline",
    "failed",
    "failure",
    "alert",
    "immediately",
    "time sensitive",
)

NOTIFY_WORDS = (
    "newsletter",
    "digest",
    "notification",
    "noreply",
    "no-reply",
    "marketing",
    "promo",
    "unsubscribe",
    "notification@",
)

# Expiry / security / credentials — things that cost money or access when
# missed: renewals ending, keys expiring, sign-in alerts, billing problems.
EXPIRY_WORDS = (
    "expir",
    "ending soon",
    "ends soon",
    "renew",
    "subscription",
    "trial end",
    "free trial",
    "billing",
    "payment failed",
    "declined",
    "api key",
    "apikey",
    "access key",
    "token expir",
    "security alert",
    "sign-in",
    "sign in attempt",
    "new device",
    "password reset",
    "password changed",
    "suspend",
    "deactivat",
    "quota",
    "limit reached",
    "verify your",
    "confirm your",
    "last day",
    "final notice",
    "auto-renew",
    "autorenew",
)


def vips() -> list[str]:
    return [v.strip().lower() for v in os.getenv("MAIL_VIP", "").split(",") if v.strip()]


def classify(meta: dict, snippet_text: str = "") -> tuple[str, str]:
    """Return (tier, reason). Pure function — unit-testable, no I/O."""
    frm = (meta.get("from") or "").lower()
    subj = (meta.get("subject") or "").lower()
    body = (snippet_text or "").lower()
    text = f"{subj} {body}"
    for vip in vips():
        if vip and vip in frm:
            return "most_important", f"VIP sender ({vip})"
    if any(w in text for w in URGENT_WORDS):
        if meta.get("bulk"):
            return "important", "urgent keywords but bulk sender"
        return "most_important", "urgent keywords"
    if any(w in text for w in EXPIRY_WORDS):
        if meta.get("bulk"):
            return "important", "expiry keywords but bulk sender"
        return "most_important", "expiry/security keywords"

    if meta.get("bulk") or any(w in frm for w in NOTIFY_WORDS):
        return "low", "bulk/notification sender"
    return "important", "direct personal mail"


def tier_emoji(tier: str) -> str:
    return {"most_important": "🔴", "important": "🟡", "normal": "⚪", "low": "⚫"}.get(tier, "⚪")


def summarize(tiered: dict[str, list]) -> str:
    order = ["most_important", "important", "normal", "low"]
    total = sum(len(v) for v in tiered.values())
    lines = [f"📬 Inbox triage ({total} unread):"]
    for tier in order:
        items = tiered.get(tier, [])
        if not items:
            continue
        lines.append(f"{tier_emoji(tier)} {tier} ({len(items)}):")
        for m in items[:4]:
            lines.append(f"  [{m['id']}] {m['from'][:40]}: {m['subject'][:70]}")
        if len(items) > 4:
            lines.append(f"  …+{len(items) - 4} more")
    return "\n".join(lines)[:2500]


DRAFT_PROMPT = (
    "Draft a short professional reply to this email. Plain text, no placeholders "
    "except the sign-off which must be exactly 'Best regards'. "
    "Keep it under 120 words. Output ONLY the reply body, no explanations.\n\n"
    "From: {frm}\nSubject: {subject}\n\nBody:\n{body}"
)


async def draft_reply(router, frm: str, subject: str, body: str) -> str:
    """One LLM call for a most-important arrival. Returns draft text."""
    prompt = DRAFT_PROMPT.format(frm=frm[:80], subject=subject[:120], body=body[:1500])
    chunks = []
    async for _, piece in router.chat(
        [{"role": "user", "content": prompt}], tools=None, task_type="fast"
    ):
        if isinstance(piece, str):
            chunks.append(piece)
    return "".join(chunks).strip()[:1500]

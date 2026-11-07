"""Storm-proof answers (roadmap #2): drain the storm queue.

Scheduler-fireable every 5 min AND user-invokable. Answers through the
normal router (budgets/breakers enforced — never force-calls a dead tier),
delivers via Telegram + voice, marks delivered. Safe to run from both the
CLI and the API child: items are claimed before answering.
"""

from __future__ import annotations

import contextlib

from core.storm_queue import MAX_PER_DRAIN, StormQueue
from core.tools import Tool


def _queue(orch) -> StormQueue:
    return StormQueue(orch.cfg.db_path.parent)


async def _answer_once(orch, text: str) -> str:
    """Single-shot answer, no tools, no shared-history pollution. Never raises."""
    try:
        msgs = [
            {
                "role": "system",
                "content": "You are JARVIS, a concise personal assistant. Answer briefly.",
            },
            {"role": "user", "content": text},
        ]
        parts: list[str] = []
        async for _name, item in orch.router.chat(msgs):
            if isinstance(item, str):
                parts.append(item)
        answer = "".join(parts).strip()
        return answer or "(empty answer)"
    except Exception as exc:
        return f"__STORM_STILL_DOWN__: {type(exc).__name__}: {str(exc)[:160]}"


def register_storm(box, orch) -> None:
    async def storm_drain(limit: int = MAX_PER_DRAIN) -> str:
        """Answer queued storm requests now that brains may be back. Telegram
        + voice per answer. Safe every 5 min (idempotent claims)."""
        q = _queue(orch)
        try:
            brains = orch.router.available()
        except Exception:
            brains = []
        if not brains:
            n = q.status()["pending"]
            return (
                f"Storm drain skipped: brains still down ({n} queued)."
                if n
                else "Storm queue empty."
            )
        claimed = q.claim(limit=int(limit or MAX_PER_DRAIN))
        if not claimed:
            return "Storm queue empty."
        sent, kept = 0, []
        for it in claimed:
            answer = await _answer_once(orch, it["text"])
            if answer.startswith("__STORM_STILL_DOWN__"):
                q.release(it["id"])  # quota died mid-drain: retry next run
                kept.append(it["id"])
                continue
            try:
                when = str(it.get("ts", "?"))
                msg = f"⛈ Storm answer (queued {when}):\nQ: {it['text'][:500]}\n\n{answer[:3000]}"
                result = await orch.notify_manager.notify(message=msg, title="Storm answer")
                ok = isinstance(result, dict) and result.get("success")
            except Exception:
                ok = False
            if not ok:
                q.release(it["id"])
                kept.append(it["id"])
                continue
            with contextlib.suppress(Exception):
                await orch.speak_alert(
                    "Sir, the brains are back. Your queued answer is on Telegram."
                )
                # text delivered; voice is best-effort
            q.mark_delivered(it["id"], answer)
            sent += 1
        summary = f"Storm drain: {sent} delivered"
        if kept:
            summary += f", {len(kept)} kept queued"
        return summary + "."

    async def storm_status() -> str:
        """Pending storm answers (queued while brains were down)."""
        q = _queue(orch)
        st = q.status()
        if not st["pending"]:
            return "Storm queue empty — no missed questions."
        lines = [f"Storm queue: {st['pending']} pending (oldest {st['oldest']})"]
        for it in q.pending()[:5]:
            lines.append(f"- {it['id']} [{it.get('surface', '?')}] {it['text'][:80]}")
        return "\n".join(lines)

    box.register(
        Tool(
            name="storm_drain",
            description="Answer queued storm requests via Telegram + voice. Safe every 5 min.",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": MAX_PER_DRAIN}},
            },
            handler=storm_drain,
        )
    )
    box.register(
        Tool(
            name="storm_status",
            description="Show pending storm answers queued while brains were down.",
            parameters={"type": "object", "properties": {}},
            handler=storm_status,
        )
    )

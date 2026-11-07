from __future__ import annotations

import time
from datetime import datetime, timedelta


class KnowledgeDistiller:
    """Nightly brain maintenance: compress old conversations into durable facts,
    prune stale memory, and keep the knowledge base healthy.

    The LLM summarizes each finished session (older than `min_age_h`) into
    1-3 compact facts with a cheap/fast model, deduplicates against existing
    memory, and stores the rest. Sessions are marked distilled so they're
    never processed twice.
    """

    DISTILL_PROMPT = """Summarize the following conversation into 1-3 short, durable facts
about the user worth remembering long-term (decisions, preferences, project details,
commitments). One fact per line, each self-contained, prefixed with "- ".
Skip pleasantries and transient chatter. If nothing durable, output exactly "NOTHING".

Conversation:
{conversation}

Durable facts:"""

    def __init__(self, orchestrator):
        self.orch = orchestrator
        self._distilled_session_ids: set[str] = set()
        self._load_markers(orchestrator.persistence.conn)

    # ---------- markers ----------

    def _load_markers(self, conn):
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS distilled_sessions ("
                "session_id TEXT PRIMARY KEY, distilled_at REAL NOT NULL)"
            )
            conn.commit()
            for (sid,) in conn.execute("SELECT session_id FROM distilled_sessions"):
                self._distilled_session_ids.add(sid)
        except Exception:
            pass

    def _mark_distilled(self, session_id: str):
        try:
            conn = self.orch.persistence.conn
            conn.execute(
                "INSERT OR REPLACE INTO distilled_sessions VALUES (?, ?)",
                (session_id, time.time()),
            )
            conn.commit()
            self._distilled_session_ids.add(session_id)
        except Exception:
            pass

    # ---------- distillation ----------

    async def distill_session(self, session_id: str) -> int:
        """Summarize one session into durable memories. Returns facts stored."""
        msgs = self.orch.persistence.get_messages(session_id, limit=200)
        if len(msgs) < 4:
            self._mark_distilled(session_id)
            return 0
        conversation = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in msgs)
        if len(conversation) < 200:
            self._mark_distilled(session_id)
            return 0

        prompt = self.DISTILL_PROMPT.format(conversation=conversation[:12000])
        collected = []
        try:
            async for _name, chunk in self.orch.router.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
                task_type="fast",
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
        except Exception:
            return 0
        text = "".join(collected).strip()

        if not text or "NOTHING" in text.upper()[:40]:
            self._mark_distilled(session_id)
            return 0

        stored = 0
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- "):
                fact = line[2:].strip()
                if 10 <= len(fact) <= 400:
                    result = self.orch.memory.add(fact, kind="distilled")
                    if result == "stored":
                        stored += 1
        self._mark_distilled(session_id)
        return stored

    async def run_nightly(self, max_sessions: int = 12) -> dict:
        """Distill all eligible sessions; prune stale memories."""
        cutoff = datetime.now() - timedelta(hours=6)
        stored_total = 0
        processed = 0
        try:
            sessions = self.orch.persistence.list_sessions(limit=50)
            for s in sessions:
                if processed >= max_sessions:
                    break
                sid = s["id"]
                if sid in self._distilled_session_ids or sid == self.orch.session_id:
                    continue
                if s["updated_at"] and datetime.fromtimestamp(s["updated_at"]) > cutoff:
                    continue  # too recent; let the conversation breathe first
                stored_total += await self.distill_session(sid)
                processed += 1
        except Exception as exc:
            print(f"[Distiller] nightly run error: {exc}")

        pruned = self._prune_memory()
        stats = {
            "sessions_distilled": processed,
            "facts_stored": stored_total,
            "memories_pruned": pruned,
        }
        self.orch.event_bus.publish("distillation_complete", stats)
        return stats

    def _prune_memory(self, max_age_days: float = 120.0) -> int:
        """Remove 'agent_result'/'note' kind memories older than the window.
        Facts and distilled memories are kept forever."""
        try:
            conn = self.orch.memory.conn
            cutoff = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(time.time() - max_age_days * 86400)
            )
            cur = conn.execute(
                "DELETE FROM memories WHERE kind IN ('agent_result', 'note') AND ts < ?",
                (cutoff,),
            )
            conn.commit()
            self.orch.memory._invalidate_vec_cache()
            return cur.rowcount
        except Exception:
            return 0

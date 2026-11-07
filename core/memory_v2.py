"""Memory v2 — automatic episodic/semantic/procedural store.

Boundary with core/memory.py
----------------------------
MemoryV2 (``data/memory_v2.db``) is the *automatic* layer: the orchestrator
writes every conversation turn as episodic records
(``Orchestrator._store_conversation_memory``) and regex-extracts simple
facts into the semantic store (``_extract_and_store_facts``); procedural
skills round out the model. Entries carry importance + decay, and
``consolidate()`` — triggered automatically on episodic writes — decays,
prunes and promotes high-value episodic content into semantic facts with no
user action required. Its search APIs (``search_episodic`` / ``query_facts``
/ ``search_all``) are currently exercised by tests and the briefing
fallback, NOT injected into prompts: prompt-time recall comes from
``core/memory.py`` (curated facts) and ``core/persistence.py`` (past
conversation transcripts). Because nothing here is hand-curated, this DB can
be wiped/reset without losing anything the user deliberately saved. Do not
double-store content that already lives in ``core/memory.py``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from core.config import Config

from .db import connect as _db_connect


@dataclass
class EpisodicMemory:
    id: str
    timestamp: float
    event_type: str  # "conversation", "action", "observation", "task"
    content: str
    context: dict
    importance: float = 1.0
    decay_rate: float = 0.01  # per day
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    embedding: list[float] | None = None


@dataclass
class SemanticFact:
    id: str
    subject: str
    predicate: str
    object: str
    confidence: float
    source: str
    timestamp: float
    importance: float = 1.0
    verified: bool = False
    embedding: list[float] | None = None


@dataclass
class ProceduralSkill:
    id: str
    name: str
    description: str
    steps: list[str]
    triggers: list[str]  # when to use
    success_rate: float = 0.0
    usage_count: int = 0
    last_used: float = 0.0
    created: float = field(default_factory=time.time)


class MemoryV2:
    """Memory v2: Episodic + Semantic + Procedural with consolidation & forgetting."""

    def __init__(self, config: Config):
        self.config = config
        db_path = config.db_path.parent / "memory_v2.db"
        self.conn = _db_connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()

        # In-memory caches
        self.episodic_cache: dict[str, EpisodicMemory] = {}
        self.semantic_cache: dict[str, SemanticFact] = {}
        self.procedural_cache: dict[str, ProceduralSkill] = {}

        # Consolidation settings
        self.consolidation_interval = 3600  # 1 hour
        self.last_consolidation = time.time()
        self.importance_threshold = 0.3
        self.max_episodic = 10000

    def _init_db(self):
        # Episodic memories
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic (
                id TEXT PRIMARY KEY,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                context TEXT,
                importance REAL DEFAULT 1.0,
                decay_rate REAL DEFAULT 0.01,
                last_accessed REAL,
                access_count INTEGER DEFAULT 0,
                embedding TEXT
            )
        """)

        # Semantic facts
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS semantic (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                confidence REAL NOT NULL,
                source TEXT,
                timestamp REAL NOT NULL,
                importance REAL DEFAULT 1.0,
                verified INTEGER DEFAULT 0,
                embedding TEXT
            )
        """)

        # Procedural skills
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS procedural (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                steps TEXT,
                triggers TEXT,
                success_rate REAL DEFAULT 0.0,
                usage_count INTEGER DEFAULT 0,
                last_used REAL DEFAULT 0,
                created REAL
            )
        """)

        # Indexes
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_episodic_time ON episodic(timestamp)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodic_importance ON episodic(importance)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_semantic_subject ON semantic(subject)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_procedural_triggers ON procedural(triggers)"
        )

        self.conn.commit()

    # ===== Episodic Memory =====

    def add_episodic(
        self,
        event_type: str,
        content: str,
        context: dict,
        importance: float = 1.0,
    ) -> str:
        mem_id = str(uuid.uuid4())[:8]
        now = time.time()

        mem = EpisodicMemory(
            id=mem_id,
            timestamp=now,
            event_type=event_type,
            content=content,
            context=context,
            importance=importance,
            last_accessed=now,
        )

        self.conn.execute(
            "INSERT INTO episodic VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mem.id,
                mem.timestamp,
                mem.event_type,
                mem.content,
                json.dumps(mem.context),
                mem.importance,
                mem.decay_rate,
                mem.last_accessed,
                mem.access_count,
                None,
            ),
        )
        self.conn.commit()

        self.episodic_cache[mem_id] = mem
        self._maybe_consolidate()

        return mem_id

    def get_episodic(self, mem_id: str) -> EpisodicMemory | None:
        if mem_id in self.episodic_cache:
            mem = self.episodic_cache[mem_id]
            mem.last_accessed = time.time()
            mem.access_count += 1
            return mem

        row = self.conn.execute("SELECT * FROM episodic WHERE id = ?", (mem_id,)).fetchone()
        if row:
            mem = self._row_to_episodic(row)
            self.episodic_cache[mem_id] = mem
            return mem
        return None

    def search_episodic(
        self,
        query: str,
        k: int = 5,
        event_type: str | None = None,
        time_range: tuple[float, float] | None = None,
    ) -> list[EpisodicMemory]:
        # Simple text search (could use FTS5 or embeddings)
        where = ["content LIKE ?"]
        params = [f"%{query}%"]

        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if time_range:
            where.append("timestamp BETWEEN ? AND ?")
            params.extend(time_range)

        sql = f"SELECT * FROM episodic WHERE {' AND '.join(where)} ORDER BY importance DESC, timestamp DESC LIMIT ?"
        params.append(k)

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_episodic(r) for r in rows]

    def _row_to_episodic(self, row) -> EpisodicMemory:
        return EpisodicMemory(
            id=row["id"],
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            content=row["content"],
            context=json.loads(row["context"] or "{}"),
            importance=row["importance"],
            decay_rate=row["decay_rate"],
            last_accessed=row["last_accessed"],
            access_count=row["access_count"],
        )

    # ===== Semantic Memory =====

    def add_fact(
        self,
        subject: str,
        predicate: str,
        object_: str,
        confidence: float = 0.8,
        source: str = "user",
        importance: float = 1.0,
    ) -> str:
        fact_id = str(uuid.uuid4())[:8]
        now = time.time()

        fact = SemanticFact(
            id=fact_id,
            subject=subject,
            predicate=predicate,
            object=object_,
            confidence=confidence,
            source=source,
            timestamp=now,
            importance=importance,
        )

        self.conn.execute(
            "INSERT INTO semantic VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact.id,
                fact.subject,
                fact.predicate,
                fact.object,
                fact.confidence,
                fact.source,
                fact.timestamp,
                fact.importance,
                int(fact.verified),
                None,
            ),
        )
        self.conn.commit()

        self.semantic_cache[fact_id] = fact
        return fact_id

    def query_facts(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        object_: str | None = None,
        min_confidence: float = 0.5,
    ) -> list[SemanticFact]:
        where = ["confidence >= ?"]
        params = [min_confidence]

        if subject:
            where.append("subject LIKE ?")
            params.append(f"%{subject}%")
        if predicate:
            where.append("predicate LIKE ?")
            params.append(f"%{predicate}%")
        if object_:
            where.append("object LIKE ?")
            params.append(f"%{object_}%")

        sql = f"SELECT * FROM semantic WHERE {' AND '.join(where)} ORDER BY importance DESC, confidence DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def _row_to_fact(self, row) -> SemanticFact:
        return SemanticFact(
            id=row["id"],
            subject=row["subject"],
            predicate=row["predicate"],
            object=row["object"],
            confidence=row["confidence"],
            source=row["source"],
            timestamp=row["timestamp"],
            importance=row["importance"],
            verified=bool(row["verified"]),
        )

    # ===== Procedural Memory =====

    def add_skill(
        self,
        name: str,
        description: str,
        steps: list[str],
        triggers: list[str],
    ) -> str:
        skill_id = str(uuid.uuid4())[:8]
        now = time.time()

        skill = ProceduralSkill(
            id=skill_id,
            name=name,
            description=description,
            steps=steps,
            triggers=triggers,
            created=now,
        )

        self.conn.execute(
            "INSERT OR REPLACE INTO procedural VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                skill.id,
                skill.name,
                skill.description,
                json.dumps(skill.steps),
                json.dumps(skill.triggers),
                skill.success_rate,
                skill.usage_count,
                skill.last_used,
                skill.created,
            ),
        )
        self.conn.commit()

        self.procedural_cache[skill_id] = skill
        return skill_id

    def get_skill(self, name: str) -> ProceduralSkill | None:
        for skill in self.procedural_cache.values():
            if skill.name == name:
                return skill

        row = self.conn.execute("SELECT * FROM procedural WHERE name = ?", (name,)).fetchone()
        if row:
            skill = self._row_to_skill(row)
            self.procedural_cache[skill.id] = skill
            return skill
        return None

    def find_skills_for(self, query: str) -> list[ProceduralSkill]:
        """Skills whose triggers match the query — DB-backed, so skills
        recorded in earlier runs are found after a restart (the in-memory
        cache starts empty on every boot)."""
        query_lower = query.lower()
        skills = []
        for row in self.conn.execute("SELECT * FROM procedural").fetchall():
            skill = self.procedural_cache.get(row["id"])
            if skill is None:
                skill = self._row_to_skill(row)
                self.procedural_cache[skill.id] = skill
            if any(t.lower() in query_lower for t in skill.triggers):
                skills.append(skill)
        return skills

    def record_skill_result(self, skill_id: str, success: bool):
        if skill_id in self.procedural_cache:
            skill = self.procedural_cache[skill_id]
            skill.usage_count += 1
            skill.last_used = time.time()
            # Update success rate (exponential moving average)
            alpha = 0.1
            skill.success_rate = (1 - alpha) * skill.success_rate + alpha * (
                1.0 if success else 0.0
            )

            self.conn.execute(
                "UPDATE procedural SET success_rate = ?, usage_count = ?, last_used = ? WHERE id = ?",
                (skill.success_rate, skill.usage_count, skill.last_used, skill_id),
            )
            self.conn.commit()

    def _row_to_skill(self, row) -> ProceduralSkill:
        return ProceduralSkill(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            steps=json.loads(row["steps"] or "[]"),
            triggers=json.loads(row["triggers"] or "[]"),
            success_rate=row["success_rate"],
            usage_count=row["usage_count"],
            last_used=row["last_used"],
            created=row["created"],
        )

    # ===== Consolidation & Forgetting =====

    def _maybe_consolidate(self):
        now = time.time()
        if now - self.last_consolidation > self.consolidation_interval:
            self.consolidate()
            self.last_consolidation = now

    def consolidate(self):
        """Consolidate important memories, decay unimportant ones."""
        # 1. Decay episodic memories based on time and access
        self._decay_episodic()

        # 2. Promote high-importance episodic to semantic
        self._promote_to_semantic()

        # 3. Update procedural success rates
        self._update_procedural()

        # 4. Prune low-importance memories
        self._prune()

        self.conn.commit()

    def _decay_episodic(self):
        now = time.time()
        rows = self.conn.execute(
            "SELECT id, timestamp, importance, decay_rate, last_accessed, access_count FROM episodic"
        ).fetchall()

        for row in rows:
            days_passed = (now - row["timestamp"]) / 86400

            # Decay based on time and lack of access
            time_decay = math.exp(-row["decay_rate"] * days_passed)
            access_boost = min(row["access_count"] * 0.1, 0.5)

            new_importance = row["importance"] * time_decay + access_boost
            new_importance = max(0.0, min(1.0, new_importance))

            self.conn.execute(
                "UPDATE episodic SET importance = ? WHERE id = ?", (new_importance, row["id"])
            )

    def _promote_to_semantic(self):
        """Promote high-importance episodic memories to semantic facts."""
        rows = self.conn.execute(
            "SELECT * FROM episodic WHERE importance > ? AND event_type = 'fact'", (0.8,)
        ).fetchall()

        for row in rows:
            # Extract fact from content (simple: subject-predicate-object)
            content = row["content"]
            if " is " in content:
                parts = content.split(" is ", 1)
                subject, object_ = parts[0].strip(), parts[1].strip()
                # Skip if this exact fact already exists (hourly re-runs
                # must not duplicate the semantic table).
                dup = self.conn.execute(
                    "SELECT 1 FROM semantic WHERE subject = ? AND predicate = 'is' AND \"object\" = ?",
                    (subject, object_),
                ).fetchone()
                if dup:
                    continue
                self.add_fact(
                    subject=subject,
                    predicate="is",
                    object_=object_,
                    confidence=row["importance"],
                    source="consolidation",
                    importance=row["importance"],
                )

    def _update_procedural(self):
        """Update procedural skill metadata."""
        pass  # Could add skill chaining, etc.

    def _prune(self):
        """Remove memories below threshold."""
        # Episodic
        self.conn.execute("DELETE FROM episodic WHERE importance < ?", (self.importance_threshold,))

        # Limit total count
        count = self.conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0]
        if count > self.max_episodic:
            excess = count - self.max_episodic
            self.conn.execute(
                "DELETE FROM episodic WHERE id IN (SELECT id FROM episodic ORDER BY importance ASC, timestamp ASC LIMIT ?)",
                (excess,),
            )
        # Cap semantic facts too (consolidation + user facts accumulate)
        sem = self.conn.execute("SELECT COUNT(*) FROM semantic").fetchone()[0]
        if sem > 5000:
            self.conn.execute(
                "DELETE FROM semantic WHERE id IN (SELECT id FROM semantic ORDER BY importance ASC, confidence ASC LIMIT ?)",
                (sem - 5000,),
            )

    # ===== Unified Search =====

    def search_all(self, query: str, k: int = 10) -> dict[str, list]:
        """Search across all memory types (per-type budget: max(1, k // 3),
        so small k never starves a category to zero)."""
        per = max(1, k // 3)
        return {
            "episodic": self.search_episodic(query, k=per),
            "semantic": self.query_facts(subject=query, min_confidence=0.3)[:per],
            "procedural": self.find_skills_for(query)[:per],
        }

    def get_stats(self) -> dict:
        ep_count = self.conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0]
        sem_count = self.conn.execute("SELECT COUNT(*) FROM semantic").fetchone()[0]
        proc_count = self.conn.execute("SELECT COUNT(*) FROM procedural").fetchone()[0]

        return {
            "episodic": ep_count,
            "semantic": sem_count,
            "procedural": proc_count,
            "total": ep_count + sem_count + proc_count,
        }

    def close(self):
        self.conn.close()

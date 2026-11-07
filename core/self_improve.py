"""Self-improvement loop: critique/refine cycles, lessons, synthetic data.

Contract: this module is an aux subsystem — failures here must never crash
the chat path. Persistence (cycles, lessons, synthetic examples) is
fail-soft but LOUD: every failed write/read is logged at error level, never
silently swallowed, because silent loss corrupts the improvement statistics
that persona injection and routing decisions rely on.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from core.config import Config
from core.providers import OpenAICompatClient
from core.router import FailoverRouter

from .db import connect as _db_connect

log = logging.getLogger(__name__)


@dataclass
class ImprovementCycle:
    id: str
    task: str
    original_output: str
    critique: str
    refined_output: str
    iterations: int
    score_improvement: float
    issues: list[str] = field(default_factory=list)  # lessons from the initial critique
    timestamp: float = field(default_factory=time.time)


@dataclass
class SyntheticExample:
    id: str
    instruction: str
    input: str
    output: str
    quality_score: float
    source: str  # "self_generated", "distilled", "human"
    created: float = field(default_factory=time.time)


class Critic:
    """Self-critique engine for outputs."""

    CRITIQUE_PROMPT = """You are a harsh but fair critic. Evaluate this response for:

1. **Accuracy** - Factual correctness, no hallucinations
2. **Completeness** - Addresses all parts of the query
3. **Clarity** - Well-structured, easy to understand
4. **Reasoning** - Logical flow, valid arguments
5. **Actionability** - Practical, usable advice
6. **Conciseness** - No fluff, gets to the point

Original Query: {query}
Response: {response}

Provide specific, actionable feedback.
Format:
SCORE: [1-10]
ISSUES:
- [specific issue 1]
- [specific issue 2]
...
STRENGTHS:
- [strength 1]
...
VERDICT: [IMPROVE/GOOD/EXCELLENT]"""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient):
        self.router = router
        self.client = client

    async def critique(
        self,
        query: str,
        response: str,
        task_type: str = "reasoning",
    ) -> dict:
        """Critique a response."""
        prompt = self.CRITIQUE_PROMPT.format(query=query, response=response)

        messages = [
            {
                "role": "system",
                "content": "You are an expert evaluator. Be specific and constructive.",
            },
            {"role": "user", "content": prompt},
        ]

        collected = []
        async for _, chunk in self.router.chat(messages, None, task_type):
            if isinstance(chunk, str):
                collected.append(chunk)

        text = "".join(collected)

        # Parse critique
        score = 5
        issues = []
        strengths = []
        verdict = "IMPROVE"

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("SCORE:"):
                try:
                    score = float(line.split(":")[1].strip().split()[0])
                except (ValueError, IndexError):
                    log.debug("unparseable SCORE line in critique: %r", line)
            elif line.startswith("- ") and "ISSUES" in text[: text.index(line)]:
                issues.append(line[2:])
            elif line.startswith("- ") and "STRENGTHS" in text[: text.index(line)]:
                strengths.append(line[2:])
            elif line.startswith("VERDICT:"):
                verdict = line.split(":")[1].strip()

        return {
            "score": score,
            "issues": issues,
            "strengths": strengths,
            "verdict": verdict,
            "full_critique": text,
        }


class Refiner:
    """Iterative refinement engine."""

    REFINE_PROMPT = """Improve the previous response based on this critique:

CRITIQUE:
{critique}

ORIGINAL QUERY: {query}

ORIGINAL RESPONSE:
{response}

Provide the complete improved response. Do not include the critique or explanation."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient):
        self.router = router
        self.client = client

    async def refine(
        self,
        query: str,
        response: str,
        critique: dict,
        task_type: str = "reasoning",
    ) -> str:
        """Refine response based on critique."""
        prompt = self.REFINE_PROMPT.format(
            critique=critique["full_critique"], query=query, response=response
        )

        messages = [
            {
                "role": "system",
                "content": "You are improving a response based on feedback. Provide only the improved response.",
            },
            {"role": "user", "content": prompt},
        ]

        collected = []
        async for _, chunk in self.router.chat(messages, None, task_type):
            if isinstance(chunk, str):
                collected.append(chunk)

        return "".join(collected)


class Distiller:
    """Knowledge distillation: generate synthetic data, distill to smaller models."""

    SYNTHETIC_GEN_PROMPT = """Generate a high-quality training example for an AI assistant.

Topic: {topic}
Difficulty: {difficulty} (easy/medium/hard)
Style: {style} (instructional, conversational, code, reasoning, creative)

Generate:
1. A clear instruction/question
2. Optional input/context
3. A high-quality response

Format as JSON:
{
  "instruction": "...",
  "input": "...",
  "output": "...",
  "topic": "...",
  "difficulty": "..."
}"""

    DISTILL_PROMPT = """You are a master teacher. Create a clear, step-by-step explanation that a smaller model could learn from.

Original response: {response}
Key concepts to preserve: {concepts}

Create a distilled version that:
- Uses simpler language
- Breaks into clear steps
- Includes examples
- Highlights key principles
- Removes jargon where possible"""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, config: Config):
        self.router = router
        self.client = client
        self.config = config
        self.examples_path = config.db_path.parent / "synthetic_data.jsonl"
        self._load_examples()

    def _load_examples(self):
        self.examples: list[dict] = []
        if self.examples_path.exists():
            for line in self.examples_path.read_text(encoding="utf-8").splitlines():
                try:
                    self.examples.append(json.loads(line))
                except json.JSONDecodeError:
                    log.debug("skipping malformed line in %s", self.examples_path)

    def _save_example(self, example: dict):
        self.examples.append(example)
        try:
            with open(self.examples_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(example, ensure_ascii=False) + "\n")
        except OSError as exc:
            # Example stays in memory; a write hiccup must not crash the loop,
            # but it must not vanish silently either.
            log.error("failed to persist synthetic example to %s: %s", self.examples_path, exc)

    async def generate_synthetic(
        self,
        topic: str,
        difficulty: str = "medium",
        style: str = "instructional",
        count: int = 1,
    ) -> list[dict]:
        """Generate synthetic training examples."""
        results = []

        for _ in range(count):
            prompt = self.SYNTHETIC_GEN_PROMPT.format(
                topic=topic, difficulty=difficulty, style=style
            )

            messages = [
                {
                    "role": "system",
                    "content": "You are generating high-quality training data. Output valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ]

            collected = []
            async for _, chunk in self.router.chat(messages, None, "coding"):
                if isinstance(chunk, str):
                    collected.append(chunk)

            text = "".join(collected)
            try:
                example = json.loads(text)
                example["id"] = str(uuid.uuid4())[:8]
                example["source"] = "self_generated"
                example["created"] = time.time()
                example["quality_score"] = 0.8  # default
                self._save_example(example)
                results.append(example)
            except json.JSONDecodeError:
                log.warning("synthetic example %d/%d was not valid JSON; skipped", _ + 1, count)

        return results

    async def distill_response(
        self,
        query: str,
        response: str,
        concepts: list[str],
    ) -> str:
        """Distill a response into a teachable format."""
        prompt = self.DISTILL_PROMPT.format(response=response, concepts=", ".join(concepts))

        messages = [
            {
                "role": "system",
                "content": "You are distilling knowledge for a smaller model. Be clear and pedagogical.",
            },
            {"role": "user", "content": prompt},
        ]

        collected = []
        async for _, chunk in self.router.chat(messages, None, "reasoning"):
            if isinstance(chunk, str):
                collected.append(chunk)

        return "".join(collected)

    def get_training_data(self, limit: int = 1000) -> list[dict]:
        """Get synthetic data for training."""
        return self.examples[-limit:]


class SelfImprovementLoop:
    """Complete self-improvement loop: critique → refine → distill → persist.

    P12: cycles persist to SQLite so learnings survive restarts; the loop
    is rate-limited (min interval between auto-runs) to control token burn;
    each cycle's critique issues are distilled into lessons that the
    persona prompt can absorb.
    """

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, config: Config):
        self.critic = Critic(router, client)
        self.refiner = Refiner(router, client)
        self.distiller = Distiller(router, client, config)
        self.config = config

        self.cycles: list[ImprovementCycle] = []
        self.max_iterations = 3
        self.improvement_threshold = 1.5  # minimum score improvement to continue

        # Rate limit: at most one auto cycle per interval (token burn control)
        self.min_interval_s: float = 600.0  # 10 minutes
        self._last_auto_run: float = 0.0

        # Persistence
        self.db_path = config.db_path.parent / "improvement_cycles.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = _db_connect(self.db_path)
        self._init_db()
        self._load_cycles()

    # ---------- persistence ----------

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS cycles (
                id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                original_output TEXT NOT NULL,
                critique TEXT,
                refined_output TEXT,
                iterations INTEGER,
                score_improvement REAL,
                issues TEXT,
                created_at REAL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue TEXT NOT NULL,
                task TEXT,
                occurrences INTEGER DEFAULT 1,
                first_seen REAL,
                last_seen REAL
            )
        """)
        self._conn.commit()

    def _persist_cycle(self, cycle: ImprovementCycle):
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO cycles VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    cycle.id,
                    cycle.task,
                    cycle.original_output[:2000],
                    cycle.critique[:4000],
                    cycle.refined_output[:2000],
                    cycle.iterations,
                    cycle.score_improvement,
                    json.dumps(cycle.issues[:10]),
                    cycle.timestamp,
                ),
            )
            self._conn.commit()
        except Exception as exc:
            # Persistence is aux state: never crash the improvement loop,
            # but never lose a cycle silently either.
            log.error("failed to persist improvement cycle %s: %s", cycle.id, exc)

    def _record_lessons(self, issues: list[str], task: str):
        """Distill recurring critique issues into lessons (deduped, counted)."""
        now = time.time()
        try:
            for issue in issues[:8]:
                key = issue.strip().lower()[:160]
                if not key:
                    continue
                row = self._conn.execute(
                    "SELECT id, occurrences FROM lessons WHERE issue = ?", (key,)
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE lessons SET occurrences = ?, last_seen = ? WHERE id = ?",
                        (row[1] + 1, now, row[0]),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO lessons (issue, task, occurrences, first_seen, last_seen) VALUES (?,?,1,?,?)",
                        (key, task[:200], now, now),
                    )
            self._conn.commit()
        except Exception as exc:
            log.error("failed to record lessons for task %.60s: %s", task, exc)

    def _load_cycles(self):
        """Reload recent cycles at boot (stats + lessons survive restarts)."""
        try:
            rows = self._conn.execute(
                "SELECT id, task, original_output, critique, refined_output, iterations, "
                "score_improvement, issues, created_at FROM cycles ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            for r in rows:
                try:
                    issues = json.loads(r[7]) if r[7] else []
                except Exception:
                    issues = []
                self.cycles.append(
                    ImprovementCycle(
                        id=r[0],
                        task=r[1],
                        original_output=r[2],
                        critique=r[3] or "",
                        refined_output=r[4] or "",
                        iterations=r[5],
                        score_improvement=r[6],
                        issues=issues,
                        timestamp=r[8],
                    )
                )
            # newest-last so append order stays chronological
            self.cycles.reverse()
        except Exception as exc:
            log.error("failed to reload improvement cycles from db: %s", exc)

    def top_lessons(self, limit: int = 5, min_occurrences: int = 2) -> list[str]:
        """Recurring critique issues — candidates for persona prompt injection."""
        try:
            rows = self._conn.execute(
                "SELECT issue FROM lessons WHERE occurrences >= ? "
                "ORDER BY occurrences DESC LIMIT ?",
                (min_occurrences, limit),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception as exc:
            log.error("failed to query lessons: %s", exc)
            return []

    # ---------- rate limiting ----------

    def should_auto_run(self) -> bool:
        """True if enough time passed since the last automatic cycle."""
        return (time.time() - self._last_auto_run) >= self.min_interval_s

    def mark_auto_run(self):
        self._last_auto_run = time.time()

    # ---------- the loop ----------

    async def improve(
        self,
        query: str,
        initial_response: str,
        task_type: str = "reasoning",
        max_iterations: int = None,
    ) -> ImprovementCycle:
        """Run one improvement cycle: critique once, refine until good.

        P12 efficiency: the initial critique is REUSED (no duplicate call),
        and the final score comes from the last critique instead of a
        fresh one — 2 LLM calls saved per cycle.
        """
        max_iter = max_iterations or self.max_iterations

        current_response = initial_response
        initial_score = 0.0
        final_critique_text = ""
        final_score = 0.0
        iterations = 0
        first_issues: list[str] = []

        for iteration in range(max_iter):
            critique = await self.critic.critique(query, current_response, task_type)
            iterations = iteration + 1
            score = critique["score"]

            if iteration == 0:
                initial_score = score
                first_issues = list(critique.get("issues", []))

            final_score = score
            final_critique_text = critique["full_critique"]

            if critique["verdict"] in ("GOOD", "EXCELLENT") or score >= 8:
                break

            if iteration == max_iter - 1:
                break

            # Refine
            current_response = await self.refiner.refine(
                query, current_response, critique, task_type
            )

        cycle = ImprovementCycle(
            id=str(uuid.uuid4())[:8],
            task=query,
            original_output=initial_response,
            critique=final_critique_text,
            refined_output=current_response,
            iterations=iterations,
            score_improvement=final_score - initial_score,
            issues=first_issues,
        )

        self.cycles.append(cycle)
        self._persist_cycle(cycle)
        if first_issues:
            self._record_lessons(first_issues, query)
        return cycle

    async def distill_knowledge(
        self,
        query: str,
        response: str,
        concepts: list[str],
    ) -> dict:
        """Distill response into synthetic training data."""
        # Generate distilled explanation
        distilled = await self.distiller.distill_response(query, response, concepts)

        # Create synthetic example
        example = {
            "id": str(uuid.uuid4())[:8],
            "instruction": query,
            "input": "",
            "output": distilled,
            "topic": concepts[0] if concepts else "general",
            "difficulty": "medium",
            "source": "distilled",
            "quality_score": 0.9,
            "created": time.time(),
        }

        self.distiller._save_example(example)

        return example

    def get_stats(self) -> dict:
        return {
            "total_cycles": len(self.cycles),
            "avg_improvement": sum(c.score_improvement for c in self.cycles)
            / max(len(self.cycles), 1),
            "synthetic_examples": len(self.distiller.examples),
            "avg_iterations": sum(c.iterations for c in self.cycles) / max(len(self.cycles), 1),
            "lessons_learned": self.lesson_count(),
        }

    def lesson_count(self) -> int:
        try:
            return self._conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        except Exception as exc:
            log.error("failed to count lessons: %s", exc)
            return 0

    def close(self):
        try:
            self._conn.close()
        except Exception as exc:
            log.warning("failed to close self_improve db: %s", exc)

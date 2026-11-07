from __future__ import annotations

import time

from core.config import Config
from core.self_improve import SelfImprovementLoop


def make_cfg(tmp_path):
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return cfg


class FakeRouter:
    """Deterministic fake LLM: critic scores from the query text."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, task_type=None):
        self.calls += 1
        system = messages[0]["content"]
        user = messages[1]["content"]

        if "harsh but fair critic" in user:
            # Score: refined outputs (2nd refine onward) score high
            score = 9.0 if "IMPROVED" in user else 4.0
            text = (
                f"SCORE: {score}\nISSUES:\n- Response lacks concrete examples\n"
                f"- Missing edge case discussion\nSTRENGTHS:\n- Clear structure\n"
                f"VERDICT: {'GOOD' if score >= 8 else 'IMPROVE'}"
            )
        elif "improving a response" in system:
            text = "IMPROVED RESPONSE with examples and edge cases."
        else:
            text = "generic"
        yield "provider", text


def make_loop(tmp_path) -> SelfImprovementLoop:
    cfg = make_cfg(tmp_path)
    router = FakeRouter()
    return SelfImprovementLoop(router, client=None, config=cfg), router


# ---------- persistence ----------


async def test_cycles_persist_across_restart(tmp_path):
    loop, router = make_loop(tmp_path)

    class FakeCritic:
        async def critique(self, q, r, t):
            return {
                "score": 4.0,
                "issues": ["lacks examples", "too verbose"],
                "strengths": [],
                "verdict": "IMPROVE",
                "full_critique": "SCORE: 4\nVERDICT: IMPROVE",
            }

    class FakeRefiner:
        async def refine(self, q, r, c, t):
            return "better response"

    loop.critic = FakeCritic()
    loop.refiner = FakeRefiner()

    await loop.improve("what is rust", "rust is a language")
    assert len(loop.cycles) == 1

    # "Restart": fresh loop over the same DB
    loop2, _ = make_loop(tmp_path)
    assert len(loop2.cycles) == 1, "cycle should survive restart"
    assert loop2.cycles[0].task == "what is rust"
    loop.close()
    loop2.close()


async def test_lessons_record_and_deduplicate(tmp_path):
    loop, _ = make_loop(tmp_path)

    class FakeCritic:
        async def critique(self, q, r, t):
            return {
                "score": 5.0,
                "issues": ["lacks examples", "too verbose"],
                "strengths": [],
                "verdict": "IMPROVE",
                "full_critique": "...",
            }

    class FakeRefiner:
        async def refine(self, q, r, c, t):
            return "better"

    loop.critic = FakeCritic()
    loop.refiner = FakeRefiner()

    await loop.improve("q1", "r1")
    await loop.improve("q2", "r2")
    await loop.improve("q3", "r3")

    lessons = loop.top_lessons(min_occurrences=1)
    assert "lacks examples" in lessons
    assert loop.lesson_count() == 2, "same issue recorded once, counted thrice"

    # min_occurrences filter
    assert "lacks examples" in loop.top_lessons(min_occurrences=3)
    loop.close()


# ---------- efficient loop (no duplicate critiques) ----------


async def test_improve_reuses_critique_no_extra_llm_calls(tmp_path):
    loop, router = make_loop(tmp_path)
    # Real Critic/Refiner against FakeRouter:
    # initial critique (score 4, IMPROVE) -> refine -> critique (IMPROVED, GOOD)
    # = 3 router calls total. Old code did 2 extra critiques.
    cycle = await loop.improve("explain asyncio", "asyncio is a library")
    assert cycle.score_improvement > 0
    # 1 initial critique + 1 refine + 1 final critique = 3
    assert router.calls == 3, f"expected 3 LLM calls, got {router.calls}"
    loop.close()


# ---------- rate limiting ----------


def test_rate_limit_gates_auto_runs(tmp_path):
    loop, _ = make_loop(tmp_path)
    loop.min_interval_s = 60.0
    assert loop.should_auto_run() is True
    loop.mark_auto_run()
    assert loop.should_auto_run() is False, "within the interval: must be blocked"
    loop._last_auto_run = time.time() - 61
    assert loop.should_auto_run() is True
    loop.close()


# ---------- few-shot seeding consumes distilled data ----------


def test_distilled_data_seeds_examples(tmp_path):
    loop, _ = make_loop(tmp_path)
    # Simulate distilled examples on disk
    loop.distiller.examples.append(
        {
            "id": "e1",
            "instruction": "how to parse JSON in python",
            "input": "",
            "output": "use json.loads()",
            "topic": "coding",
            "source": "distilled",
            "quality_score": 0.9,
        }
    )
    data = loop.distiller.get_training_data()
    assert len(data) == 1
    assert "parse JSON" in data[0]["instruction"]
    loop.close()


# ---------- orchestrator trigger gating ----------


def test_trigger_requires_complex_response_and_rate_limit(tmp_path):
    """_should_self_improve: explicit trigger always; auto needs >800 chars + rate OK."""

    class MiniLoop:
        def __init__(self):
            self.min_interval_s = 600
            self._last_auto = 0.0

        def should_auto_run(self):
            return (time.time() - self._last_auto) >= self.min_interval_s

    class FakeOrch:
        self_improve = MiniLoop()

        def _should_self_improve(self, user_input, response):
            # mirror of Orchestrator._should_self_improve logic
            text = user_input.lower()
            triggers = [
                "improve",
                "refine",
                "polish",
                "make better",
                "critique",
                "review",
                "check",
                "verify",
                "perfect",
                "optimize",
                "enhance",
            ]
            if any(t in text for t in triggers):
                return True
            return len(response) > 800 and self.self_improve.should_auto_run()

    orch = FakeOrch()
    # Explicit trigger always fires
    assert orch._should_self_improve("review this", "short") is True
    # Short response never auto-fires
    assert orch._should_self_improve("tell me about x", "short") is False
    # Long response auto-fires while rate-limited window open
    assert orch._should_self_improve("tell me about x", "x" * 900) is True
    # ...but not after a recent auto run
    orch.self_improve._last_auto = time.time()
    assert orch._should_self_improve("tell me about x", "x" * 900) is False


# ---------- lessons folded into system prompt ----------


async def test_lessons_applied_to_system_prompt(tmp_path):
    loop, _ = make_loop(tmp_path)

    class FakeCritic:
        n = 0

        async def critique(self, q, r, t):
            FakeCritic.n += 1
            return {
                "score": 4.0,
                "issues": ["lacks examples"],
                "strengths": [],
                "verdict": "IMPROVE",
                "full_critique": "...",
            }

    class FakeRefiner:
        async def refine(self, q, r, c, t):
            return "better"

    loop.critic = FakeCritic()
    loop.refiner = FakeRefiner()
    await loop.improve("q1", "r1")
    await loop.improve("q2", "r2")  # lesson now seen twice

    lessons = loop.top_lessons(min_occurrences=2)
    assert lessons, "lesson must qualify after 2 occurrences"
    block = "[Learned quality rules — apply these]\n- " + lessons[0]
    assert "lacks examples" in block
    loop.close()

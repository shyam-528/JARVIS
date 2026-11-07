"""Prompt optimization engine: versioned prompt templates, few-shot example
curation, and model-driven prompt mutation/evaluation.

Failure policy (Wave 7):
- Persisted state (prompts.json, examples.jsonl) is EVOLVED user value: a
  corrupt file is announced loudly, never silently reset to empty.
- Model-driven generation that returns unparseable output reports [] with a
  loud diagnostic naming the cause.
- render_prompt returns an error string for missing variables (fail-loud).
"""

from __future__ import annotations

import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from core.config import Config
from core.providers import OpenAICompatClient
from core.router import FailoverRouter
from utils.json_utils import load_json_store, save_json_atomic


@dataclass
class PromptTemplate:
    """A versioned prompt template with metadata."""

    id: str
    name: str
    template: str
    variables: list[str]
    version: int = 1
    created: float = field(default_factory=time.time)
    parent_id: str | None = None
    metrics: dict = field(default_factory=dict)  # accuracy, latency, etc.


@dataclass
class Example:
    """A few-shot example."""

    id: str
    input: str
    output: str
    quality_score: float = 0.5
    tags: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)


@dataclass
class OptimizationResult:
    """Result of a prompt optimization run."""

    prompt_id: str
    baseline_score: float
    optimized_score: float
    improvement: float
    iterations: int
    best_prompt: str
    best_examples: list[Example]


class PromptOptimizer:
    """DSPy-style prompt optimizer: automatic prompt engineering with metrics."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, config: Config):
        self.router = router
        self.client = client
        self.config = config

        # Storage
        self.prompts: dict[str, PromptTemplate] = {}
        self.examples: dict[str, Example] = {}
        self.optimization_history: list[OptimizationResult] = []

        # Optimization settings
        self.max_iterations = 5
        self.population_size = 4
        self.mutation_rate = 0.3

        # Load persisted data
        self._load()

    def _load(self):
        """Load prompts and examples from disk."""
        prompts_path = self.config.db_path.parent / "prompts.json"
        examples_path = self.config.db_path.parent / "examples.jsonl"

        # P0: shared corrupt-file contract for the JSON store — quarantine
        # aside (loud) instead of starting empty over a corrupt file.
        data = load_json_store(prompts_path, default={}, owner="prompt_opt", expect=dict)
        for k, v in data.items():
            try:
                self.prompts[k] = PromptTemplate(**v)
            except Exception as exc:
                print(f"[prompt_opt] skipping malformed prompt {k!r}: {exc}")

        if examples_path.exists():
            # JSONL: skip malformed LINES individually so one bad line can't
            # sink the curated few-shot library.
            bad = 0
            try:
                for line in examples_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        ex = json.loads(line)
                        self.examples[ex["id"]] = Example(**ex)
                    except Exception:
                        bad += 1
            except Exception as exc:
                print(
                    f"[prompt_opt] corrupt examples file {examples_path}: {exc} — kept what parsed"
                )
            if bad:
                print(f"[prompt_opt] skipped {bad} malformed example lines in {examples_path.name}")

    def _save_prompts(self):
        prompts_path = self.config.db_path.parent / "prompts.json"
        save_json_atomic(prompts_path, {k: v.__dict__ for k, v in self.prompts.items()})

    def _save_example(self, example: Example):
        examples_path = self.config.db_path.parent / "examples.jsonl"
        with open(examples_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(example.__dict__, ensure_ascii=False) + "\n")

    # ===== Prompt Management =====

    def register_prompt(self, name: str, template: str, variables: list[str]) -> str:
        """Register a new prompt template."""
        prompt_id = str(uuid.uuid4())[:8]
        prompt = PromptTemplate(
            id=prompt_id,
            name=name,
            template=template,
            variables=variables,
        )
        self.prompts[prompt_id] = prompt
        self._save_prompts()
        return prompt_id

    def get_prompt(self, prompt_id: str) -> PromptTemplate | None:
        return self.prompts.get(prompt_id)

    def list_prompts(self) -> list[PromptTemplate]:
        return list(self.prompts.values())

    # ===== Example Management =====

    def add_example(self, input_text: str, output_text: str, tags: list[str] = None) -> str:
        """Add a few-shot example."""
        ex_id = str(uuid.uuid4())[:8]
        ex = Example(
            id=ex_id,
            input=input_text,
            output=output_text,
            tags=tags or [],
        )
        self.examples[ex_id] = ex
        self._save_example(ex)
        return ex_id

    def get_examples(self, tags: list[str] = None, limit: int = 10) -> list[Example]:
        """Get examples, optionally filtered by tags."""
        filtered = list(self.examples.values())
        if tags:
            filtered = [e for e in filtered if any(t in e.tags for t in tags)]
        filtered.sort(key=lambda e: e.quality_score, reverse=True)
        return filtered[:limit]

    # ===== Prompt Rendering =====

    def render_prompt(self, prompt_id: str, **kwargs) -> str | None:
        """Render a prompt template with variables."""
        prompt = self.prompts.get(prompt_id)
        if not prompt:
            return None

        try:
            return prompt.template.format(**kwargs)
        except KeyError as e:
            return f"Error: missing variable {e}"

    # ===== Prompt Optimization =====

    async def optimize_prompt(
        self,
        task_name: str,
        base_prompt: str,
        test_cases: list[dict],  # [{"input": "...", "expected": "..."}]
        metric_fn: Callable[[str, str], float],  # (output, expected) -> score
        max_iterations: int = None,
    ) -> OptimizationResult:
        """Optimize a prompt using evolutionary search."""

        max_iter = max_iterations or self.max_iterations

        # Evaluate baseline
        baseline_scores = []
        for case in test_cases:
            output = await self._run_prompt(base_prompt, case["input"])
            score = metric_fn(output, case.get("expected", ""))
            baseline_scores.append(score)

        baseline_avg = sum(baseline_scores) / len(baseline_scores) if baseline_scores else 0

        # Initialize population
        population = [self._mutate_prompt(base_prompt) for _ in range(self.population_size - 1)]
        population.append(base_prompt)

        best_prompt = base_prompt
        best_score = baseline_avg

        for _iteration in range(max_iter):
            # Evaluate population
            scored_population = []
            for p in population:
                scores = []
                for case in test_cases:
                    output = await self._run_prompt(p, case["input"])
                    scores.append(metric_fn(output, case.get("expected", "")))
                avg_score = sum(scores) / len(scores) if scores else 0
                scored_population.append((p, avg_score))

            # Select best
            scored_population.sort(key=lambda x: x[1], reverse=True)
            best_prompt, best_score = scored_population[0]

            if best_score <= baseline_avg:
                break

            # Generate next generation
            new_population = [best_prompt]
            for _ in range(self.population_size - 1):
                parent = random.choice(scored_population[:2])[0]  # top 2
                new_population.append(self._mutate_prompt(parent))

            population = new_population

        result = OptimizationResult(
            prompt_id=str(uuid.uuid4())[:8],
            baseline_score=baseline_avg,
            optimized_score=best_score,
            improvement=best_score - baseline_avg,
            iterations=_iteration + 1,
            best_prompt=best_prompt,
            best_examples=[],
        )

        self.optimization_history.append(result)
        return result

    def _mutate_prompt(self, prompt: str) -> str:
        """Mutate a prompt with various strategies."""
        mutations = [
            self._add_instruction,
            self._add_step_by_step,
            self._add_examples,
            self._rephrase,
            self._add_constraints,
        ]

        mutation = random.choice(mutations)
        return mutation(prompt)

    def _add_instruction(self, prompt: str) -> str:
        additions = [
            "Think step by step.",
            "Be concise and precise.",
            "Consider edge cases.",
            "Verify your answer before responding.",
            "Use clear structure with headings.",
        ]
        return prompt + "\n\n" + random.choice(additions)

    def _add_step_by_step(self, prompt: str) -> str:
        if "step by step" not in prompt.lower():
            return prompt + "\n\nThink step by step and show your reasoning."
        return prompt

    def _add_examples(self, prompt: str) -> str:
        if "example" not in prompt.lower():
            ex = self.get_examples(limit=2)
            if ex:
                ex_text = "\n".join([f"Example: Input: {e.input}\nOutput: {e.output}" for e in ex])
                return prompt + f"\n\nExamples:\n{ex_text}"
        return prompt

    def _rephrase(self, prompt: str) -> str:
        """Simple rephrasing - in practice would use an LLM."""
        # Simple word swaps
        replacements = {
            "explain": "describe",
            "analyze": "examine",
            "solve": "resolve",
            "create": "generate",
            "improve": "enhance",
        }
        result = prompt
        for old, new in replacements.items():
            if old in result.lower() and random.random() < 0.3:
                result = result.replace(old, new)
        return result

    def _add_constraints(self, prompt: str) -> str:
        constraints = [
            "Keep response under 500 words.",
            "Use bullet points for lists.",
            "Include code examples where relevant.",
            "Cite sources when making claims.",
        ]
        return prompt + "\n\nConstraints:\n" + "\n".join(random.sample(constraints, 2))

    async def _run_prompt(self, prompt: str, input_text: str) -> str:
        """Run a prompt with input and return output."""
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": input_text}]

        output = ""
        async for _, chunk in self.router.chat(messages, None, "reasoning"):
            if isinstance(chunk, str):
                output += chunk
        return output

    # ===== Few-Shot Selection =====

    def select_few_shot(
        self,
        task_input: str,
        candidate_examples: list[Example],
        k: int = 3,
        strategy: str = "similarity",
    ) -> list[Example]:
        """Select best few-shot examples for a task."""

        if strategy == "similarity":
            # Simple keyword overlap similarity
            scored = []
            task_words = set(task_input.lower().split())
            for ex in candidate_examples:
                ex_words = set(ex.input.lower().split())
                overlap = len(task_words & ex_words)
                scored.append((overlap, ex))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:k]]

        elif strategy == "quality":
            return sorted(candidate_examples, key=lambda e: e.quality_score, reverse=True)[:k]

        elif strategy == "diversity":
            # Pick diverse examples (different tags)
            selected = []
            used_tags = set()
            for ex in sorted(candidate_examples, key=lambda e: e.quality_score, reverse=True):
                if len(selected) >= k:
                    break
                if not any(t in used_tags for t in ex.tags) or not selected:
                    selected.append(ex)
                    used_tags.update(ex.tags)
            return selected

        return candidate_examples[:k]

    # ===== Prompt A/B Testing =====

    async def ab_test(
        self,
        prompt_a: str,
        prompt_b: str,
        test_cases: list[dict],
        metric_fn: Callable[[str, str], float],
        samples: int = 10,
    ) -> dict:
        """A/B test two prompts."""

        scores_a = []
        scores_b = []

        for _ in range(samples):
            for case in test_cases:
                out_a = await self._run_prompt(prompt_a, case["input"])
                out_b = await self._run_prompt(prompt_b, case["input"])
                scores_a.append(metric_fn(out_a, case.get("expected", "")))
                scores_b.append(metric_fn(out_b, case.get("expected", "")))

        avg_a = sum(scores_a) / len(scores_a) if scores_a else 0
        avg_b = sum(scores_b) / len(scores_b) if scores_b else 0

        return {
            "prompt_a_score": avg_a,
            "prompt_b_score": avg_b,
            "winner": "A" if avg_a > avg_b else "B",
            "difference": abs(avg_a - avg_b),
        }

    # ===== Bootstrap Examples =====

    async def bootstrap_examples(
        self,
        task_description: str,
        count: int = 10,
    ) -> list[Example]:
        """Generate bootstrapped examples from successful interactions."""

        prompt = f"""Generate {count} high-quality input/output pairs for this task:

Task: {task_description}

Each pair should be:
- Realistic and diverse
- Clear input with expected output
- Covering edge cases

Format as JSON array:
[
  {{"input": "...", "output": "...", "tags": [...]}},
  ...
]"""

        messages = [
            {"role": "system", "content": "Generate training data. Output valid JSON array only."},
            {"role": "user", "content": prompt},
        ]

        output = ""
        async for _, chunk in self.router.chat(messages, None, "coding"):
            if isinstance(chunk, str):
                output += chunk

        try:
            data = json.loads(output)
            examples = []
            for item in data[:count]:
                ex = Example(
                    id=str(uuid.uuid4())[:8],
                    input=item.get("input", ""),
                    output=item.get("output", ""),
                    tags=item.get("tags", []),
                    quality_score=0.7,
                )
                self.examples[ex.id] = ex
                self._save_example(ex)
                examples.append(ex)
            return examples
        except Exception as exc:
            # Model returned unparseable JSON (or a bad item shape) — the
            # caller gets [], and the console says why
            print(
                f"[prompt_opt] example generation produced unparseable output: {type(exc).__name__}: {exc}"
            )
            return []

    # ===== Statistics =====

    def get_stats(self) -> dict:
        return {
            "total_prompts": len(self.prompts),
            "total_examples": len(self.examples),
            "optimizations_run": len(self.optimization_history),
            "avg_improvement": sum(r.improvement for r in self.optimization_history)
            / max(len(self.optimization_history), 1),
        }

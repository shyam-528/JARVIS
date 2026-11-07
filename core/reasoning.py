from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from core.providers import OpenAICompatClient, ToolCallDelta
from core.router import FailoverRouter


@dataclass
class ReasoningStep:
    step_type: str  # "thought", "action", "observation", "reflection"
    content: str
    metadata: dict = field(default_factory=dict)


@dataclass
class ReasoningTrace:
    steps: list[ReasoningStep] = field(default_factory=list)
    final_answer: str = ""
    confidence: float = 0.0


class ChainOfThought:
    """Chain-of-Thought prompting for step-by-step reasoning."""

    COT_PROMPT = """Let's think step by step.

Break down the problem into clear reasoning steps.
Show your work explicitly.
Conclude with a final answer."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient):
        self.router = router
        self.client = client

    async def reason(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "reasoning",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        cot_messages = messages + [{"role": "user", "content": self.COT_PROMPT}]
        async for item in self.router.chat(cot_messages, tools, task_type):
            yield item


class TreeOfThoughts:
    """Tree-of-Thoughts: explore multiple reasoning paths, evaluate, select best."""

    TO_T_GENERATE = """Generate {n} distinct reasoning approaches for this problem.
Each approach should be a complete reasoning path.
Format as:
APPROACH 1: ...
APPROACH 2: ...
..."""

    TO_T_EVALUATE = """Evaluate these {n} approaches for correctness, completeness, and efficiency.
Score each 1-10.
Select the best approach.
Best approach: [number]"""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, branches: int = 3):
        self.router = router
        self.client = client
        self.branches = branches

    async def reason(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "reasoning",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        # Generate branches
        gen_messages = messages + [
            {"role": "user", "content": self.TO_T_GENERATE.format(n=self.branches)}
        ]

        approaches = []
        async for _, chunk in self.router.chat(gen_messages, None, task_type):
            if isinstance(chunk, str):
                approaches.append(chunk)

        approaches_text = "".join(approaches)

        # Evaluate and select best
        eval_messages = [
            {"role": "user", "content": approaches_text},
            {"role": "user", "content": self.TO_T_EVALUATE.format(n=self.branches)},
        ]

        async for item in self.router.chat(eval_messages, tools, task_type):
            yield item


class ReActAgent:
    """ReAct: Reasoning + Acting with tools."""

    REACT_PROMPT = """You are a ReAct agent. Follow this format:

Question: {question}
Thought: [your reasoning]
Action: [tool_name]
Action Input: [json_args]
Observation: [tool_result]
... (repeat until done)
Thought: [final reasoning]
Final Answer: [answer]

Available tools: {tools}

Begin!"""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, max_steps: int = 10):
        self.router = router
        self.client = client
        self.max_steps = max_steps

    async def run(
        self,
        question: str,
        tools: list[dict],
        task_type: str = "reasoning",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        tool_desc = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']}" for t in tools
        )
        prompt = self.REACT_PROMPT.format(question=question, tools=tool_desc)

        messages = [{"role": "user", "content": prompt}]

        for _step in range(self.max_steps):
            collected = []
            async for name, chunk in self.router.chat(messages, tools, task_type):
                collected.append(chunk)
                yield name, chunk

            response = "".join(c for c in collected if isinstance(c, str))

            # Check for final answer
            if "Final Answer:" in response:
                break

            # Parse action
            if "Action:" in response and "Action Input:" in response:
                # Tool call will be handled by orchestrator
                # Add observation placeholder
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": "Observation: [tool will execute]"})
            else:
                # Just reasoning, continue
                messages.append({"role": "assistant", "content": response})


class SelfConsistency:
    """Self-Consistency: sample multiple times, majority vote."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, samples: int = 5):
        self.router = router
        self.client = client
        self.samples = samples

    async def reason(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "reasoning",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        # Run multiple samples in parallel
        tasks = [self._sample(messages, tools, task_type) for _ in range(self.samples)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter valid
        valid = [r for r in results if isinstance(r, str) and len(r) > 10]

        if not valid:
            # Fallback to single
            async for item in self.router.chat(messages, tools, task_type):
                yield item
            return

        # Majority vote on final answer (simple: most common)
        answer = self._majority_vote(valid)

        # Stream the answer
        for chunk in self._chunk(answer):
            yield "self_consistency", chunk

    async def _sample(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        task_type: str,
    ) -> str:
        chunks = []
        async for _, chunk in self.router.chat(messages, tools, task_type):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    def _majority_vote(self, answers: list[str]) -> str:
        # Simple: return longest (most complete)
        return max(answers, key=len)

    def _chunk(self, text: str, size: int = 50) -> list[str]:
        return [text[i : i + size] for i in range(0, len(text), size)]


class Reflection:
    """Reflection: generate, critique, refine loop."""

    CRITIQUE_PROMPT = """Critique your previous response for:
- Factual accuracy
- Completeness
- Logical consistency
- Clarity

Issues found: [list] or 'NONE' if perfect."""

    REFINE_PROMPT = """Improve your previous response based on this critique:
{critique}

Provide the complete improved response."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient, max_iterations: int = 3):
        self.router = router
        self.client = client
        self.max_iterations = max_iterations

    async def refine(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "reasoning",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        current_messages = messages

        for iteration in range(self.max_iterations):
            # Generate
            collected = []
            async for name, chunk in self.router.chat(current_messages, tools, task_type):
                collected.append(chunk)
                yield name, chunk

            response = "".join(c for c in collected if isinstance(c, str))

            # Critique
            critique_messages = current_messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": self.CRITIQUE_PROMPT},
            ]

            critique_chunks = []
            async for _, chunk in self.router.chat(critique_messages, None, "reasoning"):
                if isinstance(chunk, str):
                    critique_chunks.append(chunk)

            critique = "".join(critique_chunks)

            if "NONE" in critique.upper() or iteration == self.max_iterations - 1:
                break

            # Refine
            refine_messages = current_messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": self.REFINE_PROMPT.format(critique=critique)},
            ]

            current_messages = refine_messages


class ReasoningEngine:
    """Unified reasoning engine combining all strategies."""

    def __init__(self, router: FailoverRouter, client: OpenAICompatClient):
        self.router = router
        self.client = client
        self.cot = ChainOfThought(router, client)
        self.tot = TreeOfThoughts(router, client)
        self.react = ReActAgent(router, client)
        self.consistency = SelfConsistency(router, client)
        self.reflection = Reflection(router, client)

    async def reason(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "reasoning",
        strategy: str = "auto",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        """Run reasoning with specified strategy."""

        if strategy == "cot" or (strategy == "auto" and task_type == "reasoning"):
            async for item in self.cot.reason(messages, tools, task_type):
                yield item
        elif strategy == "tot":
            async for item in self.tot.reason(messages, tools, task_type):
                yield item
        elif strategy == "react":
            question = messages[-1].get("content", "")
            async for item in self.react.run(question, tools or [], task_type):
                yield item
        elif strategy == "consistency":
            async for item in self.consistency.reason(messages, tools, task_type):
                yield item
        elif strategy == "reflection":
            async for item in self.reflection.refine(messages, tools, task_type):
                yield item
        else:
            # Standard
            async for item in self.router.chat(messages, tools, task_type):
                yield item

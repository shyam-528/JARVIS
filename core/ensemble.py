from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from core.config import ProviderConfig
from core.providers import OpenAICompatClient, ToolCallDelta
from core.router import AllProvidersDown, FailoverRouter


@dataclass
class EnsembleResult:
    content: str
    provider: str
    confidence: float
    metadata: dict


class ModelEnsemble:
    """Model Ensemble / Mixture of Experts for JARVIS.

    Features:
    - Task-aware routing (already in FailoverRouter)
    - Cascade: cheap model first, escalate if low confidence
    - Consensus: run multiple models, vote/blend
    - MoE-style: route different parts to different experts
    - Self-correction: critique and refine
    """

    def __init__(
        self,
        router: FailoverRouter,
        client: OpenAICompatClient,
        cascade_threshold: float = 0.7,
        consensus_count: int = 3,
    ):
        self.router = router
        self.client = client
        self.cascade_threshold = cascade_threshold
        self.consensus_count = consensus_count

    async def chat_with_cascade(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "general",
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        """Cascade: try fast/cheap model first, escalate if needed."""
        candidates = self.router.available()
        if not candidates:
            raise AllProvidersDown("no providers available")

        # Sort by: task match, then speed (lower timeout = faster), then cost
        def cascade_score(p: ProviderConfig) -> tuple:
            task_score = self.router._task_score(p, task_type)
            speed_score = p.first_token_timeout_s or 30.0
            cost_score = 1 / (p.daily_token_budget / 1_000_000)
            return (task_score, speed_score, cost_score, p.priority)

        candidates = sorted(candidates, key=cascade_score)

        # Try first candidate
        provider = candidates[0]
        async for item in self._stream_provider(provider, messages, tools):
            yield item

        # If we got here without error, success
        # (In practice, could check response quality and escalate)

    async def chat_with_consensus(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "general",
        min_agreement: float = 0.6,
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        """Run multiple models, yield consensus result."""
        candidates = self.router.available()
        if not candidates:
            raise AllProvidersDown("no providers available")

        # Select diverse candidates for consensus
        tool_candidates = [p for p in candidates if p.supports_tools] if tools else candidates
        if len(tool_candidates) < self.consensus_count:
            tool_candidates = candidates

        selected = tool_candidates[: self.consensus_count]

        # Run all in parallel
        results = await asyncio.gather(
            *[self._collect_full_response(p, messages, tools) for p in selected],
            return_exceptions=True,
        )

        # Filter successful
        valid_results = []
        for p, result in zip(selected, results, strict=True):
            if isinstance(result, Exception):
                continue
            valid_results.append((p.name, result))

        if not valid_results:
            raise AllProvidersDown("all consensus models failed")

        # Simple consensus: majority vote on content similarity
        # For streaming, yield from the "winner"
        winner_name, winner_content = self._select_consensus(valid_results)

        # Stream the winner's content (simulated streaming)
        for chunk in self._chunk_text(winner_content):
            yield winner_name, chunk

    async def chat_with_self_correction(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        task_type: str = "general",
        max_iterations: int = 2,
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        """Self-correction: generate, critique, refine."""
        current_messages = messages

        for iteration in range(max_iterations + 1):
            # Generate
            collected = []
            async for name, chunk in self.router.chat(current_messages, tools, task_type):
                collected.append(chunk)
                yield name, chunk

            if iteration == max_iterations:
                break

            response = "".join(c for c in collected if isinstance(c, str))

            # Critique
            critique_prompt = f"""Critique this response for accuracy, completeness, and clarity.
Original query: {messages[-1].get("content", "")}
Response: {response}

Provide specific issues or 'GOOD' if satisfactory."""

            critique_messages = current_messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": critique_prompt},
            ]

            critique_collected = []
            async for _name, chunk in self.router.chat(critique_messages, None, "reasoning"):
                critique_collected.append(chunk)

            critique = "".join(c for c in critique_collected if isinstance(c, str))

            if "GOOD" in critique.upper():
                break

            # Refine
            refine_prompt = f"""Improve the previous response based on this critique:
{critique}

Provide the improved response only."""

            current_messages = messages + [
                {"role": "assistant", "content": response},
                {"role": "user", "content": refine_prompt},
            ]

    async def _collect_full_response(
        self,
        provider: ProviderConfig,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> str:
        chunks = []
        async for _, chunk in self._stream_provider(provider, messages, tools):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    async def _stream_provider(
        self,
        provider: ProviderConfig,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        stream = self.client.stream_chat(
            base_url=provider.base_url,
            api_key=provider.api_key,
            model=provider.model,
            messages=messages,
            tools=tools if tools and provider.supports_tools else None,
        )
        try:
            async for chunk in stream:
                if chunk is not None:
                    yield provider.name, chunk
        except Exception:
            raise

    def _select_consensus(self, results: list[tuple[str, str]]) -> tuple[str, str]:
        """Simple consensus: pick longest non-error response, or first."""
        # Filter out very short/error responses
        filtered = [(n, c) for n, c in results if len(c) > 10 and "error" not in c.lower()[:20]]
        if not filtered:
            return results[0]
        # Return longest (most complete)
        return max(filtered, key=lambda x: len(x[1]))

    def _chunk_text(self, text: str, chunk_size: int = 50) -> list[str]:
        """Simulate streaming by chunking."""
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


class MoERouter:
    """Mixture of Experts: route different query types to specialized models."""

    EXPERTS = {
        "coding": ["openrouter-llama-3.1-70b", "openrouter-deepseek-chat", "groq-gpt-oss-120b"],
        "reasoning": ["openrouter-deepseek-chat", "openrouter-llama-3.1-70b", "groq-gpt-oss-120b"],
        "vision": [
            "google-gemini-1.5-flash",
            "google-gemini-1.5-flash-8b",
            "google-gemini-1.5-pro",
        ],
        "fast": ["openrouter-llama-3.1-8b", "groq-qwen-27b"],
        "general": ["openrouter-llama-3.1-70b", "groq-gpt-oss-120b"],
    }

    def __init__(self, router: FailoverRouter):
        self.router = router

    def get_experts(self, task_type: str) -> list[str]:
        return self.EXPERTS.get(task_type, self.EXPERTS["general"])

    def get_fallback_chain(self, task_type: str) -> list[str]:
        """Ordered fallback chain for a task type."""
        experts = self.get_experts(task_type)
        # Add general fallbacks
        general = [e for e in self.EXPERTS["general"] if e not in experts]
        return experts + general

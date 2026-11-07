from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .providers import OpenAICompatClient
from .router import FailoverRouter


@dataclass
class SummaryConfig:
    enabled: bool = True
    model: str = "gemini-3.5-flash"
    max_summary_tokens: int = 2000
    trigger_ratio: float = 0.8
    min_turns_to_summarize: int = 10


class Summarizer:
    """LLM-based context summarization to preserve semantics during trimming."""

    SUMMARY_PROMPT = """Summarize the following conversation segment. Preserve ALL:
- Facts, preferences, and personal details about the user
- Decisions made and commitments agreed to
- Code snippets, technical details, and file paths
- Names, projects, and specific references
- Action items and follow-ups

Write as a concise narrative that could replace the original messages without losing meaning.
Do not add commentary. Do not use bullet points unless the original used them.

Conversation:
{conversation}

Summary:"""

    def __init__(
        self,
        router: FailoverRouter,
        client: OpenAICompatClient,
        config: SummaryConfig = None,
    ):
        self.router = router
        self.client = client
        self.config = config or SummaryConfig()
        self._summarization_lock = asyncio.Lock()
        # Rolling fold: the cached summary is re-folded forward each time;
        # only the new tail beyond the previous summary goes to the LLM.
        self._cached_summary: str = ""

    def should_summarize(self, history: list[dict], budget_tokens: int) -> bool:
        if not self.config.enabled:
            return False
        if len(history) < self.config.min_turns_to_summarize:
            return False
        # Estimate current usage
        total_chars = sum(len(str(m.get("content", ""))) for m in history)
        estimated_tokens = total_chars // 4
        if estimated_tokens > budget_tokens * self.config.trigger_ratio:
            return True
        # Message-count trigger: long conversations should compress even
        # below the token ceiling, so prompt size stays bounded.
        return len(history) >= self.config.min_turns_to_summarize * 2

    def invalidate_cache(self) -> None:
        """Forget the cached rolling summary (history was reset/replaced)."""
        self._cached_summary = ""

    async def summarize_segment(
        self,
        messages: list[dict],
        task_type: str = "general",
    ) -> str | None:
        """Summarize a list of messages into a single summary string."""
        if not messages:
            return None

        # Format conversation for summarization
        conv_lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                conv_lines.append(f"{role}: {content}")

        conversation = "\n\n".join(conv_lines)
        if not conversation.strip():
            return None

        prompt = self.SUMMARY_PROMPT.format(conversation=conversation)
        summary_messages = [{"role": "user", "content": prompt}]

        # Use the router with the summary model preference
        # We need to temporarily override provider selection for summarization
        collected = []
        try:
            async for _name, chunk in self.router.chat(
                summary_messages,
                tools=None,
                task_type="fast",  # Use fast model for summarization
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
        except Exception as e:
            print(f"[Summarizer] Summarization failed: {e}")
            return None

        summary = "".join(collected).strip()
        if not summary or len(summary) < 20:
            return None

        # Truncate if too long
        if len(summary) > self.config.max_summary_tokens * 4:
            summary = summary[: self.config.max_summary_tokens * 4] + "..."

        return summary

    async def create_rolling_summary(
        self,
        orchestrator=None,
        messages_to_summarize: list[dict] | None = None,
    ) -> str | None:
        """Create a summary and format it as a system message.

        Callers may pass (orchestrator, messages) or just (messages);
        the orchestrator arg is accepted for call-site compatibility.

        Rolling fold: after each summarization the caller keeps the summary
        message in history, so the slice passed next turn starts with our
        own summary. Everything after it is new — only that tail goes to
        the LLM, then gets folded into the cached summary.
        """
        if isinstance(orchestrator, list) and messages_to_summarize is None:
            # Called as create_rolling_summary(messages)
            messages_to_summarize = orchestrator
            orchestrator = None
        messages = messages_to_summarize or []
        async with self._summarization_lock:
            new_messages = messages
            if self._cached_summary:
                # Find our previous summary message in the slice; the raw
                # messages after it are the only new content.
                boundary = -1
                for i, m in enumerate(messages):
                    content = str(m.get("content", ""))
                    if content.startswith("[Previous Conversation Summary]"):
                        boundary = i
                        break
                new_messages = messages[boundary + 1 :] if boundary >= 0 else messages

            if not new_messages:
                if self._cached_summary:
                    return f"[Previous Conversation Summary]\n{self._cached_summary}\n[End Summary]"
                return None

            if self._cached_summary:
                prior = {"role": "system", "content": self._cached_summary}
                summary = await self.summarize_segment([prior] + new_messages)
            else:
                summary = await self.summarize_segment(new_messages)

            if summary:
                self._cached_summary = summary
                return f"[Previous Conversation Summary]\n{summary}\n[End Summary]"
            # LLM failure: fall back to the stale cached summary rather
            # than nothing — the conversation stays usable.
            if self._cached_summary:
                return f"[Previous Conversation Summary]\n{self._cached_summary}\n[End Summary]"
            return None


def estimate_tokens(text: str) -> int:
    """Rough token estimation."""
    return max(1, (len(text) + 3) // 4)

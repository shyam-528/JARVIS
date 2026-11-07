"""Self-improvement tools: stats, learned lessons, and triggering an
improvement run. Writes are guarded by the self-improve pipeline's review.
"""

from __future__ import annotations

from core.tools import Tool, ToolBox


def register_self_improve(box: ToolBox, orchestrator):
    """P12: expose the closed-loop self-improvement engine to the brain."""

    async def self_improve_stats() -> str:
        """Self-improvement statistics: cycles run, avg score gain, lessons."""
        stats = orchestrator.self_improve.get_stats()
        lessons = orchestrator.self_improve.top_lessons(limit=5, min_occurrences=1)
        lines = [
            f"Improvement cycles: {stats['total_cycles']}",
            f"Avg score improvement: {stats['avg_improvement']:+.1f}",
            f"Avg iterations: {stats['avg_iterations']:.1f}",
            f"Synthetic examples: {stats['synthetic_examples']}",
            f"Lessons learned: {stats['lessons_learned']}",
        ]
        if lessons:
            lines.append("\nTop recurring lessons:")
            for i, lesson in enumerate(lessons, 1):
                lines.append(f"  {i}. {lesson}")
        return "\n".join(lines)

    async def self_improve_lessons(min_occurrences: int = 2) -> str:
        """Show recurring critique lessons the assistant has learned about its own output."""
        lessons = orchestrator.self_improve.top_lessons(limit=10, min_occurrences=min_occurrences)
        if not lessons:
            return (
                f"No lessons seen {min_occurrences}+ times yet. "
                "Cycles accumulate as conversations trigger self-improvement."
            )
        return f"Lessons (seen {min_occurrences}+ times):\n" + "\n".join(
            f"- {lesson}" for lesson in lessons
        )

    async def self_improve_now(query: str) -> str:
        """Manually trigger a self-improvement cycle: critique + refine a
        previous response. Pass the user's original question."""
        # Find the latest assistant answer in history for context
        last_assistant = ""
        for m in reversed(orchestrator.history):
            if m.get("role") == "assistant" and m.get("content"):
                last_assistant = m["content"]
                break
        if not last_assistant:
            return "No previous response to improve yet."
        try:
            cycle = await orchestrator.self_improve.improve(
                query or "previous response", last_assistant, task_type="reasoning"
            )
            orchestrator._apply_lessons_to_prompt()
            return (
                f"Cycle {cycle.id}: {cycle.iterations} iterations, "
                f"score {cycle.score_improvement:+.1f}.\n\nImproved response:\n"
                f"{cycle.refined_output[:1500]}"
            )
        except Exception as e:
            return f"Self-improvement failed: {e}"

    box.register(
        Tool(
            name="self_improve_stats",
            description="Show self-improvement statistics and top recurring lessons learned by the assistant about its own output quality",
            parameters={"type": "object", "properties": {}},
            handler=self_improve_stats,
        )
    )
    box.register(
        Tool(
            name="self_improve_lessons",
            description="List recurring critique lessons the assistant has learned (issues it keeps getting flagged for)",
            parameters={
                "type": "object",
                "properties": {
                    "min_occurrences": {
                        "type": "integer",
                        "default": 2,
                        "description": "Only lessons flagged at least this many times",
                    },
                },
            },
            handler=self_improve_lessons,
        )
    )
    box.register(
        Tool(
            name="self_improve_now",
            description="Trigger a self-improvement cycle on the most recent response: critiques it, refines it, and absorbs lessons",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The original question the last response answered",
                    },
                },
                "required": ["query"],
            },
            handler=self_improve_now,
        )
    )

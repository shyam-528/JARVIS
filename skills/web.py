"""Web search tool."""

from __future__ import annotations

import asyncio

from core.tools import Tool, ToolBox


async def web_search(query: str, max_results: int = 5) -> str:
    try:
        from ddgs import DDGS
    except ImportError:
        return "error: ddgs not installed (pip install ddgs)"

    def run() -> list[dict]:
        with DDGS() as d:
            return list(d.text(query, max_results=max_results))

    rows = await asyncio.to_thread(run)
    if not rows:
        return "no results"

    lines = []
    for r in rows:
        title = r.get("title") or r.get("href") or r.get("url") or ""
        href = r.get("href") or r.get("url") or r.get("link") or ""
        body = r.get("body") or r.get("description") or r.get("snippet") or ""
        lines.append(f"- {title} — {href}\n  {body[:200]}")
    return "\n".join(lines)


def register_web(box: ToolBox) -> None:
    box.register(
        Tool(
            name="web_search",
            description="Search the web via DuckDuckGo and return top results with snippets",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
            handler=web_search,
        )
    )

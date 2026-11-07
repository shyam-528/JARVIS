"""Web-watch: scheduled page-change monitor (price/doc/policy tracking).

Uses httpx (already a dependency) + sha256. State in data/webwatch.json.
Scheduler fodder: schedule_task(name, interval, {seconds}, web_check, {url}).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from core.tools import Tool, ToolBox

_STATE = Path("data/webwatch.json")
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.DOTALL)


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _fingerprint(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = " ".join(text.split())
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16], text


async def _fetch(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": "JARVIS-MARK3-watch/1.0"}
    ) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


def register_webwatch(box: ToolBox):
    async def web_watch(url: str) -> str:
        """Check a page for changes since last check. First call baselines."""
        try:
            html = await _fetch(url)
        except Exception as exc:
            return f"error fetching {url}: {exc}"
        digest, text = _fingerprint(html)
        state = _load()
        prev = state.get(url)
        state[url] = {"digest": digest, "chars": len(text)}
        _save(state)
        if prev is None:
            return f"watching {url} (baseline {digest}, {len(text)} chars). Next check diffs."
        if prev.get("digest") == digest:
            return f"unchanged {url} ({digest})."
        return (
            f"CHANGED {url}: {prev.get('digest')} -> {digest} "
            f"({prev.get('chars', 0)} -> {len(text)} chars)."
        )

    async def web_unwatch(url: str = "") -> str:
        state = _load()
        if not state:
            return "nothing watched."
        if url:
            if state.pop(url, None) is None:
                return f"not watching {url}"
            _save(state)
            return f"unwatched {url}"
        lines = [f"- {u} ({v.get('digest')})" for u, v in state.items()]
        return "watched pages:\n" + "\n".join(lines)

    box.register(
        Tool(
            name="web_watch",
            description="Check a URL for content changes since last check (price/docs monitoring). First call baselines.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=web_watch,
        )
    )
    box.register(
        Tool(
            name="web_unwatch",
            description="List watched pages or stop watching one.",
            parameters={"type": "object", "properties": {"url": {"type": "string", "default": ""}}},
            handler=web_unwatch,
        )
    )

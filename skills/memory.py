"""Memory tools: remember/recall/forget, conversation search, consolidation,
preferences (prefs_*), anniversary lookup, and project memory.
"""

from __future__ import annotations

from core.memory import MemoryStore
from core.tools import Tool, ToolBox


def register_memory(box: ToolBox, store: MemoryStore) -> None:
    async def remember(text: str, kind: str = "fact") -> str:
        return store.add(text, kind)

    async def recall(query: str) -> str:
        rows = store.search(query, k=5)
        if not rows:
            return "no memories found for that query"
        return "\n".join(f"- {text} ({ts})" for _, ts, text, _ in rows)

    async def forget(query: str) -> str:
        n = store.forget(query)
        return (
            f"forgot {n} memory(ies) matching '{query}'" if n else f"no memories matched '{query}'"
        )

    async def search_conversations(query: str, limit: int = 5) -> str:
        """P6 episodic memory: search ALL past conversation sessions."""
        from core.tools import _get_persistence

        store = _get_persistence()
        if store is None:
            return "conversation history not available"
        results = store.search_sessions(query, limit=limit)
        if not results:
            return f"no past conversations matched '{query}'"
        lines = [f"Found {len(results)} matching conversation(s):"]
        for r in results:
            lines.append(f"- [{r['session_id']}] {r['title']}: {r['snippet'][:150]}")
        return "\n".join(lines)

    box.register(
        Tool(
            name="remember",
            description="Persist a durable fact about the user across sessions (preferences, projects, names, schedule). Keep it short and self-contained.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "default": "fact"},
                },
                "required": ["text"],
            },
            handler=remember,
        )
    )
    box.register(
        Tool(
            name="recall",
            description="Search long-term memory for facts about the user",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=recall,
        )
    )
    box.register(
        Tool(
            name="forget",
            description="Delete stored memories matching a query",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=forget,
        )
    )
    box.register(
        Tool(
            name="search_conversations",
            description="Search ALL past conversation sessions (episodic memory) — find what was discussed or decided previously: 'search_conversations query=api design'",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
            },
            handler=search_conversations,
        )
    )


def register_consolidation(box: ToolBox, store: MemoryStore, preferences, memory_v2=None) -> None:
    async def memory_consolidate() -> str:
        from core.consolidation import consolidate, format_stats

        try:
            out = format_stats(consolidate(store, preferences))
        except Exception as exc:
            return f"consolidation failed: {exc}"
        if memory_v2 is not None:
            try:
                epi = memory_v2.conn.execute("SELECT COUNT(*) FROM episodic").fetchone()[0]
                sem = memory_v2.conn.execute("SELECT COUNT(*) FROM semantic").fetchone()[0]
                out += f"\nMemoryV2: {epi} episodic, {sem} semantic (hourly decay/promote/prune active)."
            except Exception:
                pass
        return out

    box.register(
        Tool(
            name="memory_consolidate",
            description="Weekly memory hygiene: promote durable facts to preferences, list stale trivia for review. Never deletes.",
            parameters={"type": "object", "properties": {}},
            handler=memory_consolidate,
        )
    )


def register_prefs(box: ToolBox, preferences) -> None:
    """Visible/editable learned preferences (chat + Settings UI + API)."""

    async def prefs_list() -> str:
        try:
            all_prefs = preferences.list_all()
        except Exception:
            return "preferences unavailable"
        if not all_prefs:
            return "No learned preferences yet — I pick them up as we talk."
        lines = ["Learned preferences:"]
        for key in sorted(all_prefs):
            p = all_prefs[key]
            lines.append(f"- {key} = {p.value} [{p.category}, {p.source}, conf {p.confidence:.2f}]")
        return "\n".join(lines)[:2000]

    async def prefs_set(key: str, value: str, category: str = "general") -> str:
        try:
            ok = preferences.set_value(key, value, category or "general")
        except Exception:
            ok = False
        return f"preference '{key}' set." if ok else "error: key and value required"

    async def prefs_forget(key: str) -> str:
        try:
            ok = preferences.remove(key)
        except Exception:
            ok = False
        return f"forgot preference '{key}'." if ok else f"no preference '{key}'"

    box.register(
        Tool(
            name="prefs_list",
            description="List learned user preferences (visible, editable).",
            parameters={"type": "object", "properties": {}},
            handler=prefs_list,
        )
    )
    box.register(
        Tool(
            name="prefs_set",
            description="Set a user preference explicitly: key, value, optional category.",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "category": {"type": "string", "default": "general"},
                },
                "required": ["key", "value"],
            },
            handler=prefs_set,
        )
    )
    box.register(
        Tool(
            name="prefs_forget",
            description="Delete one learned preference by key.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=prefs_forget,
        )
    )


def register_anniversary(box: ToolBox, memory, persistence) -> None:
    """'A year ago today' over durable facts + past sessions."""

    async def memory_anniversary() -> str:
        try:
            from core.anniversary import anniversary_text

            text = anniversary_text(memory, persistence)
        except Exception:
            text = ""
        return text or "Nothing on this date in prior years — a quiet past, sir."

    box.register(
        Tool(
            name="memory_anniversary",
            description="What happened on this date in prior years: durable facts + past conversations ('a year ago today').",
            parameters={"type": "object", "properties": {}},
            handler=memory_anniversary,
        )
    )


def register_project_memory(box: ToolBox, data_dir) -> None:
    """Per-project conventions, open threads, standups, notes."""

    def _pm():
        from core.project_memory import ProjectMemory

        return ProjectMemory(data_dir)

    async def project_remember(project: str, kind: str = "note", text: str = "") -> str:
        res = _pm().remember(project, kind, text)
        if res.get("ok"):
            return f"noted for {project} [{kind}]."
        return f"error: {res.get('error', 'could not store')}"

    async def project_recall(project: str) -> str:
        card = _pm().recall(project)
        return card or f"no memory for '{project}' yet — teach me with project_remember."

    async def project_list() -> str:
        items = _pm().list()
        if not items:
            return "No projects remembered yet."
        lines = ["Projects I remember:"]
        for it in items[:15]:
            lines.append(
                f"- {it['name']} ({it['threads']} open threads, updated {it['updated'] or '?'})"
            )
        return "\n".join(lines)

    box.register(
        Tool(
            name="project_remember",
            description="Remember something about a project: project name/path, kind (convention|thread|standup|note), text.",
            parameters={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["convention", "thread", "standup", "note"],
                        "default": "note",
                    },
                    "text": {"type": "string"},
                },
                "required": ["project", "text"],
            },
            handler=project_remember,
        )
    )
    box.register(
        Tool(
            name="project_recall",
            description="Recall everything remembered about a project (conventions, open threads, last standup).",
            parameters={
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
            handler=project_recall,
        )
    )
    box.register(
        Tool(
            name="project_list",
            description="List all remembered projects.",
            parameters={"type": "object", "properties": {}},
            handler=project_list,
        )
    )

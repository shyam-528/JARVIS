"""Tool registry and tool packs.

Contract:
- ToolBox maps tool name -> Tool (name, description, JSON-schema parameters,
  async handler). build_toolbox() wires every skills.register_* into one box.
- execute() NEVER raises: unknown tool, bad args JSON, wrong args, and handler
  exceptions all come back as "error: ..." strings so the model can see and
  recover from failures. Callers (tool_use_v2, chains) detect failure via the
  "error:" prefix.
- Tool packs: CORE_TOOLS are always sent to the model; domain packs
  (PACK_PREFIXES) load on demand via the load_pack tool to stay under
  provider tool-count limits (Groq caps at 128).
- Optional subsystems (memory, scheduler, mesh, ...) are registered only when
  provided; a few legacy register_* calls are wrapped in try/except so one
  broken optional pack can't kill the whole toolbox (intentional fail-soft).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Awaitable[str]]


class ToolBox:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    async def execute(self, name: str, args_json: str) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"error: unknown tool '{name}'"
        try:
            args = json.loads(args_json) if args_json.strip() else {}
        except json.JSONDecodeError as e:
            return f"error: bad arguments JSON ({e})"
        try:
            return await tool.handler(**args)
        except TypeError as e:
            return f"error: wrong arguments ({e})"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    def schemas_for(self, names: list[str]) -> list[dict]:
        """Schemas for the given tool names, in order, skipping unknown."""
        out = []
        for name in names:
            t = self._tools.get(name)
            if t is None:
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
            )
        return out


# ---- Tool packs: core always sent, domain packs loaded on demand ----
# Packs match by tool-name prefix. CORE_TOOLS are explicit names.
CORE_TOOLS = [
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "list_dir",
    "search_code",
    "run_command",
    "run_tests",
    "git_status",
    "git_diff",
    "git_log",
    "get_diagnostics",
    "web_search",
    "remember",
    "recall",
    "schedule_task",
    # Reminder management must be core: schedule_task can create reminders,
    # so the model needs to see how to list/remove them without first
    # guessing that a "scheduler" pack exists (it has no auto-load keywords).
    "list_scheduled_tasks",
    "remove_scheduled_task",
    "enable_scheduled_task",
    "notify",
    "notify_reminder",
    "daily_briefing",
    "system_info",
    "open_app",
    "screenshot",
    "search_knowledge",
    "code_exec_python",
    "code_exec_shell",
    "dev_init",
    "dev_review",
    "dev_test",
    "dev_standup",
    "inbox_report",
    "load_pack",
]

PACK_PREFIXES = {
    "mail": ["mail_"],
    "github": ["dev_pr"],
    "browser": ["browser_"],
    "vision": [
        "analyze_image",
        "ocr",
        "read_screen",
        "clipboard_image",
        "list_monitors",
        "screen_capture",
    ],
    "computer": [
        "mouse_",
        "keyboard_",
        "find_on_screen",
        "find_text_on_screen",
        "focus_window",
        "list_windows",
        "move_window",
        "get_mouse_position",
        "wait_for_image",
        "record_mouse",
        "sys_",
        "demo_prep",
        "downloads_organize",
        "screenshot_note",
    ],
    "knowledge": ["knowledge_", "index_path", "clear_knowledge", "search_conversations"],
    "workflow": [
        "create_workflow",
        "run_workflow",
        "list_workflows",
        "delete_workflow",
        "toggle_workflow",
        "workflow_runs",
        "run_detail",
        "create_tool_chain",
        "execute_tool_chain",
        "list_tool_chains",
    ],
    "scheduler": ["list_scheduled_tasks", "remove_scheduled_task", "enable_scheduled_task"],
    "ml": ["ml_"],
    "self": ["self_improve_"],
    "plugin": ["plugin_"],
    "a2a": ["federated_query", "list_remote_models", "get_distributed_stats", "discover_tools"],
    "media": ["text_to_speech", "transcribe_audio"],
    "codeintel": [
        "find_references",
        "find_symbol",
        "format_code",
        "lint_file",
        "typecheck_file",
        "refactor_rename",
    ],
    "codeexec": [
        "code_exec_js",
        "code_exec_python_file",
        "code_exec_install_package",
        "code_exec_session_",
    ],
    "prompts": [
        "ab_test_prompts",
        "optimize_prompt",
        "add_example",
        "get_examples",
        "bootstrap_examples",
        "register_prompt",
        "list_prompts",
        "get_prompt_stats",
    ],
    "triage": ["inbox_sort"],
    "web": ["web_watch", "web_unwatch"],
    "wa": ["wa_"],
    "voice": ["voice_doctor"],
    "notifyx": ["notify_alert", "notify_photo", "notify_test", "notify_briefing"],
    "routine": ["morning_start", "evening_wrap"],
    "memory": [
        "forget",
        "search_conversations",
        "memory_consolidate",
        "memory_anniversary",
        "prefs_",
        "project_",
    ],
}

PACK_KEYWORDS = {
    "mail": ["mail", "email", "inbox", "gmail"],
    "github": ["pull request", "github", "/pr ", " pr ", "issue #", "repo"],
    "browser": ["browse", "website", "webpage", "web page", "url", "link"],
    "vision": ["image", "picture", "photo", "ocr", "analyze screen", "what.*screen"],
    "computer": [
        "window",
        "mouse",
        "keyboard",
        "keystroke",
        "hotkey",
        "demo prep",
        "organize downloads",
        "screenshot",
        "note my screen",
        "open app",
        "launch app",
    ],
    "knowledge": ["pdf", "document", "index", "knowledge base", "codebase"],
    "memory": [
        "forget",
        "delete memory",
        "clean memory",
        "consolidate",
        "remember when",
        "a year ago",
        "preferences",
        "my preferences",
        "project",
    ],
    "workflow": ["workflow", "automate", "every day", "recurring"],
    "ml": ["anomaly", "train model", "ml "],
    "plugin": ["plugin", "skill"],
    "a2a": ["mesh", "peer", "distributed", "remote model"],
    "media": ["speak", "say it", "read aloud", "transcribe", "audio"],
    "codeintel": ["symbol", "references", "lint", "typecheck", "rename symbol"],
    "prompts": ["optimize prompt", "prompt template"],
    "triage": ["downloads", "triage", "organize files"],
    "web": ["price drop", "monitor page", "track page", "watch url", "changed"],
    "wa": ["whatsapp", "wa chats"],
    "voice": ["microphone", "mic test", "voice check", "audio"],
    "notifyx": ["alert", "notify me", "remind"],
    # Belt-and-braces: if these tools ever leave CORE_TOOLS, reminder
    # phrasing still auto-loads the scheduler pack.
    "scheduler": ["reminder", "remind me", "scheduled task", "cancel reminder", "delete reminder"],
    "routine": [
        "starting work",
        "start my day",
        "morning routine",
        "winding down",
        "wind down",
        "evening routine",
        "end my day",
        "wrap up",
    ],
}


def pack_tools(box: ToolBox, pack: str) -> list[str]:
    """Tool names in a pack that actually exist in this toolbox."""
    prefixes = PACK_PREFIXES.get(pack, [])
    names = []
    for name in box.names():
        if any(name == p or name.startswith(p) for p in prefixes):
            names.append(name)
    return sorted(names)


def detect_packs(text: str) -> list[str]:
    """Keyword scan: which domain packs does this message need?"""
    import re as _re

    low = (text or "").lower()
    found = []
    for pack, keywords in PACK_KEYWORDS.items():
        for kw in keywords:
            if kw.startswith("what.*"):
                if _re.search(kw, low):
                    found.append(pack)
                    break
            elif kw in low:
                found.append(pack)
                break
    return found


# Global accessors so skills can reach orchestrator-owned stores without
# circular imports (set by the Orchestrator after construction).
_PERSISTENCE = None


def set_persistence(store) -> None:
    global _PERSISTENCE
    _PERSISTENCE = store


def _get_persistence():
    return _PERSISTENCE


def default_toolbox(
    memory=None,
    scheduler=None,
    proactive=None,
    automation=None,
    registry=None,
    notify_manager=None,
    mesh=None,
    ml_engine=None,
    knowledge_store=None,
    knowledge_indexer=None,
    code_agent=None,
    workflow_engine=None,
    rag=None,
) -> ToolBox:
    from skills.a2a_skills import register_a2a
    from skills.briefing import register_briefing
    from skills.browser import register_browser
    from skills.code_agent_skills import register_code_agent
    from skills.code_exec import register_code_exec
    from skills.code_intel import register_code_intel
    from skills.computer_use import register_computer_use
    from skills.dev_loop import register_dev_loop
    from skills.fs import register_fs
    from skills.knowledge_skills import register_knowledge
    from skills.mail_skills import register_mail
    from skills.memory import register_memory
    from skills.ml_proactive_skills import register_ml_proactive
    from skills.notify_skills import register_notify
    from skills.pc import register_pc
    from skills.plugin_marketplace import register_marketplace
    from skills.scheduler_skills import register_scheduler
    from skills.system import register_system
    from skills.triage import register_triage
    from skills.vision_skills import register_vision
    from skills.web import register_web
    from skills.webwatch import register_webwatch
    from skills.workflow_skills import register_workflow

    box = ToolBox()
    register_pc(box)
    register_web(box)
    register_computer_use(box)
    register_fs(box)
    register_system(box)
    register_code_exec(box)
    register_code_intel(box)
    register_browser(box)
    if memory is not None:
        register_memory(box, memory)
    if scheduler is not None:
        register_scheduler(box, scheduler)
    if memory is not None:
        from skills.briefing import register_briefing

        register_briefing(box)
    register_vision(box)
    if registry is not None:
        register_marketplace(box, registry)
    if notify_manager is not None:
        register_notify(box, notify_manager)
    if mesh is not None:
        register_a2a(box, mesh)
    if ml_engine is not None:
        register_ml_proactive(box, ml_engine)
    if knowledge_store is not None and knowledge_indexer is not None:
        register_knowledge(box, knowledge_store, knowledge_indexer, rag=rag)
    if code_agent is not None:
        register_code_agent(box, code_agent)
    if workflow_engine is not None:
        register_workflow(box, workflow_engine)
    with contextlib.suppress(Exception):
        register_dev_loop(box, knowledge_indexer=knowledge_indexer, scheduler=scheduler)
    with contextlib.suppress(Exception):
        register_triage(box)
    with contextlib.suppress(Exception):
        register_mail(box)
    with contextlib.suppress(Exception):
        register_webwatch(box)
    box.register(
        Tool(
            name="load_pack",
            description=(
                "Load a domain tool-pack for this turn (use when the task needs "
                "tools you don't have). pack: mail|github|browser|vision|computer|"
                "knowledge|memory|workflow|scheduler|ml|self|plugin|a2a|media|codeintel|"
                "codeexec|prompts|triage|web|wa|voice|notifyx. Returns the newly available tools."
            ),
            parameters={
                "type": "object",
                "properties": {"pack": {"type": "string"}},
                "required": ["pack"],
            },
            handler=_load_pack_handler(box),
        )
    )
    return box


def _load_pack_handler(box: ToolBox):
    async def load_pack(pack: str = "") -> str:
        names = pack_tools(box, (pack or "").strip().lower())
        if not names:
            return f"unknown pack '{pack}'. Available: {', '.join(sorted(PACK_PREFIXES))}"
        return f"pack '{pack}' loaded: {', '.join(names)}. Use them now."

    return load_pack

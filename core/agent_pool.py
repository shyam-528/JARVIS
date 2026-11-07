"""Specialist agent pool: named agents with dedicated provider lists and tools.

Contract:
- DEFAULT_AGENTS pins each specialist (coder, researcher, ...) to an ordered
  provider list; `providers=[]` means the full failover chain. Provider names
  must exist in config.yaml — stale names (e.g. the pruned zen-* models) are
  removed, not tolerated.
- AGENT_TOOLS gives each agent a lean tool allowlist (None = full toolbox,
  capped at MAX_AGENT_TOOLS) to stay under provider tool limits and cut TPM.
- run() streams (provider, chunk) like orchestrator.process and never raises;
  AllProvidersDown yields ("error", ...). After MAX_TOOL_ROUNDS it does one
  no-tools pass so the agent always returns text.
- Background tasks (submit/wait_for) record status/result/error on the
  BackgroundTask; failures are stored, not raised. wait_for raises KeyError
  for unknown ids and TimeoutError on budget expiry (job keeps running).
- Custom agents persist to data/custom_agents.json; load/save failures print
  a warning and continue (fail-soft, intentional).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic

from .providers import ToolCallDelta
from .router import AllProvidersDown

# Per-agent tool allowlists: lean schemas = no provider 400s (Groq caps
# tools at 128), lower TPM burn, and better model focus.
AGENT_TOOLS: dict[str, list[str]] = {
    "coder": [
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "search_code",
        "run_command",
        "run_tests",
        "git_status",
        "git_diff",
        "git_log",
        "get_diagnostics",
        "code_exec_python",
        "code_exec_shell",
        "fs_read_file",
        "fs_write_file",
        "fs_edit_file",
        "fs_list_dir",
        "fs_search_files",
        "lint_file",
        "format_code",
        "typecheck_file",
        "find_symbol",
        "find_references",
        "web_search",
        "remember",
        "recall",
    ],
    "researcher": [
        "web_search",
        "browser_navigate",
        "browser_extract",
        "browser_click",
        "browser_wait",
        "browser_screenshot",
        "search_knowledge",
        "index_path",
        "remember",
        "recall",
        "read_file",
        "code_exec_python",
        "deep_research",
        "search_conversations",
        "github_repo",
    ],
    "analyst": [
        "code_exec_python",
        "code_exec_shell",
        "read_file",
        "search_knowledge",
        "search_code",
        "list_dir",
        "web_search",
        "remember",
        "recall",
        "system_info",
        "browser_extract",
        "analyze_csv",
        "github_repo",
        "github_issues",
        "github_prs",
        "github_standup",
        "github_ci",
    ],
    "writer": [
        "web_search",
        "read_file",
        "write_file",
        "remember",
        "recall",
        "search_knowledge",
        "browser_navigate",
        # mail tools: the writer must fetch the email itself instead of
        # interrogating the user for content it can read.
        "mail_unread",
        "mail_search",
        "mail_read",
        "mail_draft",
        "mail_send_draft",
    ],
    "fast": [
        "remember",
        "recall",
        "web_search",
        "system_info",
    ],
    "code_reviewer": [
        "read_file",
        "search_code",
        "list_dir",
        "run_tests",
        "get_diagnostics",
        "code_exec_python",
        "fs_read_file",
        "fs_search_files",
        "find_symbol",
        "find_references",
        "lint_file",
        "typecheck_file",
        "remember",
        "recall",
        "github_repo",
        "github_prs",
    ],
    "docwriter": [
        "read_file",
        "write_file",
        "fs_read_file",
        "fs_write_file",
        "fs_list_dir",
        "list_dir",
        "search_knowledge",
        "search_code",
        "web_search",
        "remember",
        "recall",
    ],
    "designer": [
        "read_file",
        "web_search",
        "screenshot",
        "analyze_image",
        "browser_navigate",
        "browser_screenshot",
        "browser_extract",
        "remember",
        "recall",
    ],
    "general": None,  # full toolbox (capped to 60 by MAX_AGENT_TOOLS below)
}

MAX_AGENT_TOOLS = 60  # safety cap for any agent (Groq limit is 128)


@dataclass
class AgentSpec:
    """A named specialist agent pinned to specific providers."""

    name: str
    description: str
    providers: list[str]  # ordered provider names (empty = full chain)
    system_prompt_suffix: str
    task_type: str = "general"
    tools_enabled: bool = True
    icon: str = "🤖"


# Intent task_type -> agent name
INTENT_AGENT_MAP = {
    "coding": "coder",
    "research": "researcher",
    "analysis": "analyst",
    "creative": "writer",
    "fast": "fast",
    "reasoning": "analyst",
    "planning": "general",
    "vision": "general",
    "explanation": "general",
    "general": "general",
}


DEFAULT_AGENTS: dict[str, AgentSpec] = {
    "coder": AgentSpec(
        name="coder",
        description="Senior software engineer — coding, debugging, refactoring, code review, architecture",
        providers=[
            "groq-gpt-oss-120b",
            "groq-gpt-oss-120b-k2",
            "groq-gpt-oss-120b-k3",
            "groq-qwen3.8-27b",
            "nim-nemo-super",
        ],
        system_prompt_suffix=(
            "You are JARVIS's dedicated CODING AGENT — a senior software engineer. "
            "Write production-quality code with type hints and error handling. "
            "Read files before editing. Run tests when relevant. Be thorough but concise. "
            "Always finish with the complete working code — never leave it half-done. "
            "New files go to their mapped folders (docs/plans, data/research, logs, "
            "D:/jarvis/media, workspace/scripts, or workspace) — never the repo root."
        ),
        task_type="coding",
        icon="👨‍💻",
    ),
    "researcher": AgentSpec(
        name="researcher",
        description="Deep research — web search, multi-source synthesis, cited reports",
        providers=["gemini-3.5-flash", "gemini-3.6-flash", "groq-gpt-oss-120b"],
        system_prompt_suffix=(
            "You are JARVIS's RESEARCH AGENT. Use web_search and knowledge tools extensively. "
            "Cross-reference multiple sources, note disagreements, and provide well-structured findings. "
            "Always indicate source quality and confidence. "
            "Save reports to data/research/ (never the repo root); bulk media to D:/jarvis/media/."
        ),
        task_type="research",
        icon="🔬",
    ),
    "analyst": AgentSpec(
        name="analyst",
        description="Analysis — data, metrics, reasoning, tradeoffs, deep thinking",
        providers=["nim-nemo-super", "groq-gpt-oss-120b", "gemini-3.5-flash"],
        system_prompt_suffix=(
            "You are JARVIS's ANALYSIS AGENT. Think deeply and step by step. "
            "Examine problems from multiple angles, quantify tradeoffs, and surface non-obvious insights. "
            "Show your reasoning."
        ),
        task_type="analysis",
        icon="📊",
    ),
    "writer": AgentSpec(
        name="writer",
        description="Creative writing — brainstorming, copy, stories, naming, marketing",
        providers=[
            "xkiro-qwen37-plus",
            "xkiro-mistral-large",
            "gemini-3.5-flash",
            "groq-gpt-oss-120b",
        ],
        system_prompt_suffix=(
            "You are JARVIS's WRITER AGENT. ACT, never interrogate: when asked to draft a "
            "reply to a mail, call mail_search/mail_read to fetch it yourself, then produce "
            "the complete draft immediately. Make sensible professional assumptions for any "
            "missing detail instead of asking questions. Output the finished piece only. "
            "Save files to workspace/ (never the repo root)."
        ),
        task_type="creative",
        icon="✍️",
    ),
    "fast": AgentSpec(
        name="fast",
        description="Quick answers — short questions, summaries, yes/no, quick lookups",
        providers=["groq-gpt-oss-20b", "nim-nemo-lightning", "groq-qwen3.8-27b"],
        system_prompt_suffix=(
            "You are JARVIS's FAST AGENT. Answer in the fewest words possible. "
            "No preamble, no filler — just the answer."
        ),
        task_type="fast",
        icon="⚡",
    ),
    "code_reviewer": AgentSpec(
        name="code_reviewer",
        description="Code review — audits code for bugs, security, style; APPROVE/REVISE verdicts",
        providers=["groq-gpt-oss-120b", "nim-nemo-super", "gemini-3.5-flash"],
        system_prompt_suffix=(
            "You are JARVIS's CODE REVIEWER AGENT — a meticulous debugger and security auditor. "
            "For any code you receive: (1) list concrete bugs and security issues with line references, "
            "(2) note style/perf problems briefly, (3) end with a clear verdict line: "
            "'Verdict: APPROVE' or 'Verdict: REVISE' with the single most important reason. "
            "No praise, no filler — only findings."
        ),
        task_type="coding",
        icon="🔍",
    ),
    "docwriter": AgentSpec(
        name="docwriter",
        description="Documentation — READMEs, API docs, design docs, tutorials",
        providers=["gemini-3.5-flash", "groq-gpt-oss-120b"],
        system_prompt_suffix=(
            "You are JARVIS's DOCUMENTATION AGENT. Write clear, structured markdown docs: "
            "READMEs, API references, usage examples. Concise but complete."
        ),
        task_type="general",
        icon="📝",
    ),
    "designer": AgentSpec(
        name="designer",
        description="Design — UI critique, color/typography, layout, mockup feedback",
        providers=["gemini-3.5-flash", "groq-gpt-oss-120b"],
        system_prompt_suffix=(
            "You are JARVIS's DESIGN AGENT — a senior product designer. "
            "Critique UIs, suggest palettes, typography, layout; reference design principles."
        ),
        task_type="creative",
        icon="🎨",
    ),
    "general": AgentSpec(
        name="general",
        description="Everyday assistant — conversations, tasks, PC control, anything",
        providers=[],
        system_prompt_suffix="",
        task_type="general",
        icon="🤖",
    ),
}


@dataclass
class BackgroundTask:
    task_id: str
    agent: str
    prompt: str
    status: str = "queued"  # queued | running | done | failed
    result: str = ""
    provider: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    _async_task: asyncio.Task | None = None

    @property
    def duration_s(self) -> float:
        if self.started_at == 0:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at


class AgentPool:
    """Runs specialist agents — in foreground or background — with isolated contexts."""

    MAX_TOOL_ROUNDS = 6
    CUSTOM_AGENTS_FILE = Path("data/custom_agents.json")

    def __init__(self, orchestrator, agents: dict[str, AgentSpec] | None = None):
        self.orch = orchestrator
        self.agents: dict[str, AgentSpec] = dict(agents or DEFAULT_AGENTS)
        self.tasks: dict[str, BackgroundTask] = {}
        self.on_complete: Callable[[BackgroundTask], Awaitable[None]] | None = None
        self._seq = 0
        self._load_custom_agents()

    # ---------- custom agents (persisted) ----------

    def _load_custom_agents(self):
        # P0: shared corrupt-file contract — quarantine aside (loud) instead
        # of silently dropping the user's custom agents.
        data = load_json_store(self.CUSTOM_AGENTS_FILE, default={}, owner="agent_pool", expect=dict)
        for name, d in data.items():
            try:
                tools = d.get("tools")
                if tools is None:
                    tools = AGENT_TOOLS.get(d.get("based_on", "general"))
                self.agents[name] = AgentSpec(
                    name=name,
                    description=d.get("description", "custom agent"),
                    providers=d.get("providers", []),
                    system_prompt_suffix=d.get("system_prompt_suffix", ""),
                    task_type=d.get("task_type", "general"),
                    tools_enabled=d.get("tools_enabled", True),
                    icon=d.get("icon", "🛠️"),
                )
                if tools:
                    AGENT_TOOLS[name] = tools
            except Exception as exc:
                print(f"[AgentPool] skipping malformed custom agent {name!r}: {exc}")

    def _save_custom_agents(self):
        custom = {}
        for name, spec in self.agents.items():
            if name in DEFAULT_AGENTS:
                continue
            custom[name] = {
                "description": spec.description,
                "providers": spec.providers,
                "system_prompt_suffix": spec.system_prompt_suffix,
                "task_type": spec.task_type,
                "tools_enabled": spec.tools_enabled,
                "icon": spec.icon,
                "based_on": "general",
            }
        if not save_json_atomic(self.CUSTOM_AGENTS_FILE, custom):
            print("[AgentPool] custom agents save failed")

    def create_agent(
        self,
        name: str,
        description: str = "",
        providers: list = None,
        system_prompt_suffix: str = "",
        task_type: str = "general",
        icon: str = "🛠️",
        tools: list = None,
        tools_enabled: bool = True,
    ) -> AgentSpec:
        """Create and persist a custom agent with its own dedicated brain."""
        if not name or not re.match(r"^[a-z][a-z0-9_]{1,24}$", name):
            raise ValueError("name must be lowercase letters/digits/_ (2-25 chars)")
        if name in DEFAULT_AGENTS:
            raise ValueError(f"'{name}' is a built-in agent")
        # validate providers against live config
        known = {p.name for p in self.orch.cfg.providers}
        bad = [p for p in (providers or []) if p not in known]
        if bad:
            raise ValueError(f"unknown providers: {', '.join(bad)}")
        spec = AgentSpec(
            name=name,
            description=description or f"custom agent {name}",
            providers=providers or [],
            system_prompt_suffix=system_prompt_suffix,
            task_type=task_type,
            tools_enabled=tools_enabled,
            icon=icon,
        )
        if tools:
            AGENT_TOOLS[name] = tools
        self.agents[name] = spec
        self._save_custom_agents()
        return spec

    def delete_agent(self, name: str) -> bool:
        if name in DEFAULT_AGENTS or name not in self.agents:
            return False
        del self.agents[name]
        AGENT_TOOLS.pop(name, None)
        self._save_custom_agents()
        return True

    # ---------- routing ----------

    def route(self, prompt: str) -> str:
        """Pick the best agent for a prompt using the intent classifier."""
        try:
            result = self.orch.intent_classifier.classify(prompt)
            agent_name = INTENT_AGENT_MAP.get(result.task_type, "general")
            if agent_name in self.agents:
                return agent_name
        except Exception:
            pass
        return "general"

    # ---------- foreground execution ----------

    async def run(self, agent_name: str, prompt: str):
        """Run an agent turn in the foreground; yields (provider, chunk) like orchestrator.process."""
        spec = self.agents.get(agent_name)
        if spec is None:
            yield "error", f"unknown agent '{agent_name}' — use /agents to list"
            return

        history = self._build_history(spec, prompt)
        mem_ctx = self.orch._memory_context(prompt)
        if mem_ctx:
            history.insert(1, {"role": "system", "content": mem_ctx})

        # Lean per-agent tool schemas: avoids provider tool-count limits (Groq
        # caps at 128), cuts TPM burn, and focuses the model.
        tool_schemas = None
        if spec.tools_enabled:
            all_schemas = self.orch.toolbox.schemas()
            allow = AGENT_TOOLS.get(spec.name)
            if allow:
                allow_set = set(allow)
                tool_schemas = [
                    s for s in all_schemas if s.get("function", {}).get("name") in allow_set
                ]
            else:
                tool_schemas = all_schemas[:MAX_AGENT_TOOLS]
            if not tool_schemas:
                tool_schemas = None

        for _ in range(self.MAX_TOOL_ROUNDS):
            content_parts: list[str] = []
            pending: dict[int, dict] = {}
            provider_used = ""

            # Brain Manager: use the agent's dedicated brain if one is assigned
            # (primary first, then its fallbacks); otherwise the spec's static list.
            only_providers = spec.providers or None
            brain_mgr = getattr(self.orch, "brain_manager", None)
            if brain_mgr is not None:
                dedicated = brain_mgr.providers_for(spec.name)
                if dedicated:
                    only_providers = dedicated

            try:
                stream = self.orch.router.chat(
                    history,
                    tools=tool_schemas,
                    task_type=spec.task_type,
                    only=only_providers,
                )
                async for name, item in stream:
                    if isinstance(item, str):
                        provider_used = name
                        content_parts.append(item)
                        yield name, item
                    elif isinstance(item, ToolCallDelta):
                        acc = pending.setdefault(item.index, {"id": None, "name": None, "args": []})
                        if item.id:
                            acc["id"] = item.id
                        if item.name:
                            acc["name"] = item.name
                        acc["args"].append(item.args)
            except AllProvidersDown as exc:
                # report failure to the brain manager, then retry once on the full chain
                if brain_mgr is not None:
                    with contextlib.suppress(Exception):
                        brain_mgr.record_failure(spec.name, provider_used or "unknown")
                yield "error", f"[{spec.name}] all agent providers busy: {exc}"
                return

            text = "".join(content_parts)

            # Parse XML/JSON-style tool calls from content (some models do this)
            clean_text, xml_calls = OrchestratorParser.parse(text)
            if clean_text != text:
                text = clean_text

            calls = []
            for idx in sorted(pending):
                p = pending[idx]
                if not p["name"]:
                    continue
                calls.append((p["id"] or f"call_{idx}", p["name"], "".join(p["args"])))
            for cid, name, args in xml_calls:
                calls.append((cid, name, args))

            if not calls:
                history.append({"role": "assistant", "content": text})
                # report success to the brain manager (dedicated brain is healthy)
                if brain_mgr is not None and provider_used:
                    with contextlib.suppress(Exception):
                        brain_mgr.record_success(spec.name, provider_used)
                # learn from the interaction
                with contextlib.suppress(Exception):
                    self.orch.preferences.learn_from_turn(prompt, text)
                return

            history.append(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {"id": cid, "type": "function", "function": {"name": n, "arguments": a}}
                        for cid, n, a in calls
                    ],
                }
            )

            for cid, name, args in calls:
                yield "tool", f"⚙ {name}({args[:60]})"
                try:
                    result = await self.orch.toolbox.execute(name, args)
                except Exception as exc:
                    result = f"error: {exc}"
                history.append({"role": "tool", "tool_call_id": cid, "content": result})

        # Fallback: loop exhausted (or ended) without a final text answer —
        # do ONE no-tools pass so the agent always returns text, never nothing.
        try:
            got_text = "".join(content_parts).strip() if isinstance(content_parts, list) else ""
        except Exception:
            got_text = ""
        if not got_text:
            history.append(
                {
                    "role": "user",
                    "content": "Enough tool use. Now give your final answer as text only — complete and self-contained.",
                }
            )
            try:
                async for name, item in self.orch.router.chat(
                    history,
                    tools=None,
                    task_type=spec.task_type,
                    only=only_providers,
                ):
                    if isinstance(item, str) and item.strip():
                        yield name, item
            except Exception:
                pass

    # ---------- background execution ----------

    def submit(self, agent_name: str, prompt: str) -> str:
        """Submit a task to run in the background. Returns task_id immediately."""
        if agent_name not in self.agents:
            raise ValueError(f"unknown agent '{agent_name}'")
        self._seq += 1
        task_id = f"{agent_name[:3]}-{self._seq:03d}-{uuid.uuid4().hex[:4]}"
        task = BackgroundTask(task_id=task_id, agent=agent_name, prompt=prompt)
        self.tasks[task_id] = task
        task._async_task = asyncio.create_task(self._run_background(task))
        return task_id

    async def submit_auto(self, prompt: str) -> tuple[str, str]:
        """Auto-route a prompt to the best agent and run in background. Returns (agent, task_id)."""
        agent_name = self.route(prompt)
        task_id = self.submit(agent_name, prompt)
        return agent_name, task_id

    async def wait_for(self, task_id: str, timeout: float = 120.0) -> BackgroundTask:
        """Wait for a background task to finish; returns the task.

        Raises KeyError for unknown ids, TimeoutError when the budget
        expires (the job keeps running — caller should fall back to an
        async handoff note instead of dropping the result).
        """
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"unknown background task '{task_id}'")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while task.status in ("queued", "running"):
            if loop.time() >= deadline:
                raise TimeoutError(f"background task '{task_id}' still {task.status}")
            await asyncio.sleep(0.5)
        return task

    async def _run_background(self, task: BackgroundTask):
        task.status = "running"
        task.started_at = time.time()
        parts: list[str] = []
        try:
            async for name, chunk in self.run(task.agent, task.prompt):
                if name == "error":
                    task.status = "failed"
                    task.error = str(chunk)
                    return
                if name != "tool":
                    parts.append(chunk)
            task.result = "".join(parts).strip()
            task.provider = self.orch.router.last_provider or ""
            task.status = "done"
        except Exception as exc:
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished_at = time.time()
            # Auto-review hook: coder output gets a code_reviewer pass before
            # the user sees it (the agents collaborate automatically).
            try:
                team = getattr(self.orch, "agent_team", None)
                if team is not None and task.agent == "coder" and task.status == "done":
                    await team.maybe_auto_review(task)
            except Exception:
                pass
            if self.on_complete is not None:
                with contextlib.suppress(Exception):
                    await self.on_complete(task)

    # ---------- introspection ----------

    def list_tasks(self, status: str = "") -> list[BackgroundTask]:
        tasks = list(self.tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def get_task(self, task_id: str) -> BackgroundTask | None:
        return self.tasks.get(task_id)

    def running_count(self) -> int:
        return sum(1 for t in self.tasks.values() if t.status in ("queued", "running"))

    def agents_overview(self) -> list[dict]:
        busy_agents = {t.agent for t in self.tasks.values() if t.status in ("queued", "running")}
        out = []
        for spec in self.agents.values():
            out.append(
                {
                    "name": spec.name,
                    "icon": spec.icon,
                    "description": spec.description,
                    "providers": spec.providers or ["<full failover chain>"],
                    "busy": spec.name in busy_agents,
                }
            )
        return out

    # ---------- helpers ----------

    def _build_history(self, spec: AgentSpec, prompt: str) -> list[dict]:
        base = self.orch.persona.build_system_prompt(self.orch.cfg.system_prompt)
        if spec.system_prompt_suffix:
            base = f"{base}\n\n{spec.system_prompt_suffix}"
        return [
            {"role": "system", "content": base},
            {"role": "user", "content": prompt},
        ]


class OrchestratorParser:
    """Reuses the orchestrator's XML/JSON tool-call parser without importing it (avoids cycles)."""

    @staticmethod
    def parse(text: str) -> tuple[str, list[tuple[str, str, str]]]:
        import re

        calls: list[tuple[str, str, str]] = []
        clean = text

        tc_pattern = re.compile(
            r"<tool_call>\s*<function=(\w+)>\s*(.*?)\s*</function>\s*</tool_call>",
            re.DOTALL,
        )
        for i, match in enumerate(tc_pattern.finditer(text)):
            name = match.group(1)
            params_raw = match.group(2)
            params = {}
            for pm in re.finditer(
                r"<parameter=(\w+)>\s*(.*?)\s*</parameter>", params_raw, re.DOTALL
            ):
                params[pm.group(1)] = pm.group(2)
            calls.append((f"xml_call_{i}", name, json.dumps(params)))

        json_tc_pattern = re.compile(r'\{"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]+\})')
        offset = len(calls)
        for i, match in enumerate(json_tc_pattern.finditer(text)):
            calls.append((f"json_call_{offset + i}", match.group(1), match.group(2)))

        if calls:
            clean = tc_pattern.sub("", text)
            clean = json_tc_pattern.sub("", clean)
            clean = clean.strip()

        return clean, calls

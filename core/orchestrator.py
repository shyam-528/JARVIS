"""Orchestrator — the turn lifecycle. Everything a user message touches flows through here.

Owns: prompt assembly (time context, curated memory, episodic recall, RAG
passages, background-task notes), the tool-call loop (capped at
MAX_TOOL_ROUNDS), auto-background dispatch of heavy requests, automation
rules (suggestions + fired actions), session/history management, and
post-turn hooks (self-improvement, memory storage, budget alerts).

Rules:
- The reply path must never die because an auxiliary subsystem did. RAG,
  skill acquisition, automation, and bg-dispatch failures are swallowed by
  design ("graceful degradation"); the main reply failure path is the
  router's job and surfaces as AllProvidersDown to callers.
- Memory writes are dual by design: core/memory.py (curated, prompt-injected)
  AND core/memory_v2.py (episodic, not prompt-injected). Never collapse one
  into the other — see the boundary docstrings in those modules.
- This class wires ~25 subsystems in __init__; each wiring is individually
  fail-soft so a broken optional component degrades JARVIS instead of
  preventing boot.

Map — read this file in sections, not top-to-bottom. Line numbers drift as the
file grows: trust the `def` name, then grep it.
- ~L101  `__init__` — subsystem wiring ONLY, in boot order: brain core →
         memory/RAG → automation bus + workflow engine → plugins/skills →
         brain manager + agent team → toolbox → scheduler/notify/mail/
         whatsapp/proactive-push → storm queue → agent pool + auto-background
         (`_bg_classify` delegating to bg_router). No turn logic lives here.
- ~L835  `_add_job(...)` — the ONE way scheduled jobs get registered: it is
         loud on failure (a job that silently never registers is a lost
         capability, not a degraded one) and returns False instead of raising.
- ~L865  `start()` — job registration table (all via `_add_job`; names are
         re-replaced, never duplicated on reboot), missed-slot briefing
         catch-up, A2A mesh listener, federated inference, unified-brain
         channel (service child dials this brain — never binds :8082).
- ~L1025 context builders — `_memory_context`, `_episodic_context`,
         `_time_context`, `_drain_bg_notes` (finished background-agent jobs).
- ~L1139 classifiers/selectors — `_detect_task_type`, `_select_ensemble_mode`,
         `_select_reasoning_strategy`, `_should_use_rag`,
         `_should_self_improve`, `_extract_concepts`, `_parse_xml_tool_calls`.
- ~L1376 `process()` — THE turn pipeline: auto-background dispatch → context
         assembly (concurrent gather) → model loop with tool execution
         (capped at MAX_TOOL_ROUNDS) → post-turn hooks. Everything a user
         message touches is reachable from here.
- ~L1765 sessions & history — reset/resume/new-session, orphan load, token
         accounting, summarizer trim + hard trim.
- ~L1889 introspection — `chain_status`, `stats`, `offline_draft`.
- ~L1950 alerts — `speak_alert` (uses the configured paired voices),
         `maybe_budget_alert`.
- ~L2010 automation — `_auto_state`/`set_auto_state` (the auto-act fence:
         notify_* + remember + hard-fenced schedule_task), audit trail,
         suggestion handling, event-bus firing, failed-task suggestions.
- ~L2297 post-turn memory — conversation → episodic store + fact extraction.
- ~L2349 `aclose()` — teardown in reverse boot order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator

from .agent_mesh import MeshNetwork
from .code_agent import CodeAgent
from .config import Config
from .dialogue import DialogueManager
from .distributed import DistributedBrain
from .ensemble import ModelEnsemble, MoERouter
from .intent import IntentClassifier
from .knowledge import DocumentIndexer, KnowledgeStore
from .memory import MemoryStore
from .memory_v2 import MemoryV2
from .ml_proactive import MLProactiveEngine
from .multimodal import MultiModalBrain
from .notify import NotificationManager
from .persistence import ConversationStore
from .persona import PersonaManager
from .plugin_registry import PluginRegistry
from .preferences import PreferenceManager
from .proactive import ProactiveEngine
from .prompt_opt import PromptOptimizer
from .providers import OpenAICompatClient, ToolCallDelta
from .rag_v2 import RAGv2
from .reasoning import ReasoningEngine
from .router import FailoverRouter, UsageLedger, estimate_tokens
from .scheduler import TaskScheduler
from .self_improve import SelfImprovementLoop
from .summarizer import Summarizer, SummaryConfig
from .tool_use_v2 import ToolUseV2
from .tools import default_toolbox

# Web/API turns stay open, so a background-dispatched job is awaited up to
# this long and answered inline (shown + spoken). Slower jobs keep the old
# fire-and-forget handoff; their result lands via bg_notes next turn.
BG_WAIT_S = 120.0


VOICE_SPOKEN_INSTRUCTION = (
    "You are in a live voice conversation. Provide your response normally, but at the end "
    "include a <spoken>...</spoken> tag containing a brief, natural, 1-2 sentence spoken summary "
    "suitable for text-to-speech delivery."
)


def _embeddings_status(store) -> dict:
    """Vector-search flag for stats()/health (T6, ROADMAP P2).

    getattr-safe: partial/test orchestrators without a knowledge store report
    "unknown", never raise. The store's own embedder_status() owns the
    on/off/not-tried-yet logic.
    """
    try:
        fn = getattr(store, "embedder_status", None)
        if callable(fn):
            status = fn()
            if isinstance(status, dict) and "state" in status:
                return status
    except Exception:
        pass
    return {"state": "unknown"}


class Orchestrator:
    BASE_CONTEXT_BUDGET_TOKENS = 24000
    MIN_CONTEXT_BUDGET_TOKENS = 4000
    MAX_TOOL_ROUNDS = 5

    def __init__(self, cfg: Config, on_event=None):
        self.cfg = cfg
        self.client = OpenAICompatClient(
            connect_timeout_s=cfg.watchdog.connect_timeout_s,
            read_timeout_s=cfg.watchdog.read_timeout_s,
        )
        self.ledger = UsageLedger(cfg.db_path)
        self.router = FailoverRouter(
            cfg.providers,
            self.client,
            self.ledger,
            watchdog_s=cfg.watchdog.first_token_timeout_s,
            context_overflow_hook=self._hard_trim,
            on_event=on_event,
        )
        self.ensemble = ModelEnsemble(self.router, self.client)
        self.moe_router = MoERouter(self.router)
        self.reasoning = ReasoningEngine(self.router, self.client)
        self.rag = RAGv2(cfg)

        # Give RAG an LLM call for cited answer generation
        async def _rag_llm(system_prompt: str, user_prompt: str) -> str:
            out = []
            async for _, chunk in self.router.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=None,
                task_type="reasoning",
            ):
                if isinstance(chunk, str):
                    out.append(chunk)
            return "".join(out)

        self.rag.set_llm(_rag_llm)
        self.memory_v2 = MemoryV2(cfg)
        self.memory = MemoryStore(cfg.db_path.parent / "memory.db")
        self.scheduler = TaskScheduler(cfg)
        self.scheduler.set_orchestrator(self)
        # T3 (ROADMAP P0): failed job registrations are product-visible state,
        # not console-only. `_add_job` appends here on failure; `stats()` and
        # GET /api/tasks/errors surface it to the dashboard/HUD and /tasks view.
        self._job_errors: list[dict] = []
        self.proactive = ProactiveEngine(cfg, self.memory)
        self.ml_proactive = MLProactiveEngine(cfg, self.memory)
        self.registry = PluginRegistry(cfg)
        # Build notification config bridging YAML notify + telegram sections
        from .notify import Channel as NCChannel
        from .notify import NotificationConfig as NC

        try:
            default_channel = NCChannel(cfg.notify.default_channel)
        except Exception:
            default_channel = NCChannel.TELEGRAM
        self.notify_config = NC(
            ntfy_server=cfg.notify.ntfy_server,
            ntfy_topic=cfg.notify.ntfy_topic,
            ntfy_auth_token=cfg.notify.ntfy_auth_token,
            telegram_bot_token=cfg.telegram.bot_token if cfg.telegram.enabled else "",
            telegram_chat_id=cfg.telegram.chat_id if cfg.telegram.enabled else "",
            pushover_user_key=cfg.notify.pushover_user_key,
            pushover_app_token=cfg.notify.pushover_app_token,
            webhook_url=cfg.notify.webhook_url,
            webhook_method=cfg.notify.webhook_method,
            whatsapp_phone=cfg.notify.whatsapp_phone,
            whatsapp_apikey=cfg.notify.whatsapp_apikey,
            default_channel=default_channel,
            default_tags=list(cfg.notify.default_tags or []),
        )
        self.notify_manager = NotificationManager(self.notify_config)

        # Generate/load mesh API key
        mesh_api_key = self._get_or_create_mesh_api_key(cfg)
        cfg.a2a.api_key = mesh_api_key

        self.mesh = MeshNetwork(
            local_agent_id=f"jarvis-{uuid.uuid4().hex[:8]}",
            name="JARVIS",
            host=cfg.a2a.host,
            port=cfg.a2a.port if hasattr(cfg, "a2a") else 8082,
            api_key=mesh_api_key,
            capabilities={
                "skills": [
                    "pc_control",
                    "web_search",
                    "memory",
                    "vision",
                    "scheduling",
                    "automation",
                ],
                "tools": [
                    "system_info",
                    "web_search",
                    "remember",
                    "recall",
                    "schedule_task",
                    "screenshot",
                    "analyze_image",
                ],
                "version": "1.0.0",
            },
        )
        self.distributed = DistributedBrain(self.router, self.client, cfg, self.mesh)
        self.knowledge_store = KnowledgeStore(cfg.db_path.parent / "knowledge.db")
        self.knowledge_indexer = DocumentIndexer(self.knowledge_store)
        self.persistence = ConversationStore(cfg.db_path.parent / "conversations.db")
        self.session_id: str | None = None

        ca_cfg = getattr(cfg, "code_agent", None)
        self.code_agent = CodeAgent(
            auto_approve=ca_cfg.auto_approve if ca_cfg else False,
            blocked_commands=ca_cfg.blocked_commands if ca_cfg else None,
            max_file_size_mb=ca_cfg.max_file_size_mb if ca_cfg else 10.0,
            timeout_s=ca_cfg.timeout_s if ca_cfg else 120,
        )
        # Will set tool_use after ToolUseV2 is created

        # Deep automation: event bus + workflow engine (must exist before toolbox
        # so workflow tools can register)
        from .events import EventBus
        from .workflow import WorkflowEngine

        self.event_bus = EventBus(cfg.db_path.parent / "events.db")
        self.event_bus.start()
        self.workflow_engine = WorkflowEngine(
            self, self.event_bus, cfg.db_path.parent / "workflows.db"
        )
        from .flow_composer import FlowComposer

        self.flow_composer = FlowComposer(self)

        # P6: Continuous intelligence — learns best provider per task from outcomes
        from .routing_optimizer import RoutingOptimizer

        self.routing_optimizer = RoutingOptimizer(cfg.db_path.parent / "routing_scores.db")
        self.router.optimizer = self.routing_optimizer

        # P6: skill acquisition — watches repeated tool patterns -> proposes workflows
        from .skill_acquisition import SkillAcquisition

        self.skill_acquisition = SkillAcquisition(cfg.db_path.parent / "skill_patterns.json")

        # P6: knowledge distiller — nightly conversation -> durable facts
        from .knowledge_distiller import KnowledgeDistiller

        self.distiller = KnowledgeDistiller(self)

        # P7: specialist brains — deep research, data analysis, GitHub PM
        from .data_analyst import DataAnalyst
        from .deep_research import DeepResearcher
        from .github_sync import GitHubSync

        self.deep_research = DeepResearcher(self)
        self.data_analyst = DataAnalyst(self)
        self.github_sync = GitHubSync(self)

        # Brain Manager: dedicated per-agent brains, maintained by JARVIS.
        # (First-boot auto-assign happens after agent_pool is created below.)
        from .brain_manager import BrainManager

        self.brain_manager = BrainManager(self, cfg.db_path.parent / "brain_assignments.db")

        self.toolbox = default_toolbox(
            self.memory,
            self.scheduler,
            registry=self.registry,
            notify_manager=self.notify_manager,
            mesh=self.mesh,
            ml_engine=self.ml_proactive,
            knowledge_store=self.knowledge_store,
            knowledge_indexer=self.knowledge_indexer,
            code_agent=self.code_agent,
            workflow_engine=self.workflow_engine,
            rag=self.rag,
        )

        # Load installed plugins/skills into the toolbox
        try:
            n = self.registry.load_all_skills(self.toolbox)
            if n:
                print(f"[plugins] loaded {n} skill(s)")
        except Exception as exc:
            print(f"[plugins] skill loading skipped: {exc}")

        # P6: distillation tool + nightly schedule (02:00)
        from .tools import Tool as _Tool

        _orch = self

        async def _run_distillation() -> str:
            stats = await _orch.distiller.run_nightly()
            return (
                f"distilled {stats['sessions_distilled']} sessions, "
                f"stored {stats['facts_stored']} facts, pruned {stats['memories_pruned']} stale memories"
            )

        self.toolbox.register(
            _Tool(
                name="run_distillation",
                description="Nightly brain maintenance: compress old conversations into durable facts and prune stale memories",
                parameters={"type": "object", "properties": {}},
                handler=_run_distillation,
            )
        )
        self._add_job(
            "nightly distillation", "daily", {"hour": 2, "minute": 0}, "run_distillation", {}
        )

        # P7: specialist tools (research / analysis / GitHub PM)
        _o = self

        async def _deep_research(topic: str, depth: int = 1) -> str:
            report = await _o.deep_research.research(topic, depth=depth, save_report=True)
            srcs = "\n".join(f"[{i}] {s.title} — {s.url}" for i, s in enumerate(report.sources, 1))
            return (
                f"{report.summary}\n\nSources:\n{srcs or '(none)'}\n\n"
                f"Report saved: {report.report_path or '(not saved)'}"
            )

        async def _analyze_csv(path: str, question: str = "") -> str:
            return await _o.data_analyst.analyze_csv(path, question)

        async def _github_repo(repo: str) -> str:
            return await _o.github_sync.repo_overview(repo)

        async def _github_issues(repo: str, state: str = "open") -> str:
            return await _o.github_sync.list_issues(repo, state=state)

        async def _github_prs(repo: str, state: str = "open") -> str:
            return await _o.github_sync.list_prs(repo, state=state)

        async def _github_standup(repos_json: str = "[]") -> str:
            import json as _json

            try:
                repos = _json.loads(repos_json) if repos_json else []
            except _json.JSONDecodeError:
                repos = [r.strip() for r in repos_json.split(",") if r.strip()]
            if not repos:
                return 'provide repos: /github standup repos=["owner/repo"]'
            return await _o.github_sync.standup(repos)

        async def _github_ci(repo: str) -> str:
            return await _o.github_sync.check_ci(repo)

        self.toolbox.register(
            _Tool(
                name="deep_research",
                description="Deep research: multi-source web + knowledge-base gathering, cited synthesis, saved markdown report. Use for thorough investigations. depth=2 adds follow-up angles.",
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "depth": {"type": "integer", "default": 1},
                    },
                    "required": ["topic"],
                },
                handler=_deep_research,
            )
        )
        self.toolbox.register(
            _Tool(
                name="analyze_csv",
                description="Full data analysis on a CSV file: descriptive stats, charts (saved as PNG), LLM insights, and a markdown report",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "question": {"type": "string", "default": ""},
                    },
                    "required": ["path"],
                },
                handler=_analyze_csv,
            )
        )
        self.toolbox.register(
            _Tool(
                name="github_repo",
                description="GitHub repo overview: stars, forks, open issues, default branch, language",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/repo or full GitHub URL"}
                    },
                    "required": ["repo"],
                },
                handler=_github_repo,
            )
        )
        self.toolbox.register(
            _Tool(
                name="github_issues",
                description="List GitHub issues (and PRs) for a repo",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "state": {"type": "string", "default": "open"},
                    },
                    "required": ["repo"],
                },
                handler=_github_issues,
            )
        )
        self.toolbox.register(
            _Tool(
                name="github_prs",
                description="List GitHub pull requests for a repo",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "state": {"type": "string", "default": "open"},
                    },
                    "required": ["repo"],
                },
                handler=_github_prs,
            )
        )
        self.toolbox.register(
            _Tool(
                name="github_standup",
                description='Generate a daily standup report across repos (recent commits, PRs, issues). repos_json like ["owner/repo"] or comma-separated',
                parameters={
                    "type": "object",
                    "properties": {"repos_json": {"type": "string", "default": "[]"}},
                    "required": [],
                },
                handler=_github_standup,
            )
        )
        self.toolbox.register(
            _Tool(
                name="github_ci",
                description="Check GitHub Actions CI status for a repo; alerts on failures via notifications",
                parameters={
                    "type": "object",
                    "properties": {"repo": {"type": "string"}},
                    "required": ["repo"],
                },
                handler=_github_ci,
            )
        )

        # ---- Brain Manager: custom agents + dedicated brains (managed by JARVIS) ----

        async def _create_agent(
            name: str,
            description: str = "",
            providers_json: str = "[]",
            system_prompt: str = "",
            task_type: str = "general",
            icon: str = "🛠️",
        ) -> str:
            import json as _json

            try:
                providers = _json.loads(providers_json) if providers_json else []
            except _json.JSONDecodeError as e:
                return f"error: providers_json invalid ({e})"
            try:
                spec = self.agent_pool.create_agent(
                    name,
                    description=description,
                    providers=providers,
                    system_prompt_suffix=system_prompt,
                    task_type=task_type,
                    icon=icon,
                )
            except ValueError as e:
                return f"error: {e}"
            # give the new agent a dedicated brain immediately
            self.brain_manager.auto_assign_all()
            brain = self.brain_manager.get(spec.name)
            prim = brain.primary if brain else "(full chain)"
            return (
                f"agent '{spec.name}' created with its own brain: {prim} "
                f"({len(providers)} pinned providers). Use: /bg {spec.name} <task> or /agents"
            )

        async def _agent_brain(agent: str = "", action: str = "show", provider: str = "") -> str:
            """Manage agent brains: show / auto / set."""
            if action == "show" or not agent:
                rows = self.brain_manager.all()
                if not rows:
                    return "no brain assignments yet — run action=auto"
                lines = ["Agent brains:"]
                for a in rows:
                    fbs = f" | fallbacks: {', '.join(a.fallbacks[:3])}" if a.fallbacks else ""
                    lines.append(f"- {a.agent}: {a.primary}{fbs} ({a.reason})")
                return "\n".join(lines)
            if action == "auto":
                result = self.brain_manager.auto_assign_all()
                return "auto-assigned brains: " + ", ".join(f"{k}->{v}" for k, v in result.items())
            if action == "set":
                if not provider:
                    return "error: provider required for action=set"
                known = {p.name for p in self.cfg.providers}
                if provider not in known:
                    return f"error: unknown provider '{provider}' (see /chain)"
                brain = self.brain_manager.assign(agent, provider, reason="manual")
                return f"{agent}'s brain set to {brain.primary}"
            return "error: action must be show|auto|set"

        async def _run_brain_maintenance() -> str:
            report = self.brain_manager.maintenance_pass()
            swaps = self.brain_manager.swap_log[-5:]
            extra = (" | swaps: " + "; ".join(swaps)) if swaps else ""
            return (
                f"brain maintenance: checked {report['checked']}, "
                f"swapped {report['swapped']}, refreshed {report['refreshed']}{extra}"
            )

        self.toolbox.register(
            _Tool(
                name="create_agent",
                description='Create a custom agent with its own dedicated brain: name (lowercase_snake), description, providers_json (list of provider names from /chain, e.g. ["groq-gpt-oss-120b"]), system_prompt (persona), task_type (coding|research|analysis|creative|fast|general)',
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string", "default": ""},
                        "providers_json": {"type": "string", "default": "[]"},
                        "system_prompt": {"type": "string", "default": ""},
                        "task_type": {"type": "string", "default": "general"},
                        "icon": {"type": "string", "default": "🛠️"},
                    },
                    "required": ["name"],
                },
                handler=_create_agent,
            )
        )
        self.toolbox.register(
            _Tool(
                name="agent_brain",
                description="Manage agent brains (dedicated model assignments). action=show lists all; action=auto reassigns best brains from learning; action=set requires provider (agent+provider+action=set)",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent": {"type": "string", "default": ""},
                        "action": {"type": "string", "default": "show"},
                        "provider": {"type": "string", "default": ""},
                    },
                    "required": [],
                },
                handler=_agent_brain,
            )
        )
        self.toolbox.register(
            _Tool(
                name="run_brain_maintenance",
                description="Health-check all agent brains; swaps unhealthy primaries to fallbacks",
                parameters={"type": "object", "properties": {}},
                handler=_run_brain_maintenance,
            )
        )

        # ---- Agent Team: collaboration pipelines (agents working together) ----

        async def _run_team_pipeline(pipeline: str, task: str) -> str:
            try:
                run = self.agent_team.start(pipeline, task)
            except ValueError as e:
                return f"error: {e}"
            flow = " -> ".join(s.agent for s in self.agent_team.pipelines[pipeline].steps)
            return (
                f"pipeline '{pipeline}' started ({flow}) — run {run.run_id}. "
                f"Agents collaborate in background; results arrive in chat."
            )

        async def _create_team_pipeline(
            name: str, description: str = "", steps_json: str = "[]"
        ) -> str:
            import json as _json

            try:
                steps = _json.loads(steps_json) if steps_json else []
            except _json.JSONDecodeError as e:
                return f"error: steps_json invalid ({e})"
            try:
                spec = self.agent_team.create_pipeline(name, description, steps)
            except ValueError as e:
                return f"error: {e}"
            flow = " -> ".join(s.agent for s in spec.steps)
            return f"pipeline '{spec.name}' created: {flow}"

        self.toolbox.register(
            _Tool(
                name="run_team",
                description="Run a multi-agent collaboration pipeline: agents pass outputs to each other. code_pipeline = write then auto-review then verdict; code_pipeline_debug = write, review, fix; research_report = research then executive brief; design_review = concept then critique; doc_pipeline = analyze then document.",
                parameters={
                    "type": "object",
                    "properties": {"pipeline": {"type": "string"}, "task": {"type": "string"}},
                    "required": ["pipeline", "task"],
                },
                handler=_run_team_pipeline,
            )
        )
        self.toolbox.register(
            _Tool(
                name="create_team_pipeline",
                description='Create an agent collaboration pipeline. steps_json: [{"agent": "coder", "instruction": "Implement: $input"}, {"agent": "code_reviewer", "instruction": "Review: $input"}]. $input carries the previous agent\'s output.',
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string", "default": ""},
                        "steps_json": {"type": "string", "default": "[]"},
                    },
                    "required": ["name", "steps_json"],
                },
                handler=_create_team_pipeline,
            )
        )
        # hourly maintenance, managed by JARVIS itself
        self._add_job(
            "brain maintenance", "interval", {"seconds": 3600}, "run_brain_maintenance", {}
        )
        # P6: expose persistence to skills (search_conversations) without cycles
        from . import tools as _tools_mod

        _tools_mod.set_persistence(self.persistence)
        self.tool_use = ToolUseV2(self.toolbox)
        self.self_improve = SelfImprovementLoop(self.router, self.client, cfg)
        self.multimodal = MultiModalBrain(self.router, self.client, cfg)
        self.prompt_opt = PromptOptimizer(self.router, self.client, cfg)

        # P12: brain-facing self-improvement tools (needs self_improve above)
        from skills.self_improve_skills import register_self_improve

        try:
            register_self_improve(self.toolbox, self)
        except Exception as exc:
            print(f"[Self-Improve] skill registration skipped: {exc}")

        # P12: consume distilled training data — seed the few-shot pool with
        # the best past self-improved pairs so prompt_opt mutations and
        # select_few_shot actually have something to work with.
        try:
            seeded = 0
            existing_inputs = {e.input for e in self.prompt_opt.examples.values()}
            for ex in self.self_improve.distiller.get_training_data(limit=200):
                instr = (ex.get("instruction") or ex.get("input") or "")[:500]
                out = (ex.get("output") or "")[:1000]
                if instr and out and instr not in existing_inputs:
                    self.prompt_opt.add_example(
                        input_text=instr,
                        output_text=out,
                        tags=[ex.get("topic", "general"), ex.get("source", "distilled")],
                    )
                    seeded += 1
            if seeded:
                print(f"[Self-Improve] seeded {seeded} few-shot examples from distilled data")
        except Exception as exc:
            print(f"[Self-Improve] example seeding skipped: {exc}")

        self.code_agent.orchestrator = self  # Reference for skills

        # Phase 1: Intelligence & Conversation Core
        self.summarizer = Summarizer(
            self.router,
            self.client,
            SummaryConfig(
                enabled=cfg.summarizer.enabled,
                model=cfg.summarizer.model,
                max_summary_tokens=cfg.summarizer.max_summary_tokens,
                trigger_ratio=cfg.summarizer.trigger_ratio,
                min_turns_to_summarize=cfg.summarizer.min_turns_to_summarize,
            ),
        )
        self.persona = PersonaManager()
        self.dialogue = DialogueManager(
            max_threads=cfg.dialogue.max_threads,
            max_history=cfg.dialogue.max_history,
        )
        self.intent_classifier = IntentClassifier()
        # Cross-device continuity (step 3): shared turn journal. Every
        # surface (cli/web/telegram) records turns here and reads the
        # others' recent turns as context.
        try:
            from core.surface import SurfaceJournal

            self.surface_journal = SurfaceJournal(cfg.db_path.parent / "surface_journal.db")
        except Exception:
            self.surface_journal = None
        self.preferences = PreferenceManager()
        # Auto-speak for unprompted alerts (None = follow JARVIS_VOICE_ALERTS env)
        self.voice_alerts: bool | None = None
        self._alert_speaker = None

        # Memory consolidation tool (needs memory + preferences above)
        from skills.memory import (
            register_anniversary,
            register_consolidation,
            register_prefs,
            register_project_memory,
        )

        try:
            register_consolidation(
                self.toolbox, self.memory, self.preferences, getattr(self, "memory_v2", None)
            )
        except Exception as exc:
            print(f"[Memory] consolidation registration skipped: {exc}")
        try:
            register_prefs(self.toolbox, self.preferences)
            register_anniversary(self.toolbox, self.memory, self.persistence)
            register_project_memory(self.toolbox, cfg.db_path.parent)
        except Exception as exc:
            print(f"[Memory] compound registration skipped: {exc}")

        # DB housekeeping tool: WAL checkpoint + conditional VACUUM over
        # data/*.db. Scheduled daily in start(); also safe to invoke manually.
        try:
            from .db import maintenance as _db_maintenance
            from .tools import Tool as _Tool

            async def db_maintain() -> str:
                report = await asyncio.to_thread(_db_maintenance, cfg.db_path.parent)
                ok = sum(1 for r in report.values() if r["ok"])
                vac = sum(1 for r in report.values() if r["vacuumed"])
                reclaimed = sum(r["reclaimed_bytes"] for r in report.values())
                out = (
                    f"db maintenance: {ok}/{len(report)} checkpointed, "
                    f"{vac} vacuumed, {reclaimed / 1e6:.1f}MB reclaimed"
                )
                errs = {k: v["error"] for k, v in report.items() if v["error"]}
                if errs:
                    out += f"; errors: {errs}"
                return out

            self.toolbox.register(
                _Tool(
                    name="db_maintain",
                    description="Internal housekeeping: WAL checkpoint + conditional VACUUM for data/*.db. Runs daily at 04:10; safe to call manually.",
                    parameters={"type": "object", "properties": {}},
                    handler=db_maintain,
                )
            )
        except Exception as exc:
            print(f"[DB] maintenance tool registration skipped: {exc}")

        # Voice doctor (all checks guarded; works headless)
        from skills.voice_doctor import register_voice_doctor

        try:
            register_voice_doctor(self.toolbox)
        except Exception as exc:
            print(f"[Voice] doctor registration skipped: {exc}")

        # Mail arrival watcher (needs notify_manager above)
        from skills.mail_skills import register_mailtriage, register_mailwatch

        try:
            register_mailwatch(self.toolbox, self.notify_manager, self.router, self.speak_alert)
        except Exception as exc:
            print(f"[Mail] watcher registration skipped: {exc}")
        try:
            register_mailtriage(self.toolbox, self.router)
        except Exception as exc:
            print(f"[Mail] triage registration skipped: {exc}")

        # WhatsApp inbox + watcher (bridge optional; tools degrade gracefully)
        from skills.wa_skills import register_wa, register_wawatch

        try:
            register_wa(self.toolbox, self.router)
        except Exception as exc:
            print(f"[WA] registration skipped: {exc}")
        try:
            register_wawatch(self.toolbox, self.notify_manager, self.router, self.speak_alert)
        except Exception as exc:
            print(f"[WA] watcher registration skipped: {exc}")

        # Proactive push (overnight digest + meeting watcher)
        from skills.proactive_push import register_proactive_push

        try:
            register_proactive_push(self.toolbox, self)
        except Exception as exc:
            print(f"[Proactive] push registration skipped: {exc}")

        # Storm-proof answers: queue failed turns, drain when quota returns
        from skills.storm_skills import register_storm

        try:
            register_storm(self.toolbox, self)
        except Exception as exc:
            print(f"[Storm] registration skipped: {exc}")

        # One-tap routines: starting-work + winding-down bundles (on-demand)
        from skills.routine_skills import register_routines

        try:
            register_routines(self.toolbox, self)
        except Exception as exc:
            print(f"[Routines] registration skipped: {exc}")

        # PC deep control: demo prep, downloads, screenshot-note (free policy)
        from skills.pc_flows import register_pc_flows

        try:
            register_pc_flows(self.toolbox, self)
        except Exception as exc:
            print(f"[PCFlows] registration skipped: {exc}")

        # Multi-agent pool: specialist agents running in parallel
        from .agent_pool import AgentPool

        self.agent_pool = AgentPool(self)

        # First boot: give every agent (incl. custom ones) its best-known brain
        try:
            if not self.brain_manager.all():
                self.brain_manager.auto_assign_all()
        except Exception as exc:
            print(f"[BrainManager] initial auto-assign skipped: {exc}")

        # Agent collaboration: pipelines + auto-review (coder -> reviewer)
        from .agent_team import AgentTeam

        self.agent_team = AgentTeam(self.agent_pool, self)

        # Auto-background mode: heavy work goes to agents, chat stays foreground
        from .bg_router import classify_background

        self._bg_classify = classify_background
        self.auto_background: bool = True  # toggle via /bgmode
        self._bg_notes: list[dict] = []  # completed agent results awaiting injection
        # Single-delivery (coder+normal fix): every job result reaches the
        # user EXACTLY once. _delivered_bg holds job ids already shown inline
        # (wait_bg turn / CLI completion print); _filed_bg holds ids whose
        # side-effects (event, memory, voice interject) already ran.
        self._delivered_bg: set[str] = set()
        self._filed_bg: set[str] = set()

        self.history: list[dict] = [
            {"role": "system", "content": self.persona.build_system_prompt(cfg.system_prompt)}
        ]
        self.context_budget_tokens = self.BASE_CONTEXT_BUDGET_TOKENS
        # Defect #1: per-turn token accounting (sources, total, shed) for
        # stats()/HUD. Empty until the first turn runs pre-flight.
        self._last_context_usage: dict = {}

    # ---------- scheduled-job bootstrap ----------

    def _add_job(
        self,
        name: str,
        trigger_type: str,
        trigger_spec: dict,
        action: str,
        action_args: dict | None = None,
    ) -> bool:
        """Register one scheduled job, loudly.

        `scheduler.add_task` replaces by name, so calling this on every boot never
        duplicates jobs. A registration failure used to be swallowed — the job
        simply never ran again and nobody knew; now it prints, records the
        failure in `_job_errors` (surfaced via `stats()` + GET /api/tasks/errors),
        publishes a `scheduler_job_failed` event for the dashboard/HUD, and
        reports False.
        """
        try:
            self.scheduler.add_task(
                name=name,
                trigger_type=trigger_type,
                trigger_spec=trigger_spec,
                action=action,
                action_args=action_args or {},
            )
            return True
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            print(f"[orchestrator] failed to schedule '{name}' -> {action}: {err}")
            record = {"name": name, "action": action, "error": err}
            try:
                errors = getattr(self, "_job_errors", None)
                if isinstance(errors, list):
                    errors.append(record)
            except Exception:
                pass
            try:
                bus = getattr(self, "event_bus", None)
                if bus is not None and hasattr(bus, "publish"):
                    bus.publish("scheduler_job_failed", record)
            except Exception:
                pass
            return False

    async def start(self) -> None:
        """Start all background loops. Call once from every entrypoint."""
        try:
            await self.scheduler.start()
            # Phase 1: idempotent daily jobs (name-replace = no dupes on reboot)
            try:
                import os as _ose

                if _ose.environ.get("JARVIS_SERVICE_INSTANCE") != "1":
                    self._add_job(
                        "morning-briefing",
                        "daily",
                        {"hour": 8, "minute": 0},
                        "notify_briefing",
                        {"kind": "morning"},
                    )
                    self._add_job(
                        "evening-review",
                        "daily",
                        {"hour": 21, "minute": 0},
                        "notify_briefing",
                        {"kind": "evening"},
                    )
                    # Phase 3: inbox triage dry-run daily 19:00 (report only, never moves files)
                    self._add_job(
                        "inbox-triage", "daily", {"hour": 19, "minute": 0}, "inbox_report"
                    )
                    # Memory consolidation weekly Sunday 20:00 (promote + report, never deletes)
                    self._add_job(
                        "memory-consolidation",
                        "weekly",
                        {"weekday": 6, "hour": 20, "minute": 0},
                        "memory_consolidate",
                    )
                    # Mail arrival watch every 5 min (baseline-first, alerts on new)
                    self._add_job("mail-watch", "interval", {"seconds": 300}, "mail_check_new")
                    # DB housekeeping: WAL checkpoint + conditional VACUUM (daily 04:10)
                    self._add_job(
                        "db-maintenance",
                        "daily",
                        {"hour": 4, "minute": 10, "catch_up_always": True},
                        "db_maintain",
                    )
                    # Proactive push: overnight digest 07:30 + meeting watcher
                    # every 15 min (dedup/cap/mute enforced inside the tools).
                    self._add_job(
                        "overnight-digest", "daily", {"hour": 7, "minute": 30}, "proactive_digest"
                    )
                    self._add_job(
                        "meeting-watch", "interval", {"seconds": 900}, "proactive_meeting_watch"
                    )
                    # Proactive Phase B: nudge sweep every 30 min (overdue 6h
                    # throttle, bills once-ever, stale weekly — gate enforces).
                    self._add_job(
                        "nudge-sweep", "interval", {"seconds": 1800}, "proactive_nudge_sweep"
                    )
                    # Storm-proof answers: answer queued turns every 5 min
                    # once quota returns (Telegram + voice; idempotent claims).
                    self._add_job("storm-drain", "interval", {"seconds": 300}, "storm_drain")
                    # NOTE: wa-watch is NOT auto-registered (owner asked:
                    # check WhatsApp only on demand). Schedule manually via:
                    # schedule_task(name, interval, {seconds}, wa_check_new, {}).
                    # One-time cleanup of the previously auto-created task; a failure
                    # here only leaves a retired task in place, so it stays quiet.
                    with contextlib.suppress(Exception):
                        for _t in self.scheduler.list_tasks():
                            if _t.name == "wa-watch":
                                self.scheduler.remove_task(_t.id)
                                print("[orchestrator] retired auto wa-watch (on-demand now).")
                                break
                    # Missed-slot catch-up: booting at 10am must still deliver
                    # the 8am briefing (scheduler alone skips stale daily slots).
                    try:
                        await self._briefing_catchup()
                    except Exception as _e2:
                        print(f"[orchestrator] briefing catch-up skipped: {_e2}")
            except Exception as _e:
                print(f"[orchestrator] boot briefing jobs skipped: {_e}")
        except Exception as exc:
            print(f"[orchestrator] scheduler start failed: {exc}")
        try:
            await self.ml_proactive.start()
        except Exception as exc:
            print(f"[orchestrator] ml_proactive start failed: {exc}")
        # A2A mesh: listen + execute remote tasks via our toolbox
        # P1 single-owner: service child (API) never binds :8082 — it dials
        # the CLI brain via UnifiedBrainProxy instead. Prevents 10048.
        try:
            import os as _os

            _is_child = _os.environ.get("JARVIS_SERVICE_INSTANCE") == "1"
            if self.cfg.a2a.enabled and not _is_child:
                self.mesh.set_task_executor(self._execute_mesh_task)
                self.distributed.attach_mesh_observer(self.mesh)
                self.mesh.gossip_interval = int(self.cfg.a2a.gossip_interval or 30)
                # Federated inference: remote peers can use our provider chain
                self.mesh.inference_fn = self._stream_inference
                # Unified brain: remote clients (API server) chat through OUR
                # full process() pipeline — shared history, memory, tools.
                self.mesh.set_chat_handler(self._mesh_chat_pipeline)
                await self.mesh.start()
            elif _is_child:
                print("[orchestrator] service child: mesh bind skipped (dials CLI via proxy).")
        except Exception as exc:
            print(f"[orchestrator] mesh start failed: {exc}")

    async def _briefing_catchup(self) -> None:
        """Deliver briefing slots missed while offline. The scheduler skips stale
        daily slots, so booting at 10am would otherwise lose the 8am briefing
        until tomorrow. Dedup file guarantees one delivery per day/kind."""
        from datetime import datetime

        from skills.briefing import generate_briefing
        from skills.notify_skills import _dedup_hit

        now = datetime.now()
        for kind, hour in (("morning", 8), ("evening", 21)):
            if now.hour < hour:
                continue
            key = f"briefing:{now.date().isoformat()}:{kind}"
            if _dedup_hit(key, dry=True):
                continue
            try:
                text = await generate_briefing(kind)
                title = (
                    "Morning Briefing (catch-up)"
                    if kind == "morning"
                    else "Evening Review (catch-up)"
                )
                result = await self.notify_manager.notify(message=text, title=title)
                if result.get("success"):
                    _dedup_hit(key)  # record only on success so failures retry
                    print(f"[orchestrator] catch-up {kind} briefing delivered.")
                else:
                    print(f"[orchestrator] catch-up {kind} briefing failed: {result.get('error')}")
            except Exception as exc:
                print(f"[orchestrator] catch-up {kind} briefing error: {exc}")

    async def _mesh_chat_pipeline(self, message: str, wait_bg: bool = False, surface: str = "web"):
        """Full-brain chat over the mesh: yields (label, chunk) exactly like
        the local CLI loop consumes. One shared conversation for CLI, web
        and Telegram clients."""
        async for label, chunk in self.process(message, wait_bg=wait_bg, surface=surface or "web"):
            yield label, chunk
        # P12: apply persisted self-improvement lessons from the first turn
        self._apply_lessons_to_prompt()

    async def _stream_inference(self, query: str, task_type: str = "general"):
        """Stream LLM inference for a remote mesh peer."""
        async for item in self.router.chat(
            [{"role": "user", "content": query}],
            tools=None,
            task_type=task_type,
        ):
            yield item

    async def _execute_mesh_task(self, action: str, args_json: str) -> str:
        """Run a remote peer's requested toolbox action locally."""
        return await self.toolbox.execute(action, args_json)

    def _memory_context(self, user_input: str) -> str | None:
        rows = self.memory.search(user_input, k=4)
        if not rows:
            return None
        # Cap each snippet: long memories must not bloat every prompt
        lines = "\n".join(
            f"- {text[:240]}{'…' if len(text) > 240 else ''}" for _, _, text, _ in rows
        )
        return f"[Relevant long-term memories about the user]\n{lines}"

    def _episodic_context(self, user_input: str) -> str | None:
        """P6: recall relevant past conversations (episodic memory).

        Searches previous sessions (excluding the current one) for content
        matching this input, returning short snippets so the brain can say
        "last time we discussed X, we decided Y".
        """
        try:
            results = self.persistence.search(user_input, limit=6)
            lines = []
            seen_sessions = set()
            for r in results:
                if r.get("session_id") == self.session_id:
                    continue  # current conversation, already in history
                sid = r.get("session_id")
                if sid in seen_sessions:
                    continue
                seen_sessions.add(sid)
                title = r.get("session_title", "past conversation")
                snippet = (r.get("content") or "").replace("\n", " ")[:220]
                if len(r.get("content") or "") > 220:
                    snippet += "…"
                lines.append(f"- From '{title}': {snippet}")
                if len(lines) >= 2:
                    break
            if not lines:
                return None
            return (
                "[Relevant past conversations — use naturally if the topic connects]\n"
                + "\n".join(lines)
            )
        except Exception:
            return None

    def _time_context(self) -> str:
        """Current date/time injected every turn so the model can reason about '9 pm', 'tomorrow', etc."""
        from datetime import datetime

        now = datetime.now()
        return (
            f"[Current time context]\n"
            f"Date: {now.strftime('%Y-%m-%d')} ({now.strftime('%A')})\n"
            f"Time: {now.strftime('%H:%M')} ({now.strftime('%I:%M %p').lstrip('0')})\n"
            f"ISO: {now.isoformat(timespec='seconds')}\n"
            f"Timezone: {now.astimezone().tzname()}"
        )

    # ---------- auto-background plumbing ----------

    def _drain_bg_notes(self) -> str | None:
        """Return completed agent results as a system note; clears the queue.

        Single-delivery: notes for jobs already shown inline are skipped
        (their receipt, if any, was filed at delivery time). The note tells
        the model to present job results FIRST and distinctly — a stale job
        answer must never blur into the fresh answer in the same turn.
        """
        if not self._bg_notes:
            return None
        delivered, _ = self._bg_sets()
        parts = []
        for note in self._bg_notes:
            if note.get("task_id") in delivered and not note.get("receipt"):
                continue
            delivered.add(note.get("task_id"))
            parts.append(
                f"[Background agent result · {note['agent']} · job {note['task_id']} · prompt: {note['prompt'][:80]}]\n"
                f"{note['result'][:2500]}\n"
                "[Present this completed-job result FIRST and distinctly — it arrived "
                "between turns. Then answer the user's new message separately.]"
            )
        self._bg_notes.clear()
        return "\n\n".join(parts) or None

    def _bg_sets(self) -> tuple[set, set]:
        """(delivered, filed) single-delivery sets, created on demand.

        On-demand because tests (and partial hosts) build Orchestrator via
        __new__ without __init__ — delivery guarantees must hold there too.
        Never raises.
        """
        try:
            delivered = getattr(self, "_delivered_bg", None)
            if delivered is None:
                delivered = self._delivered_bg = set()
            filed = getattr(self, "_filed_bg", None)
            if filed is None:
                filed = self._filed_bg = set()
            return delivered, filed
        except Exception:
            return set(), set()

    def mark_bg_delivered(self, task_id: str | None) -> None:
        """Record that a job's full result was already shown (inline turn /
        completion print). Later registrations file a short receipt instead
        of re-injecting the whole result. Never raises."""
        try:
            if task_id:
                self._bg_sets()[0].add(str(task_id))
        except Exception:
            pass

    def note_bg_completion(self, task) -> None:
        """CLI completion path: mark shown, then register (receipt form)."""
        with contextlib.suppress(Exception):
            self.mark_bg_delivered(getattr(task, "task_id", None))
        self._register_bg_result(task)

    def _register_bg_result(self, task) -> None:
        """File a finished job for the next chat turn — exactly once.

        Single-delivery: side-effects (dashboard event, memory, voice
        interject) run once per job (_filed_bg). The injected note is the
        FULL result only when the user hasn't seen it yet; when already
        shown inline/printed (_delivered_bg), a short receipt is filed so
        the next turn keeps continuity without re-reading the whole answer.
        Failed jobs file an AGENT FAILED note through the same dedup.
        """
        try:
            task_id = str(getattr(task, "task_id", "") or "")
            agent = getattr(task, "agent", "?") or "?"
            prompt = getattr(task, "prompt", "") or ""
            status = getattr(task, "status", "") or ""
        except Exception:
            return
        if not task_id:
            return
        delivered, filed = self._bg_sets()
        if status == "done":
            full_result = getattr(task, "result", "") or "(empty)"
        else:
            full_result = f"AGENT FAILED: {getattr(task, 'error', 'unknown error')}"
        if task_id in delivered:
            self._bg_notes.append(
                {
                    "agent": agent,
                    "task_id": task_id,
                    "prompt": prompt,
                    "result": (
                        f"[Background job {task_id} ({agent}) finished — the user already "
                        "saw the full result. Mention it briefly only if relevant.]"
                    ),
                    "receipt": True,
                }
            )
        else:
            # Unshown: file the FULL result. Delivery happens at drain
            # (next-turn injection), which marks it — never here. A full
            # note already queued for this job is not queued twice.
            try:
                queued = any(
                    n.get("task_id") == task_id and not n.get("receipt") for n in self._bg_notes
                )
            except Exception:
                queued = False
            if not queued:
                self._bg_notes.append(
                    {
                        "agent": agent,
                        "task_id": task_id,
                        "prompt": prompt,
                        "result": full_result,
                    }
                )
        if task_id in filed:
            return
        filed.add(task_id)
        if status == "done":
            # Notify the website/dashboard live: publish an event (persisted
            # to the shared events.db) so the API layer can broadcast it.
            # Swallowed by design: a dashboard that misses one event is cosmetic.
            with contextlib.suppress(Exception):
                self.event_bus.publish(
                    "bg_job_done",
                    {
                        "agent": agent,
                        "task_id": task_id,
                        "prompt": prompt[:120],
                        "result": (getattr(task, "result", "") or "")[:2000],
                        "duration_s": getattr(task, "duration_s", None),
                    },
                )
            # durable copy in long-term memory too — swallowed by design (the
            # bg_notes injection above is the user-facing path).
            with contextlib.suppress(Exception):
                self.memory.add(
                    f"[{agent} agent completed] {prompt[:120]} -> {(getattr(task, 'result', '') or '')[:300]}",
                    kind="agent_result",
                )
            # Spoken interjection (movie-JARVIS): announce the finished job
            # aloud when voice alerts are on and not in quiet hours.
            # Swallowed by design: a headless box has no speaker.
            with contextlib.suppress(Exception):
                if getattr(self.cfg.voice, "interject_jobs", True):
                    import asyncio as _aio

                    _line = f"Sir, the {agent} agent has finished — result on screen."
                    _aio.get_running_loop().create_task(self.speak_alert(_line))

    def _detect_task_type(self, user_input: str) -> str:
        """Detect task type from user input for model routing using IntentClassifier."""
        # Use dialogue context for better classification
        dialogue_context = {
            "previous_task_type": getattr(self, "_last_task_type", "general"),
            "intent": getattr(self, "_last_intent", "general"),
        }
        intent_result = self.intent_classifier.classify(user_input, dialogue_context)
        self._last_task_type = intent_result.task_type
        self._last_intent = intent_result.task_type
        # Domain-aware routing: boost the intent's preferred providers in the
        # router. Swallowed by design: a failed hint only loses a boost, the
        # router still falls back to its normal scoring order.
        with contextlib.suppress(Exception):
            self.router.set_preferred_names(intent_result.suggested_models)
        return intent_result.task_type

    def _get_or_create_mesh_api_key(self, cfg: Config) -> str:
        """Generate or load mesh API key from .env file."""
        import secrets
        from pathlib import Path

        env_path = Path(".env")
        key_name = "MESH_API_KEY"

        # Load existing .env
        env_vars = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()

        if key_name in env_vars and env_vars[key_name]:
            return env_vars[key_name]

        # Generate new key
        new_key = secrets.token_urlsafe(32)
        env_vars[key_name] = new_key

        # Write back to .env
        lines = []
        for k, v in env_vars.items():
            lines.append(f"{k}={v}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(f"[Security] Generated new mesh API key: {new_key[:8]}...")
        return new_key

    def _select_ensemble_mode(self, user_input: str, task_type: str) -> str:
        """Select ensemble mode based on task complexity."""
        text = user_input.lower()

        # Complex reasoning triggers self-correction
        complex_kw = [
            "think step by step",
            "analyze deeply",
            "critique",
            "verify",
            "prove",
            "optimize",
            "architecture",
            "design pattern",
            "best practice",
            "security audit",
            "refactor",
        ]

        # Consensus for high-stakes
        consensus_kw = [
            "important",
            "critical",
            "production",
            "deploy",
            "money",
            "financial",
            "legal",
            "medical",
            "safety",
        ]

        if any(kw in text for kw in consensus_kw):
            return "consensus"
        if any(kw in text for kw in complex_kw) or task_type == "reasoning":
            return "self_correction"
        return "standard"

    def _select_reasoning_strategy(self, user_input: str, task_type: str) -> str:
        """Select reasoning strategy based on query."""
        text = user_input.lower()

        # Explicit strategy requests
        if any(kw in text for kw in ["tree of thought", "multiple approaches", "explore options"]):
            return "tot"
        if any(
            kw in text
            for kw in ["react", "use tools", "step by step with tools", "act and observe"]
        ):
            return "react"
        if any(
            kw in text
            for kw in ["self-consistency", "multiple samples", "vote", "consensus answer"]
        ):
            return "consistency"
        if any(kw in text for kw in ["reflect", "critique yourself", "improve your answer"]):
            return "reflection"

        # Task-based defaults
        if task_type == "reasoning":
            return "cot"

        return "standard"

    def _should_use_rag(self, user_input: str) -> bool:
        """Determine if RAG retrieval should be used.

        Only question-style knowledge lookups — chat, commands and tool-call
        intents ("remember X", "search the web", "find the file") must NOT
        burn a retrieval round-trip.
        """
        text = user_input.lower()
        rag_triggers = [
            "what is",
            "who is",
            "when did",
            "where is",
            "how to",
            "explain",
            "define",
            "describe",
            "tell me about",
            "according to",
            "document",
            "knowledge base",
        ]
        return any(t in text for t in rag_triggers)

    def _should_self_improve(self, user_input: str, response: str) -> bool:
        """Determine if self-improvement should run.

        P12: explicit user requests (improve/refine/critique…) always run.
        Automatic runs only for genuinely complex responses (>800 chars) AND
        gated by the 10-minute rate limit so we don't burn tokens on every turn.
        """
        text = user_input.lower()

        improve_triggers = [
            "improve",
            "refine",
            "polish",
            "make better",
            "critique",
            "review",
            "check",
            "verify",
            "perfect",
            "optimize",
            "enhance",
        ]
        has_trigger = any(t in text for t in improve_triggers)
        if has_trigger:
            return True

        # Auto path: complex responses only, and rate-limited
        is_complex = len(response) > 800
        return is_complex and self.self_improve.should_auto_run()

    def _extract_concepts(self, text: str) -> list[str]:
        """Extract key concepts from text for distillation."""
        import re

        # Simple extraction: capitalized words, technical terms
        words = re.findall(r"\b[A-Z][a-z]{2,}\b", text)
        # Filter common words
        common = {
            "The",
            "This",
            "That",
            "These",
            "Those",
            "Then",
            "There",
            "When",
            "Where",
            "What",
            "How",
            "Why",
            "Can",
            "Will",
            "Would",
            "Could",
            "Should",
            "Must",
            "May",
            "Might",
        }
        return list(dict.fromkeys(w for w in words if w not in common))[:10]

    @staticmethod
    def _parse_xml_tool_calls(text: str) -> tuple[str, list[tuple[str, str, str]]]:
        """Parse XML-style tool calls that some models output in content.
        Returns (clean_text, list_of_(call_id, name, args_json))."""
        import re

        calls = []
        clean = text

        # Pattern: <tool_call><function=name><parameter=key>value</parameter>...</function></tool_call>
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

        # Pattern: {"name": "tool_name", "arguments": {...}} in JSON
        json_tc_pattern = re.compile(r'\{"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]+\})')
        offset = len(calls)
        for i, match in enumerate(json_tc_pattern.finditer(text)):
            name = match.group(1)
            args = match.group(2)
            calls.append((f"json_call_{offset + i}", name, args))

        if calls:
            clean = tc_pattern.sub("", text)
            clean = json_tc_pattern.sub("", clean)
            clean = clean.strip()

        return clean, calls

    async def process(
        self, user_input: str, wait_bg: bool = False, surface: str = "cli"
    ) -> AsyncIterator[tuple[str, str | ToolCallDelta]]:
        import json

        # Lazy start distributed brain
        if not hasattr(self, "_distributed_started"):
            asyncio.create_task(self.distributed.start())
            self._distributed_started = True

        # Context-budget recovery (defect #1): _hard_trim halves the budget on
        # overflow and it used to stay halved for the whole session (trim death
        # spiral). Now each new turn following a clean turn steps back toward
        # BASE. A turn that overflows sets the flag again via _hard_trim.
        self._recover_budget()
        self._overflowed_last_turn = False

        # Phase 1: Process dialogue context and learn preferences. The return
        # value is a context-hint dict; prompt injection reads the manager's
        # state later (`get_context_for_prompt`), so this call is for its
        # side effect (topic threading / entity tracking) only.
        self.dialogue.process_user_message(user_input)
        # Step 3: journal this turn so other devices can see it.
        _journal = getattr(self, "surface_journal", None)
        if _journal is not None:
            _journal.add(surface, "user", user_input)
        self.preferences.learn_from_turn(user_input)

        # Auto-background: heavy work (coding/project/research) goes to a
        # specialist agent; the main brain stays free for chatting.
        # wait_bg (web/API path): the turn stays open, so await the job
        # (bounded) and answer inline — the browser shows + speaks the
        # result instead of a bare handoff chip. CLI keeps fire-and-forget
        # (it has /result + terminal notify for this).
        try:
            if self.auto_background and self.agent_pool is not None:
                decision = self._bg_classify(user_input, enabled=True)
                if decision.background:
                    spec = self.agent_pool.agents.get(decision.agent)
                    task_id = self.agent_pool.submit(decision.agent, user_input)
                    icon = spec.icon if spec else "🤖"
                    dispatch_note = (
                        f"{icon} {decision.agent} agent took this over in the background "
                        f"(job {task_id}) — {decision.reason}. Keep chatting; I'll notify you when it's done."
                    )
                    if wait_bg:
                        yield "bg_dispatch", dispatch_note
                        try:
                            task = await self.agent_pool.wait_for(task_id, timeout=BG_WAIT_S)
                        except Exception:
                            task = None
                        if task is not None and task.status == "done":
                            dur = f"{task.duration_s:.0f}s"
                            body = (task.result or "(empty)").strip()
                            if len(body) > 4000:
                                body = (
                                    body[:4000]
                                    + f"\n\n…(truncated — full output: /result {task_id} on the CLI)"
                                )
                            # Shown inline: single-delivery marks it so the
                            # next turn files a receipt, not the full text.
                            self.mark_bg_delivered(task_id)
                            yield (
                                task.provider or decision.agent,
                                (f"{icon} {decision.agent} finished ({dur}):\n\n{body}"),
                            )
                        elif task is not None:
                            self.mark_bg_delivered(task_id)
                            yield (
                                task.provider or decision.agent,
                                (
                                    f"{icon} {decision.agent} job {task_id} FAILED: "
                                    f"{(task.error or 'unknown error')[:300]}"
                                ),
                            )
                        # timeout (task None): dispatch chip already shown;
                        # the result lands via bg_notes on the next turn.
                        # keep dialogue history clean: remove the dispatched message
                        self.dialogue.recent_user_messages.pop() if self.dialogue.recent_user_messages else None
                        return
                    yield "bg_dispatch", dispatch_note
                    # keep dialogue history clean: remove the dispatched message
                    self.dialogue.recent_user_messages.pop() if self.dialogue.recent_user_messages else None
                    return
        except Exception as exc:
            # never let auto-bg break normal chat — but say so: a dispatch that
            # keeps failing means heavy work silently never reaches an agent.
            print(f"[orchestrator] auto-background dispatch skipped: {type(exc).__name__}: {exc}")

        # Inject any completed background-agent results into this turn's context
        bg_note = self._drain_bg_notes()

        self.history.append({"role": "user", "content": user_input})
        # Use summarizer instead of hard trim (falls back deterministically).
        _summarized = await self._compress_history()

        self._trim()

        # Auto-create session if needed
        if self.session_id is None:
            self.session_id = self.persistence.create_session()
        self.persistence.add_message(self.session_id, "user", user_input)

        # Gather context sources concurrently — they are independent.
        # (RAG retrieval is async and runs while the two cheap sync SQLite
        # lookups execute; both return in milliseconds.)

        rag_ctx = None
        if self._should_use_rag(user_input):
            try:
                rag_chunks = await self.rag.retrieve_context(user_input)
                if rag_chunks:
                    passages = "\n".join(
                        f"[{i + 1}] (source: {c.source}): {c.content[:300]}"
                        for i, c in enumerate(rag_chunks)
                    )
                    rag_ctx = f"[Retrieved Knowledge]\n{passages}\n[End Retrieved Knowledge]"
            except Exception:
                pass  # Graceful degradation

        mem_ctx = self._memory_context(user_input)
        epi_ctx = self._episodic_context(user_input)

        # Add dialogue context
        dialogue_context_str = self.dialogue.get_context_for_prompt()

        # Add preferences context
        task_type = self._detect_task_type(user_input)
        prefs_context = self.preferences.format_for_prompt(task_type)

        # Tool packs: core always sent (~30 schemas), domain packs added by
        # keyword scan. Was: 103 schemas then truncate to 60 — small models
        # stopped function-calling entirely. Now: small, relevant payloads.
        from core.tools import CORE_TOOLS, detect_packs, pack_tools

        _packs = detect_packs(user_input)
        _names: list[str] = []
        for _n in CORE_TOOLS + [n for _p in _packs for n in pack_tools(self.toolbox, _p)]:
            if _n not in _names:
                _names.append(_n)
        MAX_PACK_TOOLS = 40
        tool_schemas = self.toolbox.schemas_for(_names[:MAX_PACK_TOOLS])
        _loaded_packs = set(_packs)

        # Proactive: analyze and maybe inject suggestion
        suggestions = self.proactive.analyze_and_suggest(user_input, self.history)
        if suggestions:
            for s in suggestions[:2]:
                res = await self._maybe_act_on_suggestion(s)
                yield (
                    "proactive",
                    (
                        f"💡 {s.title}: acted → {res}"
                        if res is not None
                        else f"💡 {s.title}: {s.description}"
                    ),
                )

        # ML Proactive: analyze and inject ML-based suggestions
        ml_suggestions = self.ml_proactive.get_suggestions({"text": user_input})
        if ml_suggestions:
            for s in ml_suggestions[:2]:
                res = await self._maybe_act_on_suggestion(s)
                yield (
                    "ml_proactive",
                    (
                        f"🤖 {s.title}: acted → {res}"
                        if res is not None
                        else f"🤖 {s.title}: {s.description}"
                    ),
                )

        # Scheduler failures surface as actionable suggestions (alert once per
        # task per process; auto-executes only when proactive.auto_act is on).
        for s in self._failed_task_suggestions():
            res = await self._maybe_act_on_suggestion(s)
            yield (
                "proactive",
                (
                    f"⚠️ {s.title}: acted → {res}"
                    if res is not None
                    else f"⚠️ {s.title}: {s.description}"
                ),
            )

        # P6 skill acquisition: surface repeated-behavior automation proposals
        try:
            candidates = self.skill_acquisition.find_candidates()
            for cand in candidates[:1]:
                yield (
                    "skill",
                    f"✨ Automation idea: {self.skill_acquisition.describe(cand)} (/flow compose to create it)",
                )
                self.skill_acquisition.mark_proposed(tuple(cand.tools))
                self.event_bus.publish("skill_proposal", {"tools": cand.tools, "count": cand.count})
        except Exception:
            pass

        # Check command-based automation (matched actions EXECUTE, not just log)
        for fired in await self._fire_automation("command", {"text": user_input}):
            yield (
                "auto",
                f"🔄 automation: {fired['rule_id']} -> {fired['action']}: {(fired['result'] or '')[:200]}",
            )

        # Detect task type for model routing
        task_type = self._detect_task_type(user_input)

        # Defect #2 companion: size the answer to the question. Short asks get
        # a brevity instruction, complex asks get room; normal asks get nothing
        # (byte-identical to before). Explicit markers and learned communication
        # prefs feed the verdict — see core/verbosity.py.
        from core.verbosity import score_question

        try:
            _verbosity = score_question(user_input, self.preferences)
        except Exception:
            _verbosity = None
        _verbosity_instruction = ""
        _verbosity_level = "normal"
        if _verbosity is not None:
            _verbosity_instruction = _verbosity.instruction
            _verbosity_level = _verbosity.level

        # Determine ensemble mode based on task complexity
        ensemble_mode = self._select_ensemble_mode(user_input, task_type)

        # Select reasoning strategy
        reasoning_strategy = self._select_reasoning_strategy(user_input, task_type)

        for _ in range(self.MAX_TOOL_ROUNDS):
            # Step 3: cross-device continuity — recent turns from the user's
            # OTHER surfaces (recomputed each round; cheap SQLite read).
            _xctx = None
            if _journal is not None:
                try:
                    _xctx = _journal.context_block(surface)
                except Exception:
                    _xctx = None
            # Defect #1 pre-flight: assemble tagged parts, estimate the FULL
            # payload (history + injections + tool schemas), shed lowest-value
            # sources first. Skipped parts keep the original insert order for
            # survivors, so no-shed turns are byte-identical to before.
            _parts = {
                "time": self._time_context(),
                "verbosity": _verbosity_instruction or None,
                "bg_note": bg_note,
                "memory": mem_ctx,
                "episodic": epi_ctx,
                "rag": rag_ctx,
                "dialogue": (
                    f"[Dialogue Context]\n{dialogue_context_str}" if dialogue_context_str else None
                ),
                "prefs": prefs_context,
                "xctx": _xctx,
            }
            _shed, _usage = self._preflight_context(_parts, tool_schemas)
            _usage["summarized"] = bool(_summarized)
            _usage["verbosity"] = _verbosity_level
            with contextlib.suppress(Exception):
                self._last_context_usage = dict(_usage)
            _hist = list(self.history)
            _hist_trimmed = False
            if _usage["total"] > _usage["budget"]:
                # Everything droppable dropped and still over: shrink the
                # round's COPY (live history untouched — _trim owns that).
                _room = _usage["budget"] - (_usage["total"] - _usage["history"])
                self._trim_list(_hist, max(_room, 0))
                _hist_trimmed = len(_hist) < len(self.history)
            if _shed or _summarized or _hist_trimmed:
                print(
                    f"[context] {round(_usage['total'] / 1000, 1)}k/"
                    f"{round(_usage['budget'] / 1000, 1)}k tokens"
                    + (f" shed: {','.join(sorted(_shed))}" if _shed else "")
                    + (" summarized" if _summarized else "")
                    + (" history-trimmed" if _hist_trimmed else "")
                )
            turn_msgs = _hist
            turn_msgs.insert(1, {"role": "system", "content": self._time_context()})
            if _verbosity_instruction:
                turn_msgs.insert(2, {"role": "system", "content": _verbosity_instruction})
            if bg_note and "bg_note" not in _shed:
                turn_msgs.insert(2, {"role": "system", "content": bg_note})
                bg_note = None  # inject only once per turn
            if mem_ctx and "memory" not in _shed:
                turn_msgs.insert(2, {"role": "system", "content": mem_ctx})
            if epi_ctx and "episodic" not in _shed:
                turn_msgs.insert(3, {"role": "system", "content": epi_ctx})
            if rag_ctx and "rag" not in _shed:
                turn_msgs.insert(3, {"role": "system", "content": rag_ctx})
            if dialogue_context_str and "dialogue" not in _shed:
                turn_msgs.insert(
                    4, {"role": "system", "content": f"[Dialogue Context]\n{dialogue_context_str}"}
                )
            if prefs_context and "prefs" not in _shed:
                turn_msgs.insert(5, {"role": "system", "content": prefs_context})
            if surface == "voice":
                # Single-shot spoken tag, sized like the distiller would size
                # it (question budget + voice taste). Never breaks generation:
                # any failure falls back to the static instruction.
                try:
                    from core.config import ROOT as _CFG_ROOT
                    from voice.distiller import spoken_instruction as _spoken_instruction
                    from voice.prefs import VoicePrefs as _VoicePrefs

                    _vp = _VoicePrefs.load(_CFG_ROOT / "data" / "voice_prefs.json")
                    _voice_instruction = _spoken_instruction(
                        user_text=user_input, address=_vp.address, brevity=_vp.brevity
                    )
                except Exception:
                    _voice_instruction = VOICE_SPOKEN_INSTRUCTION
                turn_msgs.insert(5, {"role": "system", "content": _voice_instruction})
            # Step 3: cross-device continuity — inject recent turns from the
            # user's OTHER surfaces so the conversation continues seamlessly.
            if _xctx and "xctx" not in _shed:
                turn_msgs.insert(5, {"role": "system", "content": _xctx})
            content_parts: list[str] = []
            pending: dict[int, dict] = {}

            if ensemble_mode == "self_correction":
                stream = self.ensemble.chat_with_self_correction(
                    turn_msgs, tools=tool_schemas, task_type=task_type
                )
            elif ensemble_mode == "consensus":
                stream = self.ensemble.chat_with_consensus(
                    turn_msgs, tools=tool_schemas, task_type=task_type
                )
            elif reasoning_strategy != "standard":
                stream = self.reasoning.reason(
                    turn_msgs, tools=tool_schemas, task_type=task_type, strategy=reasoning_strategy
                )
            else:
                stream = self.router.chat(turn_msgs, tools=tool_schemas, task_type=task_type)

            async for name, item in stream:
                if isinstance(item, str):
                    content_parts.append(item)
                    yield name, item
                elif isinstance(item, ToolCallDelta):
                    acc = pending.setdefault(item.index, {"id": None, "name": None, "args": []})
                    if item.id:
                        acc["id"] = item.id
                    if item.name:
                        acc["name"] = item.name
                    acc["args"].append(item.args)

            text = "".join(content_parts)

            # Parse XML-style tool calls from content (nemotron, etc.)
            text, xml_calls = self._parse_xml_tool_calls(text)
            calls = []
            for idx in sorted(pending):
                p = pending[idx]
                if not p["name"]:
                    continue
                cid = p["id"] or f"call_{idx}"
                calls.append((cid, p["name"], "".join(p["args"])))

            # Merge XML-parsed calls with OpenAI-format calls
            for cid, name, args in xml_calls:
                calls.append((cid, name, args))

            if not calls:
                if text.strip():
                    self.history.append({"role": "assistant", "content": text})
                    self.persistence.add_message(
                        self.session_id, "assistant", text, provider=self.router.last_provider or ""
                    )
                    # Store in MemoryV2
                    await self._store_conversation_memory(user_input, text)

                    # Self-improvement loop (async, non-blocking)
                    if self._should_self_improve(user_input, text):
                        asyncio.create_task(self._run_self_improvement(user_input, text))
                return

            assistant_msg: dict = {"role": "assistant", "content": text}
            if calls:
                # Poison-guard: a hop that died mid-stream leaves truncated
                # tool-call args in history. Strict validators (NIM, Groq qwen
                # template) 400 every later round on them. Drop invalid ones.
                clean = []
                for cid, name, args in calls:
                    if not name:
                        continue
                    try:
                        json.loads(args) if isinstance(args, str) else None
                        clean.append((cid, name, args if isinstance(args, str) else "{}"))
                    except Exception:
                        continue
                if clean:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                        for cid, name, args in clean
                    ]
                    calls = clean
                else:
                    # All tool calls were truncated garbage: execute nothing,
                    # keep text-only history so the next round stays valid.
                    calls = []
            self.history.append(assistant_msg)
            # Update dialogue manager with assistant response
            self.dialogue.process_assistant_message(text, calls)
            # Step 3: journal the assistant reply for other surfaces.
            if _journal is not None:
                _journal.add(surface, "assistant", text)

            for cid, name, args in calls:
                # load_pack: expand schemas mid-turn so next round can use them
                if name == "load_pack":
                    try:
                        import json as _jl

                        _pack = (
                            _jl.loads(args).get("pack", "") if isinstance(args, str) else ""
                        ) or ""
                        _pack = _pack.strip().lower()
                    except Exception:
                        _pack = ""
                    _new = (
                        [n for n in pack_tools(self.toolbox, _pack) if n not in _names]
                        if _pack not in _loaded_packs
                        else []
                    )
                    if _new:
                        _loaded_packs.add(_pack)
                        _names.extend(_new)
                        tool_schemas = tool_schemas + self.toolbox.schemas_for(_new)
                    yield "tool", f"⚙ load_pack({_pack or '?'}) → {len(_new)} tools"
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": f"pack '{_pack}' loaded: {', '.join(_new) or 'already loaded'}. Use them now.",
                        }
                    )
                    continue
                yield "tool", f"⚙ {name}({args[:60]})"
                result = await self.toolbox.execute(name, args)
                self.history.append({"role": "tool", "tool_call_id": cid, "content": result})
                # P6 skill acquisition: watch tool-usage patterns.
                # Swallowed by design: pattern watching is a background nicety.
                with contextlib.suppress(Exception):
                    self.skill_acquisition.record_tool_use(name)

            # Check automation rules after tool execution (actions EXECUTE)
            for _cid, name, args in calls:
                try:
                    parsed_args = (
                        json.loads(args)
                        if isinstance(args, str)
                        else (args if isinstance(args, dict) else {})
                    )
                except Exception:
                    parsed_args = {"_raw": str(args)[:500]}
                for fired in await self._fire_automation(
                    "tool_result", {"tool": name, "args": parsed_args, "result": "executed"}
                ):
                    yield (
                        "auto",
                        f"🔄 automation: {fired['rule_id']} -> {fired['action']}: {(fired['result'] or '')[:200]}",
                    )

    def reset(self) -> None:
        self.history = [
            {"role": "system", "content": self.persona.build_system_prompt(self.cfg.system_prompt)}
        ]
        self.context_budget_tokens = self.BASE_CONTEXT_BUDGET_TOKENS
        self.session_id = None
        self.summarizer.invalidate_cache()
        self.dialogue = DialogueManager(
            max_threads=self.cfg.dialogue.max_threads,
            max_history=self.cfg.dialogue.max_history,
        )

    def resume_session(self, session_id: str) -> list[dict]:
        """Load a past session's messages into context and return them."""
        msgs = self.persistence.get_messages(session_id)
        self.session_id = session_id
        self.history = [
            {"role": "system", "content": self.persona.build_system_prompt(self.cfg.system_prompt)}
        ]
        for m in msgs:
            self.history.append({"role": m["role"], "content": m["content"]})
        self.summarizer.invalidate_cache()
        return msgs

    def start_new_session(self) -> str:
        self.session_id = self.persistence.create_session()
        self.history = [
            {"role": "system", "content": self.persona.build_system_prompt(self.cfg.system_prompt)}
        ]
        self.summarizer.invalidate_cache()
        self.dialogue = DialogueManager(
            max_threads=self.cfg.dialogue.max_threads,
            max_history=self.cfg.dialogue.max_history,
        )
        return self.session_id

    def load_latest_session(self) -> list[dict] | None:
        sid = self.persistence.latest_session_id()
        if sid:
            return self.resume_session(sid)
        return None

    def _history_total(self) -> int:
        return sum(estimate_tokens(str(m.get("content") or "")) for m in self.history)

    # Pre-flight shed order (defect #1): cheapest value destroyed first.
    # System prompt, time context and the live turn are never shed.
    SHED_ORDER = ("xctx", "dialogue", "prefs", "rag", "episodic", "memory", "bg_note")

    def _preflight_context(
        self, parts: dict[str, str | None], tool_schemas: list
    ) -> tuple[set[str], dict]:
        """Estimate the full model payload and shed lowest-value sources.

        The old code only trimmed `history` while injections (memory, RAG,
        dialogue, prefs, packs) piled on top unchecked — the actual overflow
        source. Returns (shed_tags, usage) where usage maps every source to
        estimated tokens plus total/budget/shed. Records to
        `_last_context_usage` for stats()/HUD. Never raises.
        """
        usage: dict = {}
        try:
            hist_toks = sum(estimate_tokens(str(m.get("content") or "")) for m in self.history)
        except Exception:
            hist_toks = 0
        usage["history"] = hist_toks
        try:
            usage["tools"] = estimate_tokens(json.dumps(tool_schemas)) if tool_schemas else 0
        except Exception:
            usage["tools"] = 0
        for tag, content in parts.items():
            try:
                usage[tag] = estimate_tokens(content) if content else 0
            except Exception:
                usage[tag] = 0
        budget = self.context_budget_tokens
        total = sum(v for v in usage.values() if isinstance(v, int))
        shed: set[str] = set()
        for tag in self.SHED_ORDER:
            if total <= budget:
                break
            if parts.get(tag):
                shed.add(tag)
                total -= usage.get(tag, 0)
        usage["total"] = total
        usage["budget"] = budget
        usage["shed"] = sorted(shed)
        with contextlib.suppress(Exception):
            self._last_context_usage = dict(usage)
        return shed, usage

    def _trim(self) -> None:
        self._trim_list(self.history, self.context_budget_tokens)

    async def _compress_history(self) -> bool:
        """Summarize oldest history into a rolling summary (True if compressed).

        Falls back to a deterministic cut when the summarizer LLM call fails
        (defect #1: free-tier failure used to leave history oversized, feeding
        the trim death spiral). Shape is identical either way: system +
        summary-or-nothing + last 5.
        """
        if not self.summarizer.should_summarize(self.history, self.context_budget_tokens):
            return False
        # Summarize oldest messages (keep system prompt and last 5 messages)
        messages_to_summarize = self.history[1:-5]
        if len(messages_to_summarize) < self.summarizer.config.min_turns_to_summarize:
            return False
        try:
            summary = await self.summarizer.create_rolling_summary(self, messages_to_summarize)
        except Exception as exc:
            print(f"[context] summarizer failed ({exc}) — deterministic trim instead")
            summary = ""
        if summary:
            self.history = (
                [self.history[0]] + [{"role": "system", "content": summary}] + self.history[-5:]
            )
        else:
            self.history = [self.history[0]] + self.history[-5:]
        return True

    def _trim_list(self, msgs: list[dict], budget: int, minimum: int = 3) -> list[dict]:
        """Trim a message list to a token budget, preserving tool pairing.

        Shared by _trim (live history) and the pre-flight check (per-round
        copy): drops oldest-non-system first, skips orphan tool messages and
        assistant tool-calls whose result is gone, never drops below
        `minimum` (system + recent tail, incl. the current turn).
        """

        def _total() -> int:
            try:
                return sum(estimate_tokens(str(m.get("content") or "")) for m in msgs)
            except Exception:
                return 0

        while len(msgs) > minimum and _total() > budget:
            del msgs[1]
            while len(msgs) > minimum and msgs[1].get("role") == "tool":
                del msgs[1]
            if (
                len(msgs) > minimum
                and msgs[1].get("role") == "assistant"
                and msgs[1].get("tool_calls")
                and msgs[2].get("role") != "tool"
            ):
                del msgs[1]
        return msgs

    async def _run_self_improvement(self, user_input: str, response: str):
        """Run self-improvement loop in background.

        P12 closed loop: after each cycle the recurring critique issues are
        folded into the live system prompt so future responses avoid them.
        """
        try:
            was_auto_eligible = self.self_improve.should_auto_run()
            cycle = await self.self_improve.improve(user_input, response, task_type="reasoning")
            if was_auto_eligible:
                self.self_improve.mark_auto_run()

            # Distill key concepts
            concepts = self._extract_concepts(response)
            if concepts:
                await self.self_improve.distill_knowledge(
                    user_input, cycle.refined_output, concepts
                )

            # Feed the refined output back as a high-quality few-shot example.
            # Swallowed by design: losing one training example degrades the
            # few-shot pool slowly, nothing the user is waiting on.
            with contextlib.suppress(Exception):
                self.prompt_opt.add_example(
                    input_text=user_input[:500],
                    output_text=cycle.refined_output[:1000],
                    tags=["self_improved", "high_quality"],
                )

            # Closed loop: absorb recurring lessons into the system prompt
            self._apply_lessons_to_prompt()

            # Log improvement
            print(
                f"[Self-Improve] Cycle {cycle.id}: {cycle.iterations} iterations, "
                f"improvement: {cycle.score_improvement:+.1f}"
            )
        except Exception as e:
            print(f"[Self-Improve] Error: {e}")

    def _apply_lessons_to_prompt(self):
        """Fold recurring critique lessons into the live system prompt.

        Rebuilds history[0] so the very next turn is shaped by what the
        critic keeps flagging (only lessons seen >= 2 times qualify).
        """
        try:
            lessons = self.self_improve.top_lessons(limit=5, min_occurrences=2)
            if not lessons:
                return
            block = "\n\n[Learned quality rules — apply these]\n" + "\n".join(
                f"- {lesson}" for lesson in lessons
            )
            base = self.persona.build_system_prompt(self.cfg.system_prompt)
            new_prompt = base + block
            if self.history and self.history[0].get("role") == "system":
                self.history[0]["content"] = new_prompt
        except Exception:
            pass

    def _recover_budget(self) -> None:
        """Step a halved budget back toward BASE after a clean turn."""
        if not getattr(self, "_overflowed_last_turn", False) and (
            self.context_budget_tokens < self.BASE_CONTEXT_BUDGET_TOKENS
        ):
            self.context_budget_tokens = min(
                self.BASE_CONTEXT_BUDGET_TOKENS, self.context_budget_tokens * 2
            )

    def _hard_trim(self) -> None:
        self._overflowed_last_turn = True
        self.context_budget_tokens = max(
            self.MIN_CONTEXT_BUDGET_TOKENS, self.context_budget_tokens // 2
        )
        self._trim()

    def chain_status(self) -> list[tuple[int, str, str, str, str]]:
        rows = []
        for p in sorted(self.cfg.providers, key=lambda x: x.priority):
            kind, wait = self.router.breakers.describe(p.name)
            used = self.ledger.today(p.name)
            left = max(0, p.daily_token_budget - used)
            if not getattr(p, "supports_chat", True):
                state = "NO-CHAT"
            elif used >= p.daily_token_budget:
                state = "BUDGET-OUT"
            elif not kind.startswith("ok"):
                state = f"COOLING[{kind}] {int(wait)}s"
            elif kind != "ok":
                state = f"READY*({kind})"
            else:
                state = "READY"
            rows.append((p.priority, p.name, p.model, state, f"{left:,}"))
        return rows

    def stats(self) -> dict:
        return {
            "usage": self.ledger.summary(),
            "total_hops": self.router.total_hops,
            "last_brain": self.router.last_provider,
            "context_messages": len(self.history),
            "context_budget": self.context_budget_tokens,
            # T3 (ROADMAP P0): boot-time job registration failures, so the
            # dashboard/HUD and /tasks view can show them instead of console only.
            "scheduler_job_errors": list(getattr(self, "_job_errors", [])),
            # T6 (ROADMAP P2): vector-search state — "off" means every search
            # is silently FTS-only and the HUD must show it loudly.
            "embeddings": _embeddings_status(getattr(self, "knowledge_store", None)),
            # Defect #1: per-turn context accounting for stats()/HUD.
            "context_usage": dict(getattr(self, "_last_context_usage", {}) or {}),
        }

    def offline_draft(self, user_input: str) -> str:
        """Last-resort answer with zero provider calls: memory + knowledge.

        Used when every brain is down so the user still gets something
        useful (clearly labeled) instead of a bare error."""
        parts = [
            "[Offline draft — all brains unreachable. From local memory and indexed docs only:]"
        ]
        try:
            hits = self.memory.search(user_input, k=3)
            if hits:
                parts.append("What I remember:")
                parts += [f"  - {text[:200]}" for _, _, text, _ in hits]
        except Exception:
            pass
        try:
            docs = self.knowledge_store.search(user_input, k=3)
            if docs:
                parts.append("From your documents:")
                for d in docs[:3]:
                    src = d.get("source", "?") if isinstance(d, dict) else "?"
                    txt = (d.get("text", "") if isinstance(d, dict) else str(d))[:200]
                    parts.append(f"  - [{src}] {txt}")
        except Exception:
            pass
        if len(parts) == 1:
            parts.append(
                "Nothing relevant stored locally. Try again when a "
                "provider recovers (see /chain), or ask something "
                "from your notes/documents."
            )
        return "\n".join(parts)[:2000]

    async def speak_alert(self, text: str) -> bool:
        """Speak an unprompted alert aloud. Returns True if spoken.
        Gated by voice_alerts policy; never raises; text delivery unaffected."""
        try:
            from .voice_alerts import should_speak

            if not should_speak(self.voice_alerts):
                return False
            spoken = " ".join((text or "").split())
            if not spoken:
                return False
            if self._alert_speaker is None:
                from voice.tts import Speaker2

                # Use the configured paired voices (Sonia/Kore), NOT the
                # Speaker2 constructor defaults (Ryan male). A bare
                # Speaker2() here spoke alerts in a different, male voice.
                v = self.cfg.voice
                self._alert_speaker = Speaker2(
                    voice=getattr(v, "piper_voice", "en_GB-alan-medium"),
                    edge_voice=v.tts_voice,
                    enabled=True,
                    engine=getattr(v, "tts_engine", "auto"),
                    persona=getattr(v, "tts_persona", ""),
                    gemini_voice=getattr(v, "gemini_voice", "Kore"),
                )
            ok = await self._alert_speaker.say(spoken[:600])
            return bool(ok)
        except Exception as exc:
            print(f"[voice-alert] skipped: {exc}")
            return False

    async def maybe_budget_alert(self) -> str:
        """Once-per-day Telegram nudge when a free tier drops below 20%.
        Cheap (a few SQLite SUMs). Never raises."""
        try:
            from .budget_guard import due_alerts, format_alert, record_alerts, tier_status

            state = self.cfg.db_path.parent / "budget_alerts.json"
            due = due_alerts(self.router.providers, self.ledger, state)
            if not due:
                return ""
            msg = format_alert(tier_status(self.router.providers, self.ledger), due)
            try:
                result = await self.notify_manager.notify(message=msg, title="Budget guard")
                if result.get("success"):
                    record_alerts(state, due)
                    return msg
            except Exception:
                pass
        except Exception:
            pass
        return ""

    # Actions auto-act may ever execute. notify_* push text; remember stores
    # a fact; schedule_task is fenced hard (once/near/few, see below).
    AUTO_ACT_ALLOW = ("notify", "notify_alert", "notify_reminder", "remember", "schedule_task")
    AUTO_SCHEDULE_MAX_PENDING = 3
    AUTO_SCHEDULE_MAX_DAYS = 7

    def _auto_state(self) -> tuple[bool, bool]:
        """(auto, dry) for proactive auto-act. Runtime file wins, env
        overrides both, legacy config flag counts as auto-on."""
        import os as _os

        from utils.json_utils import load_json_store as _load_store

        auto, dry = False, True
        # P0: shared corrupt-file contract — a corrupt toggle file quarantines
        # aside (loud) and reads as off/dry-on (safe side) instead of silently
        # resetting the operator's choice. The path lookup itself stays
        # fail-soft: minimal/partial configs without db_path skip the file.
        try:
            auto_path = self.cfg.db_path.parent / "proactive_auto.json"
        except Exception:
            auto_path = None
        if auto_path is not None:
            data = _load_store(auto_path, default={}, owner="auto_act", expect=dict)
            auto, dry = bool(data.get("auto", False)), bool(data.get("dry", True))
        try:
            if (
                bool(getattr(getattr(self, "cfg", None), "proactive", None))
                and self.cfg.proactive.auto_act
            ):
                auto = True
        except Exception:
            pass
        if _os.getenv("PROACTIVE_AUTO_ACT", "").strip() == "1":
            auto = True
        if _os.getenv("PROACTIVE_DRY_RUN", "").strip() == "0":
            dry = False
        return auto, dry

    def set_auto_state(
        self, auto: bool | None = None, dry: bool | None = None
    ) -> tuple[bool, bool]:
        """Persist auto/dry toggles to data/proactive_auto.json. Default off/dry-on.
        Returns the effective (auto, dry) after write (before env overrides)."""
        from datetime import datetime as _dt

        from utils.json_utils import load_json_store as _load_store
        from utils.json_utils import save_json_atomic as _save_store

        cur_auto, cur_dry = False, True
        try:
            path = self.cfg.db_path.parent / "proactive_auto.json"
        except Exception:
            return self._auto_state()
        data = _load_store(path, default={}, owner="auto_act", expect=dict)
        cur_auto = bool(data.get("auto", False))
        cur_dry = bool(data.get("dry", True))
        if auto is not None:
            cur_auto = bool(auto)
        if dry is not None:
            cur_dry = bool(dry)
        _save_store(
            path,
            {
                "auto": cur_auto,
                "dry": cur_dry,
                "updated": _dt.now().isoformat(timespec="seconds"),
            },
        )
        return self._auto_state()

    def recent_acts(self, limit: int = 5) -> list[dict]:
        """Last N rows from data/proactive_acts.jsonl (newest last). Never raises."""
        import json as _json

        try:
            lines = (
                (self.cfg.db_path.parent / "proactive_acts.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        except Exception:
            return []
        out = []
        for line in lines[-max(1, min(int(limit), 20)) :]:
            try:
                out.append(_json.loads(line))
            except Exception:
                continue
        return out

    def _audit_act(self, mode: str, s, detail: str = "") -> None:
        """Append-only audit of every auto-act decision (live AND dry)."""
        import json as _json
        from datetime import datetime as _dt

        try:
            path = self.cfg.db_path.parent / "proactive_acts.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    _json.dumps(
                        {
                            "ts": _dt.now().isoformat(timespec="seconds"),
                            "mode": mode,
                            "suggestion": str(getattr(s, "id", "?")),
                            "action": str(getattr(s, "action", "?")),
                            "args": str(getattr(s, "action_args", ""))[:300],
                            "detail": str(detail)[:200],
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass

    def _auto_schedule_count(self) -> int:
        """Unrun [auto]-created reminders (the cap denominator)."""
        n = 0
        try:
            for t in self.scheduler.list_tasks():
                if (
                    str(getattr(t, "name", "")).startswith("[auto]")
                    and not getattr(t, "run_count", 0)
                    and not getattr(t, "last_run", None)
                ):
                    n += 1
        except Exception:
            pass
        return n

    def _validate_auto_schedule(self, args) -> str | None:
        """Validate auto schedule_task args. Returns error text or None if OK."""
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        if not isinstance(args, dict):
            return "schedule_task args must be an object"
        if not str(args.get("name", "")).startswith("[auto]"):
            return "auto schedule_task name must start with [auto]"
        if args.get("trigger_type") != "once":
            return "auto schedule_task must be once-type"
        try:
            spec = args.get("trigger_spec", {}) or {}
            if isinstance(spec, str):
                import json as _json

                spec = _json.loads(spec)
            run_at = _dt.fromisoformat(str(spec.get("run_at", "")))
        except (ValueError, TypeError):
            return "auto schedule_task needs a valid run_at"
        now = _dt.now()
        if run_at <= now:
            return "auto schedule_task run_at is in the past"
        if run_at > now + _td(days=self.AUTO_SCHEDULE_MAX_DAYS):
            return f"auto schedule_task beyond {self.AUTO_SCHEDULE_MAX_DAYS}d horizon"
        if args.get("action") != "notify_reminder":
            return "auto schedule_task may only fire notify_reminder"
        msg = args.get("action_args", {}) or {}
        if isinstance(msg, str):
            try:
                import json as _json

                msg = _json.loads(msg)
            except Exception:
                return "auto schedule_task needs action_args.message"
        if not isinstance(msg, dict) or not str(msg.get("message", "")).strip():
            return "auto schedule_task needs action_args.message"
        if self._auto_schedule_count() >= self.AUTO_SCHEDULE_MAX_PENDING:
            return f"already {self.AUTO_SCHEDULE_MAX_PENDING} unrun auto-reminders"
        return None

    async def _maybe_act_on_suggestion(self, s) -> str | None:
        """Execute a suggestion's action when auto-act is on.
        Guards: confidence >= 0.9, action in AUTO_ACT_ALLOW, per-action
        validation (schedule_task fenced to once/near/few). Dry-run mode
        logs + announces without executing. Dismisses the suggestion after
        a LIVE act so it fires at most once per issue. Returns result text
        if acted (or would-act), else None. Never touches history (orphan
        tool messages poison strict validators). Never raises."""
        import json as _json

        try:
            if not getattr(getattr(self, "cfg", None), "proactive", None):
                return None
            auto, dry = self._auto_state()
            if not auto:
                return None
            action = getattr(s, "action", None)
            if not action or action not in self.AUTO_ACT_ALLOW:
                return None
            if float(getattr(s, "confidence", 0) or 0) < 0.9:
                return None
            args = getattr(s, "action_args", None) or {}
            if action == "schedule_task":
                problem = self._validate_auto_schedule(args)
                if problem:
                    self._audit_act("blocked", s, problem)
                    return None
            if action == "remember":
                text = args.get("text", "") if isinstance(args, dict) else ""
                if not str(text).strip():
                    return None
            if dry:
                announced = getattr(self, "_dry_announced", None)
                if announced is None:
                    announced = self._dry_announced = set()
                self._audit_act("dry", s, "would act")
                if getattr(s, "id", "") in announced:
                    return None
                announced.add(getattr(s, "id", ""))
                return f"would act → {action} {str(args)[:160]}"
            result = await self.toolbox.execute(
                action, _json.dumps(args) if isinstance(args, dict) else str(args)
            )
            self._audit_act("live", s, str(result)[:200])
            # Swallowed by design: an un-dismissed suggestion is re-offered later,
            # never lost.
            with contextlib.suppress(Exception):
                self.proactive.dismiss(getattr(s, "id", ""))
            return str(result)[:300]
        except Exception:
            return None

    def _failed_task_suggestions(self) -> list:
        """Scheduler tasks carrying last_error become one-shot suggestions."""
        from types import SimpleNamespace as _NS

        if not hasattr(self, "_acted_failures"):
            self._acted_failures: set = set()
        out = []
        try:
            tasks = self.scheduler.list_tasks()
        except Exception:
            return out
        for t in tasks:
            err = getattr(t, "last_error", None)
            if not err or t.id in self._acted_failures:
                continue
            self._acted_failures.add(t.id)
            out.append(
                _NS(
                    title=f"Task '{t.name}' keeps failing",
                    description=f"Last error: {err[:200]}. Check /tasks.",
                    confidence=0.95,
                    action="notify_alert",
                    action_args={
                        "message": f"Task '{t.name}' failed: {err[:200]}",
                        "title": "Task failure",
                    },
                )
            )
        return out

    async def _fire_automation(self, event_type: str, context: dict, limit: int = 3) -> list[dict]:
        """Evaluate rules AND execute their actions (the old code matched rules
        but never ran anything). Returns [{rule_id, action, result}]."""
        import json as _json

        fired = []
        try:
            actions = self._check_automation(event_type, context)
        except Exception:
            return fired
        for entry in actions[:limit]:
            action = entry.get("action", {}) if isinstance(entry, dict) else {}
            tool = action.get("tool", "")
            args = action.get("args", {})
            if not tool:
                continue
            try:
                result = await self.toolbox.execute(
                    tool, _json.dumps(args) if isinstance(args, dict) else str(args)
                )
            except Exception as exc:
                result = f"error: {exc}"
            fired.append(
                {"rule_id": entry.get("rule_id", "?"), "action": tool, "result": str(result)[:500]}
            )
        return fired

    def _check_automation(self, event_type: str, context: dict) -> list[dict]:
        # Publish to the event bus (workflow event-triggers + audit trail).
        # Swallowed by design: automation rules are best-effort.
        with contextlib.suppress(Exception):
            self.event_bus.publish(event_type, context)
        try:
            from .automation import AutomationEngine

            if not hasattr(self, "_automation"):
                self._automation = AutomationEngine(self.cfg, self)
            return self._automation.evaluate(event_type, context)
        except Exception:
            return []

    async def _store_conversation_memory(self, user_input: str, assistant_response: str):
        """Store conversation in MemoryV2."""
        try:
            # Store user message as episodic
            self.memory_v2.add_episodic(
                event_type="conversation",
                content=f"User: {user_input}",
                context={"role": "user", "session_id": self.session_id},
                importance=0.7,
            )

            # Store assistant response as episodic
            self.memory_v2.add_episodic(
                event_type="conversation",
                content=f"Assistant: {assistant_response[:500]}",
                context={"role": "assistant", "session_id": self.session_id},
                importance=0.6,
            )

            # Extract facts from conversation (simple heuristic)
            combined = f"{user_input} {assistant_response}"
            self._extract_and_store_facts(combined)

        except Exception:
            pass

    def _extract_and_store_facts(self, text: str):
        """Extract simple facts from text and store as semantic."""
        # Simple fact extraction: "X is Y" patterns
        import re

        patterns = [
            r"(\w+(?:\s+\w+)*)\s+is\s+([^.]+)",
            r"(\w+(?:\s+\w+)*)\s+was\s+([^.]+)",
            r"(\w+(?:\s+\w+)*)\s+has\s+([^.]+)",
            r"(\w+(?:\s+\w+)*)\s+equals?\s+([^.]+)",
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                subject = match.group(1).strip()
                value = match.group(2).strip()
                if len(subject) > 1 and len(value) > 1:
                    self.memory_v2.add_fact(
                        subject=subject,
                        predicate="is",
                        object_=value,
                        confidence=0.7,
                        source="conversation",
                        importance=0.5,
                    )

    async def aclose(self) -> None:
        # Teardown is best-effort by design: every stop is independent so one
        # wedged subsystem cannot block the rest (see the module rules).
        # Shutdown-hang fix: the HTTP client close below used to be a bare
        # await — one wedged connection stalled Ctrl+C with no timeout and no
        # message. Everything here is now individually guarded.
        with contextlib.suppress(Exception):
            await self.workflow_engine.stop_all()
        with contextlib.suppress(Exception):
            await self.event_bus.stop()
        with contextlib.suppress(Exception):
            self.self_improve.close()
        with contextlib.suppress(Exception):
            await self.client.aclose()
        with contextlib.suppress(Exception):
            await self.scheduler.stop()
        with contextlib.suppress(Exception):
            await self.mesh.stop()
        with contextlib.suppress(Exception):
            await self.ml_proactive.stop()

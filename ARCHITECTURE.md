# JARVIS MARK 3 — Architecture & Audit Report

*Generated 2026-09-21. 184 Python files, ~28k lines of app code, 55 test files (350+ tests).*

---

## 1. What this system is

A personal AI assistant that lives on your Windows PC and follows you across surfaces:
**terminal CLI** (the primary), **web dashboard**, **Telegram bot**, **WhatsApp bridge**,
**voice** (wake word + push-to-talk), a **desktop HUD orb**, and a **mesh layer** that lets
it delegate to other machines. All intelligence runs through one "brain" (the CLI
orchestrator); every other surface is a thin client of it.

Design rules that hold the whole thing together:
- **One brain, many mouths** — CLI owns the orchestrator; API child/Telegram/web dial in over the mesh (port 8082) or share its SQLite files directly.
- **Everything degrades, nothing dies** — every feature fails soft: TTS engine chains, provider failover, offline queues, local fallbacks.
- **Free-tier only** — all LLM providers are free tiers with budgets, circuit breakers, and learned routing.
- **Shared state is SQLite (WAL)** — no external services; every cross-process feature (reminders, journal, events) is a `.db` file in `data/`.

---

## 2. Process map — what runs when JARVIS is up

```
jarvis-start.bat
└─ python jarvis.py                    ← THE BRAIN (CLI REPL + orchestrator)
   ├─ uvicorn api.main:app :8080       ← managed child: web dashboard + API + Telegram polling
   ├─ pythonw hud.py                   ← managed child: Siri-style orb overlay
   ├─ node wa-bridge/server.js :3101   ← managed child: WhatsApp (Baileys)
   ├─ mesh listener :8082              ← in-process: unified-brain server for the API child
   ├─ scheduler loop                   ← in-process: reminders, mail-watch, storm-drain
   ├─ wake-word listener               ← in-process: "Hey Jarvis" (openwakeword)
   └─ unlock greeter                   ← in-process thread: lock-state watcher
```

Inter-process contract: the CLI binds 8082 (mesh) and 8080 gets the API child.
Single-owner file locks (`core/single_owner.py`) prevent two instances double-polling
Telegram or double-firing reminders.

---

## 3. How one message flows (the educational core)

You type `hello jarvis` in the terminal. Here is the entire journey:

1. **Entry** (`jarvis.py` REPL loop) — aliases resolved, slash-commands checked.
   NL shortcuts get first crack: mail questions, routines, PC control, WhatsApp.
2. **`Orchestrator.process(text, surface="cli")`** — the one true pipeline:
   - `SurfaceJournal.add("cli", "user", …)` — journaled for cross-device continuity.
   - `DialogueManager.process_user_message` — topic tracking, entities, pending questions.
   - `IntentClassifier.classify` — keyword scoring → task type (coding/research/fast/…)
     → `router.set_preferred_names(...)` biases which brain answers.
   - Auto-background gate: heavy prompts dispatch to a specialist agent instead of blocking chat.
   - Context assembly: memory facts, dialogue summary, cross-surface turns, RAG hits,
     preferences — injected as system messages in a fixed order.
3. **`FailoverRouter.chat(messages, tools, task_type)`** (`core/router.py`) — picks a provider:
   sorted by (preferred names → sticky last-good → rate penalty → budget pressure →
   learned optimizer score → task-tag match → priority). Streams tokens; on 429/timeout
   it "hops" to the next candidate (the `· hop:` lines you see). Circuit breakers park
   dead providers for minutes; the `UsageLedger` enforces per-day token budgets;
   `RoutingOptimizer` learns which model is good at which task from real outcomes.
4. **Tool loop** — if the model returns tool calls, `ToolBox.execute` runs them
   (`core/tools.py` + `skills/*` handlers), results go back into the model, repeat
   (max rounds capped). Only CORE_TOOLS are sent by default; domain packs
   (`mail_`, `browser_`, `computer`…) load on demand via `load_pack` or keyword hints.

---

## 5. `core/` — the engine room (57 modules)

### Brain & routing
- **orchestrator.py** (~2,360) — the brain: `process()` pipeline, tool loop, memory/RAG/preference injection, auto-background dispatch, alert speaker, mesh server hookup, scheduled-task bootstrap. *The god-file* — read its module docstring for the section map before editing (see §9 "Parked debt").
- **router.py** (583) — provider failover: scoring sort, circuit breakers, budgets, sticky retry, streaming. Highest-traffic module.
- **providers.py** — OpenAI-compatible HTTP client (streaming chat).
- **routing_optimizer.py** — learns task→provider scores from outcomes (SQLite).
- **intent.py** — keyword intent classifier + per-task preferred providers.
- **brain_manager.py** — assigns each specialist agent a primary brain + fallbacks; hourly health swaps.
- **budget_guard.py** — per-tier daily token caps + load shedding.
- **ensemble.py** — multi-model modes (cascade/consensus/self-correction).
- **reasoning.py** — reasoning strategies (CoT, react, reflection) selection.
- **prompt_opt.py** — prompt A/B testing + optimization toolkit.

### Agents
- **agent_pool.py** — specialist agents (coder/writer/researcher/analyst/…), foreground + background execution, per-agent tool subsets.
- **agent_team.py** — pipelines: chains of agents with gate conditions (uses `safe_expr`).
- **agent_mesh.py** (866) — the A2A mesh protocol: TCP JSON-lines, peers, chat relay, federated inference, the unified-brain channel.
- **agent_discovery.py** — LAN peer discovery (zeroconf).
- **distributed.py** — distributed task execution over the mesh.
- **bg_router.py** — decides what goes to background agents vs foreground chat.
- **unified_brain.py** — API-child side: forwards chat to the CLI brain over the mesh; local fallback when unreachable.

### Memory & knowledge
- **memory.py** — facts/preferences store (remember/recall/forget). The *primary* store.
- **memory_v2.py** — episodic memory (time-stamped events, anniversaries). Parallel system, both live — see §8.
- **knowledge.py** (424) — document store + indexer (your files → searchable knowledge).
- **rag_v2.py** — retrieval over knowledge + conversations (fastembed/BM25).
- **knowledge_distiller.py** — condenses knowledge into learnings.
- **consolidation.py** — promotes durable facts to preferences (deterministic, no LLM).
- **anniversary.py** — "a year ago today" queries.

### Action & automation
- **scheduler.py** — the reminder/timer engine: once/daily/interval/cron, catch-up of missed fires, action execution.
- **workflow.py** — multi-step workflows with `$var` interpolation, foreach, gate conditions.
- **flow_composer.py** — LLM-built workflows from a description.
- **automation.py** — event→action rules (the `/rule` system).
- **code_agent.py** — file/shell/git with blocklist + approval queue (approvals off by your choice).
- **safe_expr.py** — AST-whitelist gate-condition evaluator (replaced `eval`).
- **tool_use_v2.py** — next-gen tool-call parsing helpers.
- **tools.py** — the ToolBox registry + tool packs (core always-on, domain on demand).

### Senses & output
- **vision.py** — screenshots, screen reading (mss).
- **multimodal.py** — image/audio analysis via vision/STT providers.
- **browser.py** — Playwright browser automation.
- **mail.py** — read-only IMAP (unread/search/read; never deletes).
- **mail_triage.py** — deterministic importance tiers (VIP/urgent/expiry keywords).
- **whatsapp_inbox.py** — WA read + approval-gated send via the bridge.
- **notify.py** — multi-channel push (Telegram/ntfy/Pushover/webhook) with fallback chain.
- **calendar_feed.py** — ICS calendar reading.
- **proactive.py / proactive_push.py / ml_proactive.py** — suggestion engine, push gate (mute/cap/dedup), ML behavior model.
- **session_greeting.py** — unlock greeting (lock-state watcher thread).
- **surface.py** — cross-device turn journal.
- **voice_alerts.py** — quiet-hours + master-switch policy for unprompted speech.

### Infra
- **config.py** — typed config loader (yaml + .env). *Had the double-get bug we fixed.*
- **db.py** — SQLite helper (WAL + busy timeout everywhere).
- **events.py** — event bus (in-process pub/sub + persisted `events.db`).
- **logging_config.py** — loguru setup.
- **single_owner.py** — file-lock ownership (Telegram polling, ports).
- **storm_queue.py** — offline answer queue for total provider outages.
- **telegram.py** (803) — the whole Telegram bot surface.

## 6. `skills/` — the tool packs (34 files)

Each file registers tools into the ToolBox (`register_X(box, …)`). Grouped:

- **Mail**: `mail_skills.py` (unread/search/read/triage/draft/send/watcher)
- **Files & code**: `fs.py`, `code_agent_skills.py`, `code_intel.py`, `code_exec.py`
- **PC control**: `pc.py`, `pc_flows.py`, `computer_use.py`, `system.py`
- **Web**: `web.py` (search), `browser.py`, `webwatch.py` (page monitors), `web_agent.py`
- **Knowledge**: `knowledge_skills.py` · **Memory**: `memory.py` · **Scheduling**: `scheduler_skills.py`, `routine_skills.py`
- **Proactive/ML**: `proactive_push.py`, `ml_proactive_skills.py`, `self_improve_skills.py`
- **Comms**: `notify_skills.py`, `wa_skills.py`, `a2a_skills.py`, `triage.py`
- **Vision/voice**: `vision_skills.py`, `voice_doctor.py` · **Workflows**: `workflow_skills.py`
- **Briefing**: `briefing.py` · **Dev loop**: `dev_loop.py` · **Plugins**: `plugin_marketplace.py`

## 7. `voice/` — the voice stack

- **session.py** — VoiceSession: push-to-talk + wake mode + distiller wiring.
- **tts.py** (557) — Speaker2: engine chain (edge→gemini→piper→sapi, kokoro opt-in), playback, barge-in hook, engine health cooldowns.
- **mic.py** — VAD recording (tuned for your quiet mic) + BargeInMonitor.
- **stt.py** — faster-whisper transcription · **wake.py** — openwakeword "hey_jarvis".
- **distiller.py** — long replies → 2-sentence butler lines · **expressive.py** — tone + Gemini TTS styles · **audio.py** — devices + cleanup.

---

## 8. Findings — what's actually wrong

### 🔴 Real problems
1. **`jarvis.py` (3,110 lines) is five programs in one file**: REPL, startup orchestration, NL shortcut engine (~700 lines of mail/wa/pc shortcuts), slash-command dispatch, child supervision. Every feature touches it.
2. **`orchestrator.py` (2,080)**: pipeline + alerts + mesh server + scheduler bootstrap + bg dispatch + self-improve wiring. Same problem.
3. ✅ ~~**`api/main.py` (1,424)**: 60+ endpoints in one file~~ → split into `api/state.py` + 9 routers under `api/routes/` (main.py now 372 lines).
4. ✅ ~~**Two memory systems live side by side** (`memory.py` facts + `memory_v2.py` episodic)~~ → boundary documented in the module docstrings of `core/memory.py` (curated, prompt-injected) and `core/memory_v2.py` (automatic episodic/semantic/procedural); intentional, no merge.
5. ✅ ~~**`web_agent.py`** registers 9 tools all named `default`~~ → deleted: `register_web_agent` was a no-op placeholder (nothing ever registered), functions duplicated `skills/web.py` + `skills/browser.py` + `skills/webwatch.py` and contained real bugs (`web_monitor_price` NameError). Unwired from `core/tools.py`.
6. ✅ ~~**Zen provider block**~~ → pruned: 9 permanently 403-gated entries removed from `config.yaml` (906→805 lines), replaced by one comment recording the probe result, removed model names, and the re-add condition.

### 🟡 Dead-ish weight
- `archive/` (git history only), `scripts/dev/` experiments.
- `ai-code-reviewer/` and `todo_app/` — unrelated subprojects living in the repo.
- ✅ ~~`tray.py`~~ — deleted (step 8): legacy pre-mesh design with its own orchestrator instance; the orb HUD + web dashboard replace its role. Recoverable from git history if ever needed.

### 🟢 Genuinely good
- Router + breakers + budgets + learned routing (battle-tested by your 429 storms).
- Single-owner locks, WAL-everywhere, fail-soft discipline.
- Tool packs with on-demand loading.
- The test suite (350+ green) — the reason refactoring is safe.

## 9. Recommended consolidation order (pain removed per hour)

1. ✅ ~~Extract NL shortcuts from jarvis.py~~ → done: `skills/nl_shortcuts.py` (jarvis.py 3377→3107 lines, +7 regression tests).
2. ✅ ~~Extract child supervision (API/HUD/wa spawn + watchdogs)~~ → done: `core/supervisor.py` (jarvis.py 3107→2756 lines, +16 regression tests; also fixed latent UnboundLocalError for `api_proc` in brain-only shutdown).
3. ✅ ~~Extract slash-command table~~ → done: `core/commands.py` (data-driven registry; jarvis.py 2756→687 lines, +25 regression tests; /morning·/evening folded into shared `run_routine_tool` also used by the NL shortcut; pre-existing `/bg`→`/bgmode` prefix shadow preserved and documented).
4. ✅ ~~Split api/main.py into routers~~ → done: `api/state.py` (shared runtime state + `get_auth_token`) + `api/routes/{system,voice,tasks,memory,workflows,agents,web,vision,chat}.py` (api/main.py 1,695→372 lines, +7 regression tests; verbatim handler moves, `state.*` request-time access, PEP 562 shim keeps `api.main.orch`-style access working).
5. ✅ ~~**Document the memory/memory_v2 boundary**~~ → done: module docstrings added to `core/memory.py` and `core/memory_v2.py` (curated vs. automatic store, prompt-injection path, no double-store rule).
6. ✅ ~~**Delete/repair `web_agent.py`**~~ → done: deleted `skills/web_agent.py` + removed its registration from `core/tools.py` (toolbox unchanged — it registered nothing).
7. ✅ ~~**Prune dead zen providers** from config.yaml~~ → done: 9 entries deleted (7 client-gated 403 + laguna retired 401 + 2 muse-spark responses-only), one explanatory comment kept. Stale zen names in `agent_pool.py`/`ensemble.py` lists are inert — the router's `only=` filter skips unknown providers and falls back to the full chain.
8. ✅ ~~**Decide tray.py's fate**~~ → done: **deleted** `tray.py` (365 lines) + its README references; nothing imported it. `core/config.py`'s `TrayConfig` section is unrelated (it configures HUD spawning) and stays.

Each step is one focused session with the test suite green before and after.

*Nothing above requires stopping daily use — it's renovation, not demolition.*

### Parked debt (deliberately NOT scheduled)

After §10 the remaining debt is *shape*, not trustworthiness — every module passes
the gate below. Nothing here is a bug; do not start a split that isn't forced by a
real feature or a real bug report.

| File | Lines | What hurts | Ready-made direction |
|---|---|---|---|
| `core/orchestrator.py` | ~2,360 | one class, ~55 methods; `__init__` alone is ~730 lines of subsystem wiring | **section map now lives in the module docstring** (2026-09-25) — read the file in sections, not top-to-bottom. If a split is ever forced: extract the `__init__` wiring into per-phase `_wire_*` methods, then lift the `start()` job table (already isolated behind `_add_job`) into its own module. |
| `core/commands.py` | ~2,190 | data-driven slash registry, mostly literal tables | move the tables to `core/commands_data.py`; the registry logic is already tiny. |
| `core/agent_mesh.py` | ~990 | protocol codec + peer bookkeeping + relay in one class | split the JSON-lines codec from peer state when federated inference next changes. |
| `core/telegram.py` | ~865 | handlers + long-poll loop + formatting | handlers → `core/telegram_handlers.py`. |
| `skills/code_agent_skills.py` | ~680 | several skills per file | already grouped by tool; leave alone. |

Also parked: the repo-wide lint baseline (`ruff check .` → ~1,049 findings across the
project; the pre-commit gate only enforces `E9,F601,F821,F402,F811`). `orchestrator.py`
itself was brought to zero findings on 2026-09-25; the rest is mechanical debt
(`SIM105`/`E741`/`W291`/`B904`) that can be burned down file-by-file whenever a file is
touched for another reason.

- **github_sync.py** — repo sync helpers.
- **plugins.py / plugin_registry.py / skill_acquisition.py / skill_manifest.py** — plugin system.
- **self_improve.py** — critique→refine→distill loop, lessons persisted to SQLite.

- **preferences.py** — learned user preferences with evidence.
- **project_memory.py** — per-repo conventions/threads/standups.
- **dialogue.py** — conversational state: threads, topic shifts, pending questions.
- **persistence.py** — conversation history in SQLite (resume across restarts).
- **summarizer.py** — long-thread summarization.

5. **Answer** — streamed to the terminal, journaled as the assistant turn, fed to
   `preferences.learn_from_turn`, and (if `/tts on`) distilled + spoken by `voice/`.

If every provider is down: `AllProvidersDown` → the turn lands in `storm_queue`
(`data/storm_queue.json`) and is answered on Telegram + voice when quota returns.

---

## 4. Entry points

| File | Role |
|---|---|
| `jarvis.py` (687 lines) | CLI REPL + startup orchestration. Slash commands → `core/commands.py`; NL shortcuts → `skills/nl_shortcuts.py`; child-process supervision (API/HUD/wa-bridge) → `core/supervisor.py`. |
| `api/main.py` (372) | FastAPI app shell: lifespan, middleware, static/dashboard hosting. Endpoints live in `api/routes/*` (9 domain routers), shared runtime state in `api/state.py`. |
| `hud.py` / `hud_orb.py` | Desktop overlay. `hud.py` = info card (`--card`); default = the orb (voice in/out, no text). |
| `run_telegram_bot.py` | Standalone Telegram runner (normally the API child owns polling). |
| `wa-bridge/server.js` | Node/Baileys WhatsApp bridge: QR pairing, message cache, REST on :3101. |

---

## 10. Hardening waves — making every module trustworthy

Per-module gate: contract docstring · no silent failures (fail-soft is fine,
fail-invisible is not — aux subsystems swallow by documented design only) ·
failure paths tested · boot smoke · no dead references · suite green.

| Wave | Modules | Status |
|---|---|---|
| 0 — The Spine | `core/router.py`, `core/orchestrator.py`, `core/config.py`, `core/db.py` | ✅ contract docstrings; router dup defs removed; str-path ledger bug fixed; loud malformed-provider errors; empty-YAML boot; YAML bool-name coercion; +11 tests |
| 1 — Boot & API | `jarvis.py`, `core/supervisor.py`, `api/state.py`, `api/main.py` + `api/routes/*` | ✅ contract docstrings; double `_bg_event_poller` fixed (service mode double-broadcast); jarvis.py `tg_bot`/`telegram_bot` double-stop unified; dead `_time` import dropped; lifespan boot smoke (poller count, telegram lock acquire/release/lock-loser); +8 tests |
| 2 — Data integrity | `memory.py`, `memory_v2.py`, `persistence.py`, `preferences.py` | ✅ contract docstrings (persistence/preferences added); persistence: orphan-message guard (add_message raises on unknown session) + FTS-sanitized search; memory_v2: find_skills_for DB-backed (survives restart), search_all small-k starvation fixed; preferences: set/get/remove share key normalization, atomic saves, corrupt file moved aside not emptied; +9 tests |
| 3 — Tools & agents | `tools.py`, `tool_use_v2.py`, `agent_pool.py`, `skills/*` | ✅ contract docstrings everywhere (3 core + 20 skills); stale zen-* providers removed from agent_pool/ensemble/budget_guard (pruned in config 2026-09-22); tool_use_v2: execute_parallel now flags "error:" results as failures (was success=True always), dead category refs dropped, import_chain validates; +19 tests |
| 4 — Voice | `voice/*` + `core/multimodal.py` | ✅ contract docstrings (all 8 voice modules); dead `_HEADER_RE` dropped; multimodal: dead `Speaker` import + broken whisper fallback fixed (text_to_speech was silent-None always, _local_whisper awaited a sync method on a path); mic env-parse narrowed to ValueError; session volume-command failures now visible on console; barge-in/STT-resample/tts-split/wav-wrap/voice-command/DSP failure paths; +17 tests |
| 5 — Comms | `telegram.py`, `whatsapp_inbox.py`, `wa_skills.py`, `mail_skills.py` | ✅ contract docstrings everywhere (mail_skills had a one-liner); telegram `_load_state` + `_call` failures loud (were silent), voice-note tmp file cleaned in `finally`, duplicate `/storm` help line dropped; whatsapp_inbox dead module-level `BASE` removed; dead `register_web_agent` dropped from default toolbox (skill file long gone); draft-store contract enforced — `_save_draft`/`wa_draft`/triage-draft write failures now return empty + log + report instead of announcing unsaved drafts, mail watcher skips unsaved drafts (was announcing id-less ghosts), sent-draft removal + seen-state + repaired-recipient write failures all logged (double-send / re-alert spam risks); +30 tests |
| 6 — Peripherals | vision, proactive, ml_proactive, workflows, self_improve | ✅ contract docstrings; vision: `analyze_screen` now truly async (was returning an un-awaited coroutine), capture helpers lazy (headless-safe import), corrupt screenshot/clipboard reads logged; proactive: corrupt `suggestions.json` quarantined aside + loud, save failures logged not raised; ml_proactive: `_extract_sequences` unbounded-duplication bug fixed, partial pickles no longer replace healthy defaults (loud "claims fitted but missing detector/scaler"), `get_suggestions(None)` crash fixed, `hash()`→`zlib.crc32` for persisted-feature stability, sklearn-less construction no longer NameErrors, `stop()` suppresses CancelledError; workflow: `_run_step` treats ToolBox `"error: ..."` strings as step failures (was recorded "ok"), `_record_step` + final run-UPDATE guarded loud (runs no longer wedge as "running"); self_improve: all silent `except: pass` in persistence (cycles/lessons/examples) now loud logs, malformed synthetic-gen JSON logged; skills: lying ML stubs wired to the real engine (ml_stats/ml_anomaly_check/ml_train real calls, dead ml_get_prediction dropped), dead screenshot lambda registration replaced with the real fail-soft handler, E741 fixes; +18 tests |
| 7 — Hot-path cluster | `agent_mesh`, `agent_team`, `agent_discovery`, `events`, `scheduler`, `proactive_push`, `notify`, `knowledge`, `prompt_opt`, `project_memory`, `mail` | ✅ contract docstrings everywhere; agent_mesh: ~14 silent excepts → loud, dead code removed (MeshMessage/AgentNode/_process_queue), bug fixes (broadcast exclude mutation, surface= passthrough, B023, gossip parse narrowing, stop() task-failure masking); agent_team: auto-review/on_complete/bg-note failures loud; agent_discovery: loud local-IP fallback + peer announce/scan/fetch failures; events: publish() still never raises but persist/queue/handler/trim/corrupt-row all loud; scheduler: bad cron expr loud + recorded as last_error (was silent disable), corrupt stored rows loud-skipped instead of crashing load, retry-also-failed printed; proactive_push: corrupt mute/count/dedup state files loud (silent unmute / double-send risks); notify/mail: verified fail-loud contracts (error dicts, channel fallback) now tested; knowledge: per-file extraction failures loud naming the file, embedder loss announced; prompt_opt: corrupt evolved state loud; project_memory: corrupt file loud (was silent total wipe), save failures reported ok:False; +81 tests |

§10 status: **complete**. Every module on the request path (spine → boot → data → tools → voice → comms → peripherals → hot-path cluster) now carries a contract docstring, loud failure paths, and failure-path tests. Remaining unhardened files are aux-by-design (dev tooling, one-off scripts) and out of scope by the gate definition above.

| 7 — Hot-path cluster | agent_mesh, agent_team, agent_discovery, events, scheduler, proactive_push, notify, knowledge, prompt_opt, project_memory, mail | ✅ contract docstrings everywhere; agent_mesh: loud wire failures + dead-code removal + bug fixes; events/agent_team/agent_discovery: loud subscription/mesh failure paths; scheduler: bad cron expr loud + `last_error`, corrupt stored rows/next_run loud-skipped per row, retry failures printed, WAL pragma + migration loud fail-soft; proactive_push: corrupt mute/count/dedup state loud (preference-loss class); notify verified already fail-loud by design; knowledge: per-file extraction failures (txt/pdf/docx/xlsx/pptx/ocr) loud naming the file, embedder loss announced as FTS-only fallback; prompt_opt: corrupt prompts.json/examples.jsonl loud (evolved user value), unparseable model generation loud + `[]`; project_memory: corrupt memory file loud (was silent total wipe), `_save` returns bool — `remember`/`forget` report `ok: False` when persistence fails (never claim success for data not on disk); mail: IMAP/SMTP errors already returned as `{"error": ...}`, `box.close()` cleanup via `contextlib.suppress`, decode/date fallbacks documented; +74 tests |

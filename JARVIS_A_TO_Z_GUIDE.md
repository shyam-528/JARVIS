# JARVIS MARK 3 — A to Z Guide (Simple Words)

This is your own Jarvis. One brain. Many mouths.
You talk in terminal, web page, Telegram, WhatsApp, or voice. The brain answers.

How it starts:
`jarvis-start.bat` -> `jarvis.py` -> brain starts -> web page on 8080, orb on screen, WhatsApp on 3101, mesh on 8082.

---

## 1. Tech stack (what we used to build it)

- Python 3.10+ : main language. Brain, tools, tests.
- FastAPI + Uvicorn : web page and API on port 8080.
- SQLite (WAL mode) : all memory. 16 small database files in `data/`. No cloud needed.
- Rich + Prompt Toolkit : pretty terminal chat.
- Playwright + MSS + Pillow : browser control and screenshots.
- faster-whisper, edge-tts, Kokoro, Piper, SAPI : ears and mouth (voice).
- openwakeword, webrtcvad, sounddevice : "Hey Jarvis" wake word and mic.
- fastembed, sentence-transformers, scikit-learn : search meaning, not just words.
- httpx, openai client : talk to Groq, Xkiro, Gemini, Nvidia, OpenRouter.
- PyYAML, python-dotenv : `config.yaml` + `.env` settings.
- croniter : understand "daily 8am" type schedules.
- Baileys + Express (Node.js) : WhatsApp bridge on port 3101.
- pytest, pytest-asyncio, ruff : tests and clean code.
- HTML + JS single file : HUD dashboard in `web/index.html`.

Providers today: Groq x3 keys, Xkiro x2 keys (35 models split 20/15), Gemini vision, Nvidia last-resort, OpenRouter paid backup.

---

## 2. Every algorithm and method (simple)

1. Failover router: try model 1, if 429 or timeout, hop to model 2. Sort by preferred name, last-good, rate penalty, budget left, learned score, task tag, priority.
2. Circuit breaker: if a model fails many times, park it for some minutes. Then try again.
3. Token budget: each model gets tokens per day. Stop before overuse. Free tier safe.
4. Learned routing: remember which model was good at coding, fast chat, vision. Pick better next time.
5. Intent check: count keywords. "code, bug, file" = coding task. "fast, hi" = fast task. Then bias model choice.
6. Tool loop: model asks for tool -> we run tool -> give result back -> model replies. Max rounds capped.
7. Tool packs: only 24 core tools sent always. Mail, browser, PC tools load on demand by keyword or `load_pack`.
8. Memory inject: before answering, add facts, recent chat, RAG hits, preferences, time. Fixed order.
9. RAG search: BM25 (word match) + vector (meaning match) -> best docs from your files.
10. Consolidation: once a week, move strong facts to preferences. Never delete, only promote.
11. Scheduler: save tasks in SQLite. Types: once, daily, interval, cron. If PC was off, fire missed ones once (catch-up, deduped per day, 24h anti-spam except housekeeping).
12. Mesh protocol: TCP JSON-lines. CLI binds 8082. API child dials. Chat relay + federated inference.
13. Single owner lock: file lock so only one Telegram poller and one scheduler runs. Fixes 409 Conflict.
14. Storm queue: if all models down, save question to `storm_queue.json`. Answer later on Telegram + voice.
15. TTS chain: try edge-tts -> gemini -> piper -> kokoro -> SAPI. First working wins. 5-min cooldown per broken engine.
16. STT: faster-whisper tiny, CPU, int8. Listens, writes text.
17. Wake word: openwakeword listens for "hey_jarvis" always in background.
18. Distiller: long reply -> 2-line butler line for speaking. So voice is short.
19. Proactive push: suggest only if not muted, not too many, not duplicate. Quiet hours 22-7 suppress speech.
20. Safe expr: gate conditions run with AST whitelist, not eval. Safe.
21. JSON quarantine: if a store file is corrupt, move it aside with timestamp, rebuild empty, print loud. Never silent wipe.
22. Auth tokens: `AuthManager` mints tokens, 30-day expiry, stored in `api_tokens.db`. Web needs token.

---

## 3. Every file and its use

### Root — start here
- `jarvis.py` : THE BRAIN starter. Opens chat, starts web, orb, WhatsApp, scheduler, Telegram.
- `jarvis-start.bat` : double-click starter. Uses venv Python, no traceback.
- `run_telegram_bot.py` : standalone Telegram runner. Normally API child does this.
- `hud.py` : info card on desktop. `hud_orb.py` : Siri-style voice orb.
- `config.yaml` : 805 lines. All 51 providers, priorities, budgets, timeouts. Live probe notes in comments — do not delete comments.
- `.env` : secrets (keys, mail, Telegram). Never commit. `.env.example` : blank template.
- `pyproject.toml` : project name, ruff + mypy rules. `requirements.txt` : loose deps. `requirements.lock` : exact working pins. `pytest.ini` : test path + asyncio mode.
- `opencode.json` : project MCP (sentry). `Register-JarvisAutostart.ps1` : autostart on logon.
- `README.md` : quickstart. `ARCHITECTURE.md` : full audit + waves. `ROADMAP.md` : what is done + next. `HANDOFF.md` : last session handoff. `JARVIS_A_TO_Z_GUIDE.md` : this file.

### `core/` — engine room
- `orchestrator.py` : brain pipeline. process() -> memory -> intent -> router -> tools -> answer. God-file, read section map on top.
- `router.py` : picks model, hops on fail, breakers, budgets.
- `providers.py` : HTTP client to talk to models.
- `config.py` : loads yaml + env. Loud error if bad, defaults if empty. Cleanest file.
- `db.py` : SQLite helper, WAL + timeout everywhere.
- `commands.py` : all `/commands` table. Prints to terminal. God-file 2.
- `supervisor.py` : starts/stops API, HUD, WhatsApp children. No orphans.
- `scheduler.py` : reminders, cron, catch-up.
- `memory.py` : curated facts you saved. Injected every turn. Never decays. `memory.db`.
- `memory_v2.py` : automatic diary. Every turn + regex facts + decay + prune. `memory_v2.db`. Wipe safe.
- `persistence.py` : chat history across restarts. `conversations.db`.
- `preferences.py` : learned likes with proof. Survives corrupt file.
- `project_memory.py` : per-repo notes. Conventions, threads, standups.
- `knowledge.py` : your files -> searchable. `knowledge.db`.
- `rag_v2.py` : BM25 + vector search over knowledge + chats.
- `knowledge_distiller.py` : condenses docs to learnings.
- `consolidation.py` : weekly fact -> preference promoter. No LLM, deterministic.
- `anniversary.py` : "a year ago today".
- `dialogue.py` : topic, threads, pending questions.
- `summarizer.py` : squeezes long chats.
- `intent.py` : keyword task classifier.
- `bg_router.py` : heavy prompt -> background agent, light -> foreground?
- `routing_optimizer.py` : learns task -> provider scores. `routing_scores.db`.
- `budget Guard` = `budget_guard.py` : daily caps + shed load.
- `brain_manager.py` : each agent gets primary brain + fallback. Hourly health swap.
- `agent_pool.py` : coder, writer, researcher agents. Foreground + background.
- `agent_team.py` : chain of agents with gates.
- `agent_mesh.py` : mesh network, peers, relay.
- `agent_discovery.py` : find peers on LAN via zeroconf.
- `distributed.py` : run tasks over mesh.
- `unified_brain.py` : API child forwards chat to CLI brain. Local fallback if down.
- `ensemble.py` : cascade, consensus, self-correct with many models.
- `reasoning.py` : CoT, ReAct, reflection picker.
- `prompt_opt.py` : prompt A/B test.
- `tools.py` : ToolBox registry + packs.
- `tool_use_v2.py` : next-gen tool parser, parallel run.
- `code_agent.py` : file/shell/git with blocklist. Auto-approve true (your choice).
- `safe_expr.py` : safe gate checker.
- `workflow.py` : multi-step `$var`, foreach, gates.
- `flow_composer.py` : LLM builds workflow from words.
- `automation.py` : `/rule` event -> action.
- `scheduler` skills glue below.
- `events.py` : in-process bus + `events.db`.
- `single_owner.py` : file-lock owner.
- `storm_queue.py` : offline queue when all models down.
- `telegram.py` : Telegram bot surface.
- `notify.py` : push via Telegram/ntfy/Pushover/webhook with fallback.
- `proactive.py` : suggestion engine. `proactive_push.py` : mute/cap/dedup gate. `ml_proactive.py` : ML behavior model.
- `mail.py` : Gmail IMAP read-only. Never deletes.
- `mail_triage.py` : VIP/urgent/expiry tiers, deterministic.
- `whatsapp_inbox.py` : WhatsApp read + approval-gated send.
- `calendar_feed.py` : ICS calendar read.
- `vision.py` : screenshot, screen read via mss.
- `multimodal.py` : image/audio analysis.
- `browser.py` : Playwright automation.
- `voice_alerts.py` : quiet-hours + master switch for speech.
- `session_greeting.py` : greets on unlock.
- `surface.py` : cross-device journal. `surface_journal.db`.
- `self_improve.py` : critique -> refine -> distill, lessons in SQLite.
- `skill_acquisition.py`, `skill_manifest.py`, `plugins.py`, `plugin_registry.py` : plugin system.
- `persona.py` : voice/persona settings. `data_analyst.py` : data stats. `deep_research.py` : deep research helper. `github_sync.py` : repo sync. `logging_config.py` : loguru setup.

### `skills/` — tool packs (thin wrappers over core)
- `fs.py` : read/write/edit files. `code_agent_skills.py` : code tools. `code_intel.py` : code search. `code_exec.py` : run code.
- `pc.py`, `pc_flows.py`, `computer_use.py`, `system.py` : open apps, demo_prep, organize downloads, screenshot_note, PC control.
- `web.py` : web search. `browser.py` : browser tools. `webwatch.py` : page monitors. `triage.py` : inbox triage.
- `mail_skills.py` : unread/search/read/draft/send/watch. `wa_skills.py` : WhatsApp triage/chats/read/send. `notify_skills.py` : notify tools. `a2a_skills.py` : mesh tools.
- `knowledge_skills.py` : knowledge search. `memory.py` : remember/recall/forget. `scheduler_skills.py` : schedule_task. `routine_skills.py` : morning_start/evening_wrap.
- `workflow_skills.py` : run workflows. `nl_shortcuts.py` : "remind me, send mail, open chrome" in plain words.
- `briefing.py` : morning/evening briefing. `dev_loop.py` : /init /review /test /pr /standup.
- `vision_skills.py` : screenshot/OCR. `voice_doctor.py` : /doctor self-check.
- `proactive_push.py`, `ml_proactive_skills.py`, `self_improve_skills.py` : proactive + ML + self-improve tools.
- `storm_skills.py` : /storm queue controls. `plugin_marketplace.py` : plugins.

### `api/` — web server :8080
- `main.py` : FastAPI app, lifespan, static HUD. 372 lines (was 1424, now split).
- `state.py` : shared runtime (orchestrator, tokens). `auth.py` : token mint/verify. `websocket.py` : /ws live chat.
- `routes/chat.py` : chat API. `routes/system.py` : health/stats. `routes/voice.py` : TTS/STT. `routes/tasks.py` : tasks + errors. `routes/memory.py` : memory API. `routes/workflows.py` : workflows. `routes/agents.py` : agents. `routes/web.py` : web tools. `routes/vision.py` : vision.

### `voice/` — ears and mouth
- `tts.py` : Speaker2 chain edge->gemini->piper->kokoro->SAPI. `status()/last_error`.
- `stt.py` : faster-whisper transcribe. `mic.py` : VAD record + barge-in. `audio.py` : devices + cleanup.
- `session.py` : push-to-talk + wake mode. `wake.py` : "hey_jarvis". `distiller.py` : long -> 2 lines. `expressive.py` : tone styles.

### Others
- `utils/json_utils.py` : load/save JSON safe. Quarantine corrupt, atomic save. Used by 15 stores.
- `web/index.html` : HUD dashboard single file. `manifest.json`, `sw.js` : PWA bits.
- `wa-bridge/server.js` : WhatsApp Baileys bridge :3101. `wa-bridge/auth/` : session creds — TRACKED by mistake, need `git rm --cached`, rotate if pushed.
- `scripts/eval_router.py` : live probe (spends tokens). `backup_drill.py` : backup + integrity. `web_exercise.py` : 43 web checks. `coverage_ratchet.py` : coverage never-decreases. `chaos_harness.py` : kill child, revoke key, disk full tests.
- `plugins/weather_plugin/skill.py` : example plugin.
- `todo_app/` : separate demo (main, models, service, database). Not wired as skill. Packaged but unused.
- `tests/` : 76 files, 681 tests. Router, chain, memory, mail, WA, voice, boot, scheduler, wave7 failure-paths. Run with `.venv\Scripts\python -m pytest tests/ -q`.
- `data/` : 16 .db files + router_state.json + backups. Git-ignored. `logs/` : jarvis/api/wa/test logs. Git-ignored.
- `archive/`, `ai-code-reviewer/` : old retired code, reference only.

---

## 4. How to use daily (simple)

- Chat: just type. `/reset` clear, `/new` new chat.
- `/chain /health` : see models + budgets. `/briefing` : morning now. `/tasks` : scheduled. Say "remind me at 9pm ..." to set reminder.
- `/memory`, `/forget xxx`, `/consolidate` : memory.
- `/mail`, `/mail triage`, `/wa triage` : inboxes. Drafts never send until you say "send it".
- `/tts on|off`, `/voice`, `/doctor` : voice.
- `/init /review /test /pr title /standup` : coding.
- Telegram: `/status /chat /screenshot /tasks /ask /briefing /remind 30 xxx /mail /wa`.

If 429 wall: `/chain`, wait for reset, optional OpenRouter key. If 409 Conflict: delete `data/telegram_owner.lock`, restart. If silent TTS: look for `tts skipped:` line.

That is everything. Start with `jarvis.py`, follow brain -> router -> tools -> memory. All other files are helpers to those four.

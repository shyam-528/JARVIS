# JARVIS MARK 3 — Roadmap (rebuilt 2026-09-25)

Owner picks (unchanged): proactive first. $0 spend, free-tier brains, no local LLM.
Baseline: **§10 closed, 681 tests green (2m06s), `core/orchestrator.py` lint-clean.**
This file merges `HANDOFF.md` (2026-09-25, §10 close-out) with the independent
review (2026-09-25) — conflicts resolved in favour of HANDOFF where it is newer
(see §8 notes). Items are in build order. Nothing here adds features until P0 is green.

## 1–6. Shipped history (condensed — details in git log)

- **Proactive A+B+C** (2026-09-14): push gate, digest, watchers, safe auto-act (dry-run default ON).
- **Storm-proof answers**: `storm_queue` + `storm_drain`, Telegram `/storm` controls.
- **Memory compounds**: anniversary, prefs tools + API + Telegram, per-project memory.
- **One-tap routines**: `morning_start` / `evening_wrap`, on-demand only.
- **PC deep control**: demo_prep, downloads_organize, screenshot_note, open_app.
- **Voice silence hardening** (2026-09-19): edge-first chain, kokoro explicit-only,
  Speaker2 `last_error`/`status()`, 3 chain-policy regression tests.
- **Ops quick wins** (2026-09-22): token expiry 30d + expiry-aware bootstrap mint,
  mesh refuses empty-key bind + deleted dead `core/a2a.py`, wa_bridge log rotation,
  `db_maintain` job + scheduler catch-up fix, pre-commit gate, `requirements.lock`.
- **§10 hardening waves 0–7** (closed 2026-09-25, HANDOFF): every hot-path module
  carries a contract docstring, loud failure paths, failure-path tests.
  Wave 7 closed the cluster (agent_mesh, agent_team, agent_discovery, events,
  scheduler, proactive_push, notify, knowledge, prompt_opt, project_memory, mail)
  plus orchestrator section map, zero ruff findings in orchestrator, loud `_add_job()`.

## 7. P0 — correctness gaps (DONE 2026-09-25, 250 tests green across affected suites)

- [x] **T3 — failed job registrations visible in product.** `_add_job()` prints and
      returns `False`, but 12 call sites ignore it. On `False`, publish an
      `events.db` event (or `scheduler_last_error` surfaced by `stats()`), plus test.
      Source: HANDOFF T3. This is the only known invisible-failure left from §10.
      DONE: `_add_job` records into `_job_errors` + publishes
      `scheduler_job_failed` (both best-effort, never raise — bare-SimpleNamespace
      callers keep working); `stats()` carries `scheduler_job_errors`;
      `GET /api/tasks/errors` serves it to dashboard/HUD; +5 tests in
      `test_orchestrator_wave7.py`, route parity table updated.
- [x] **T5 — root clutter.** Untracked now: `_toolnames.txt`, `chaining_out/err.txt`,
      `jarvis_upgrade_recommendations.md`, `stark_surprise_for_shyam.txt`
      (HANDOFF-era `orch_*` scratch already cleaned). Delete, move to `docs/`, or
      extend `.gitignore` — review + HANDOFF agree. Acceptance: `git status --short`
      clean apart from intentional work.
      DONE via `.gitignore` (non-destructive): `chaining_*`, `orch_*`, `stark_*`,
      `*_out/_err.txt`, `_toolnames.txt`, `jarvis_upgrade_recommendations.md` now
      ignored — status shows only real work. Deletion left to owner (stark letter
      and upgrade doc are personal content). ALSO FOUND: `wa-bridge/wa-bridge/auth/`
      session creds are TRACKED in git and not covered by the `wa-bridge/auth/`
      ignore rules — noted in `.gitignore`, needs `git rm --cached` (owner call).
- [x] **Corrupt-file recovery for all stores** (split out of WAL quick win).
      preferences.py pattern (quarantine + rebuild) extended to every store init path.
      Source: ROADMAP Medium. Touches many inits — one focused session.
      DONE: shared contract in `utils/json_utils.py` (`load_json_store` with
      timestamped quarantine + loud print + OSError-vs-corrupt split, `save_json_atomic`
      tmp+replace) + migrated all 15 JSON stores (storm_queue, proactive,
      proactive_push mute/count, telegram link state, project_memory, budget_guard
      alerts, single_owner lock, router snapshot, skill_acquisition, persona,
      prompt_opt prompts + per-line JSONL examples, agent_pool customs,
      plugin_registry index, orchestrator auto-act toggles). preferences.py already
      was the reference — untouched. skill_manifest `from_file` intentionally still
      raises (install path, caller-visible). JSONL audit/dedup logs out of scope by
      format. +8 helper tests, +7 recovery tests, +2 auto-act regression tests
      (including a real catch: quarantine must not narrow the fail-soft path for
      partial configs without `db_path`); 4 Wave-7 tests upgraded to the quarantine
      contract; ruff findings on touched files 80 → 57 (net −23, zero new).

## 8. P1 — quality ratchet (a day each, any order after P0)

- [x] **T1 — burn down repo-wide lint: ~1,049 findings → 0.**
      Top: SIM105, I001, E741, W291, B904, B905 (HANDOFF count; the review's "155"
      was F401-only subset). Policy: whenever a file is touched, `ruff check --fix`
      + hand-fix rest, number only goes down. `core/orchestrator.py` at zero is the
      reference. Then **widen the pre-commit gate** beyond E9,F601,F821,F402,F811
      and consider `ruff format` on the tree. (Review's "god-file split" demand is
      explicitly NOT part of this — see §10.)
      DONE 2026-09-25: 993 → 0 findings, `ruff format` applied tree-wide
      (225 files formatted), pre-commit gate widened to full `ruff check` +
      `format --check` (narrow subset retired). Real bugs found by the burn-down:
      E722 bare-excepts in code_exec, B023 loop closures in calendar_feed (refactored
      to module helper), dead REPL `history`/`completer` locals in jarvis.py,
      `{rel}:{line}` content-duplication bug in code_intel find_symbol/find_references
      (now `{rel}:{i}`), expressive.py persona `style` computed but never sent to
      Gemini (now prefixed into contents), 40 unused pygls imports collapsed to a
      probe. Judgment calls: E402 per-file-ignores for the 3 bootstrap entry points
      (venv-guard/sys.path must run first), SIM115 noqa for supervisor's child-log
      lifetime handles, `strict=True` on 5 equal-by-construction zips. SIM105 bulk
      (59 sites) via delegated pass, verified zero + smoke green.
- [x] **Coverage ratchet, not cliff.** Record current coverage, gate CI on
      "never decreases". (Hard `--cov-fail-under=85` rejected — unrealistic for
      Windows/hardware-heavy code, trains junk tests.)
      DONE 2026-09-25: `scripts/coverage_ratchet.py` (check-only in CI, `--update`
      ratchets the baseline up locally, never down) + `scripts/coverage_baseline.json`
      at **51.01%** over core/api/skills/voice/utils (656 green). pytest-cov pinned
      in requirements-dev.txt.
- [x] **Windows-only GitHub Actions CI.** pytest + ruff + 60s boot smoke on push.
      (ubuntu matrix rejected — pycaw/comtypes/tray are Windows-native.)
      DONE 2026-09-25: `.github/workflows/ci.yml` (windows-latest, py3.11, pip cache):
      ruff check + format check + pytest smoke + boot smoke (api import, config load)
      + full suite with coverage ratchet. No secrets needed (suite runs offline).
      Note: full suite takes **~2m06s** (verified HANDOFF baseline). The review's
      earlier ">120s hang" was a misread of the 30s shell cap vs the 126s suite —
      background long runs per HANDOFF §5, don't treat slowness as failure.
- [x] **Unwire dead weight: `todo_app/` + `archive/`.** `todo_app/` (5 files) has zero
      importers outside itself yet is packaged in `pyproject.toml`. Either wire it as
      a real skill or remove from packaging. `archive/` (47 tracked files) + top-level
      `ai-code-reviewer/` stub: untrack, keep in history only. Review + HANDOFF agree.
      DONE 2026-09-25 (remove-from-packaging option): `[project.scripts] todo` entry
      + `todo_app*` packaging include dropped from pyproject.toml (files stay on disk,
      standalone CLI unaffected); `archive/` untracked via `git rm --cached` (47 files,
      working copies kept) + `.gitignore`d; `ai-code-reviewer/` was already untracked
      (only an ignored `.env`). ALSO FOUND: `wa-bridge/wa-bridge/auth/` session creds
      tracked in git, outside the `wa-bridge/auth/` ignore rules — flagged in
      `.gitignore`, needs `git rm --cached` (owner call).

## 9. P2 — operations (after P1, one session each)

- [x] **Metrics surface.** `/api/metrics` (TPM from UsageLedger, p95 TTFT, breaker state
      from `data/router_state.json`) wired into the EXISTING web HUD — no new UI.
      DONE 2026-09-25: `/api/metrics` (which already served host stats) gained a
      best-effort `router` section — usage_today, live TPM, latency p95, breaker
      states with waits, per-key pools (`xkiro#1`/`xkiro#2`, never key material),
      hops + last brain. Never breaks the polling HUD (per-section suppress).
      HUD `loadSystem` renders pools, open breakers, worst p95, embeddings flag in
      the existing drawer (JS syntax verified via node --check). +10 tests in
      `tests/test_metrics_wave8.py` (shape, key grouping, secrecy, partial-orch
      survival, health embeddings). No new routes — parity table untouched.
- [x] **T6 — embedder-off flag.** `knowledge._get_embedder()` announces "FTS-only"
      once; surface `embeddings: off` in `/health` or `Orchestrator.stats()` so a
      fastembed break can't silently degrade search.
      DONE 2026-09-25 with the metrics work: `KnowledgeStore.embedder_status()`
      (on/off/not-tried-yet) → `Orchestrator.stats()["embeddings"]` (getattr-safe
      for partial stubs) → `/api/stats` passthrough + `/api/health` section.
- [x] **Split requirements.** `requirements.core.txt` (boot <10s) / `voice.txt` /
      `ml.txt`. 300MB of ML deps (faster-whisper/kokoro/fastembed/
      sentence-transformers) shouldn't be mandatory; lazy-import where not already.
      DONE 2026-09-25: audit showed heavy imports were ALREADY function-level
      everywhere except top-level `numpy` (kept in core deliberately — knowledge.py
      needs it at import; documented in the header). `requirements.core/voice/ml.txt`
      written (46-package union verified identical to the old set),
      `requirements.txt` kept as the full set via `-r` includes (zero breakage),
      `tests/test_req_split.py` gates it 3 ways (no hard voice/ML imports outside
      domain files, EXEMPT list currency, tier-union equality).
- [x] **Provider-chain de-duplication (review finding).** 51 provider entries
      shared ~5 keys (35 Xkiro on one key): on throttle, 35 hops hammered the same
      quota before reaching Google/NVIDIA.
      MITIGATED 2026-09-25 by owner: second XKIRO key added, split 20/15 across
      `XKIRO_API_KEY` + `XKIRO_API_KEY_2` (both set, verified in config + env).
      Worst-case single-quota stall halved; `/api/metrics` key_pools now shows the
      two pools balancing live. Full architectural fix (failover grouped by
      key/quota instead of model row) remains optional — only if throttles still
      stall after this split proves itself in production.
- [x] **Chaos harness.** Kill API child + wa-bridge mid-turn, revoke GROQ key
      mid-stream, disk 99% full, corrupt a .db-wal. Expect: AllProvidersDown →
      storm_queue, supervisor backoff restart, telegram_owner.lock release.
      DONE 2026-09-25 as `scripts/chaos_harness.py` (6/6 green, temp dirs only,
      no network, no data/, CI-excluded by location): key-revoked hop with auth
      breaker, outage queue preservation + mid-outage corruption quarantine,
      garbage scheduler.db quarantine + fresh boot, stale/dead/corrupt lock
      takeover, failed-save byte-identical fallback, killed-child poll detection
      + quiet cleanup. REAL CATCH: garbage scheduler.db raised DatabaseError out
      of the constructor — the orchestrator builds it in `__init__` with no
      fallback, so one corrupt file killed the whole brain at boot. Fixed with
      the SQLite quarantine contract (`_quarantine_db`: close handle, move
      db+-wal+-shm aside loud, reconnect fresh) + regression test. Estimate was
      2+ days; took one session because §10/P0 had already built the contracts
      the drills exercise.

## 10. Parked — do NOT schedule (split only when a feature or bug forces it)

Per HANDOFF T2 + ARCHITECTURE.md §9 "Parked debt": `orchestrator.py` (~2.1k lines,
63 methods), `commands.py` (~2k lines, 224 literals in one dispatch),
`agent_mesh.py` (~990), `telegram.py` (~865), `skills/code_agent_skills.py` (~680).
Ready-made split directions live in ARCHITECTURE.md. Rule: only split when a real
feature or bug forces it — premature splits on working solo code create risk
without payoff. The review's "split the god files now" is overruled for this reason.
- [ ] Provider template expansion in config.py — CRITICAL: YAML comments carry live
      probe metadata ("probed 2026-09-19, do NOT move below priority 30 without
      re-probing"). Any collapse must preserve that history or it destroys knowledge.

## 11. Rejected (with reasons — don't re-add)

- ✗ wa-bridge:3101 LAN exposure — FALSE, binds `127.0.0.1` (server.js).
- ✗ `--cov-fail-under=85` hard gate; ✗ ubuntu CI matrix; ✗ web/ → Vite+TS;
      ✗ review's feature wishlist (greeting variety, budget dashboard) — features
      stay on hold per the priority directive below.

## 12. Done (history index)

P0 stability · single-owner runtime · briefings · dev loop · chain-collapse fix ·
calendar · mail + background triage · voice/TTS + auto-speak · automations + rule CLI ·
WhatsApp bridge · HUD/orb · budget guardrails · memory consolidation · sticky retry ·
read-only no-approval · bg wait-for-web · greeting + focus + autoscroll ·
Brains (2026-09-19 probe): 35 Xkiro free in chain, Zen *-free disabled (403 gated),
NIM last-resort (17–75s TTFT), Groq fast tier.

# --- Priority directive (2026-09-19, still in force) ---
# Before adding new JARVIS-style features (barge-in, HUD, wake greeting,
# cross-device continuity), finish P0–P1 above.
# Feature ideas are on hold until the user confirms the major issue is fixed.

---

## Scorecard — where we are vs where this roadmap lands

Current (honest, post-§10): **6.9/10 overall**
(solid system, P0–P1 debt still open; review's 6.4 predates the §10 close-out,
external 7.5 product / 6.0 maintainability sits between).

Update 2026-09-25 (P0 done, P1 T1 + coverage + CI + unwire done, P2 open):
**7.5/10** — code quality 6.5→8.0 (lint zero tree-wide, verified), testing
7.5→8.0 (656 green + ratchet + CI), maintainability 6.0→7.0 (dead weight
unwired, gate widened; god files still solo-sized), reliability 7.0→7.5
(T3 + quarantine; chain de-dup still pending in P2). The 8.3 target needs P2.

Update 2026-09-25 evening (P2 done: metrics, T6, key-split mitigated, req
split, chaos 6/6): **8.0/10** — reliability 7.5→8.0 (scheduler boot-kill fixed,
disaster paths demonstrated), testing 8.0→8.5 (ratchet + CI + chaos + split
gate). Remaining gap to 8.3+ is structural (parked god-file splits,
Windows-only, free-tier dependence) — deferred by design until a feature or
bug forces it.

| Dimension | Now | After P0–P2 | Why it's credible |
|---|---|---|---|
| Features | 8.5 | 9.0 | chain de-dup + metrics make existing features actually dependable |
| Reliability | 7.0 | 8.5 | T3 closes the last invisible failure; chain de-dup removes the throttle trap |
| Architecture | 6.0 | 7.5 | parked splits cap this — 7.5 is the honest ceiling without them |
| Code quality | 6.5 | 8.5 | orchestrator already zero; T1 takes the whole tree there |
| Maintainability | 6.0 | 8.0 | lint-zero + CI + dead-weight removal; god files still solo-sized |
| Security | 7.0 | 8.5 | expiry/mesh/rotation done; CI + ratchet keep it there |
| Testing | 7.5 | 8.5 | 681 green today; ratchet + CI + chaos harness lock it |
| **Overall** | **6.9** | **8.3** | a dependable solo system, not a demo — 10s are for teams with packaging + local-first, which is out of scope by owner pick |

Promise kept deliberately narrow: 8.3, not 10. The remaining gap is structural
(Windows-only, free-tier-dependent, parked god files) and can only close with the
§10 parked splits + packaging work the owner has explicitly deferred. Everything
in P0–P2 is verified-buildable from the HANDOFF baselines — no new inventions required.

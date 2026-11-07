# HANDOFF — mark3 `§10` hardening program

**Session date:** 2026-09-25 (git timestamps run a little behind — the newest commit
in the log is dated 2026-09-24).
**Status:** §10 **closed**. Wave 7 complete, section map + parked-debt note landed,
orchestrator lint-clean, full suite green.

---

## 1. TL;DR for the next session

- **§10 is done.** Seven waves, every module passed the same gate (see §2). The wave
  table + per-wave summaries live in `ARCHITECTURE.md` → `## 10. Hardening waves`.
- **This session did:** finished the Wave 7 cluster (`knowledge`, `prompt_opt`,
  `project_memory`, `mail`), ran the full suite (675 green), recorded Wave 7 in
  `ARCHITECTURE.md`, added the **section map** to `core/orchestrator.py`'s module
  docstring, **removed all 35 ruff findings** in that file, replaced 12 silent
  `try/except: pass` job registrations with a loud `_add_job()` helper, and added
  `tests/test_orchestrator_wave7.py` → final suite **681 passed, 0 failed**.
- **Nothing is left half-finished.** No known broken state, no failing tests, no
  uncommitted work except what this file's own commit brings.

## 2. What "the §10 gate" means (apply it to any new module)

Per module, all six must hold:

1. contract docstring at the top (what it owns, what it must never do);
2. no silent failures — fail-soft is fine, **fail-invisible is not**; an aux subsystem
   may swallow only where the docstring says so;
3. failure paths have tests;
4. boot smoke still passes (`tests/test_boot.py`, `tests/test_chaining.py`);
5. no dead references (imports, tool registrations, provider names);
6. suite green + `ruff check`/`ruff format --check` clean **for the files you touched**.

## 3. Wave 7 — the hot-path cluster (11 modules, +74 tests)

| Commit | Modules | Headline |
|---|---|---|
| `256bf6b` | `core/agent_mesh.py` | loud wire failures, dead-code removal, bug fixes |
| `f033ff4` | `core/agent_team.py`, `core/agent_discovery.py`, `core/events.py` | loud subscription/mesh failure paths |
| `f153fe8` | `core/scheduler.py` | bad cron expr → loud + `last_error`; corrupt stored rows skipped loud per row; retry failures printed; WAL pragma + migration loud |
| `ae7980a` | `core/proactive_push.py`, `core/notify.py` | corrupt mute/count/dedup state loud (preference-loss class); notify verified fail-loud by design |
| `0114d76` | `core/knowledge.py`, `core/prompt_opt.py`, `core/project_memory.py`, `core/mail.py` | per-file extraction failures loud; corrupt prompts/examples loud; **corrupt project memory was a silent total wipe** → now loud; failed `_save` reports `ok: False` |
| `990ac10` | `ARCHITECTURE.md` | Wave 7 row; §10 declared closed (675 green) |
| *(this commit)* | `core/orchestrator.py`, `ARCHITECTURE.md`, `tests/` | section map, lint-cleanup, loud `_add_job()`, parked-debt table |

## 4. How to verify the whole thing (copy-paste)

```powershell
cd C:\Users\krith\mark3
.venv\Scripts\python -m pytest tests/ -q            # ~2m40s → background it, see §5
.venv\Scripts\python -m ruff check core skills api voice tests
.venv\Scripts\python -m ruff format --check <files you touched>
```

Baseline: **675 passed** before the orchestrator work → **681 passed (verified, 0 failed,
2 m 06 s)** after it. `+6` come from `tests/test_orchestrator_wave7.py`. Related-suite
checks run earlier in the same session: `test_knowledge.py test_p10_rag.py
test_offline_draft.py test_memory_compounds.py test_*_wave7.py` → 56 passed;
`test_boot.py test_chaining.py test_automation.py test_bg_wait.py
test_phase1_briefing.py test_p9_notify_scheduler.py test_orchestrator_wave7.py`
→ 46 passed.


## 5. Environment quirks that cost time (read before you fight them)

- **Shell commands are capped at ~30 s.** Any pytest run longer than that must be
  backgrounded, then polled:
  ```powershell
  Start-Process -FilePath .venv\Scripts\python.exe -ArgumentList '-m','pytest','tests/','-q' `
    -RedirectStandardOutput full_out.txt -RedirectStandardError full_err.txt -NoNewWindow
  # …later…
  Get-Content full_out.txt | Select-Object -Last 3
  ```
  Delete the `*_out.txt`/`*_err.txt` scratch files afterwards (they are untracked).
- **PowerShell + UTF-8:** reading a UTF-8 file with `Get-Content` mojibakes `✅`/`—`
  in the console. The file on disk is fine — verify with `read_files`, not `Get-Content`.
- **`ruff` config is in `pyproject.toml`** (`select = E,F,I,W,UP,B,C4,SIM`,
  `ignore = E501`, `line-length = 100`). But the **pre-commit gate only enforces
  `E9,F601,F821,F402,F811`**, so "the commit passed" ≠ "the file is lint-clean".
  Always run `ruff check <file>` yourself; it prints the count with `--statistics`.
- **Run `ruff format` after manual edits.** Manually wrapped call arguments get
  re-wrapped, so `--check` fails even when `check` passes.
- **Editor tool limits:** `new_text` over ~6,000 chars is rejected (split the write);
  `old_text` must be non-null for existing files; if a snippet matches twice, add
  surrounding context.
- **Tests:** `tests/conftest.py` has an autouse `no_embeddings` fixture
  (`JARVIS_NO_EMBEDDINGS=1`) and asyncio auto mode, so a bare `async def test_…`
  works without a decorator.
- **Simulating IO failures on Windows:** there is no usable `chmod`, so monkeypatch
  `Path.read_text` / `Path.replace` / `Path.write_text` to raise — that is exactly how
  the Wave 7 failure-path tests do it.
- **`git commit` runs the pre-commit hooks** (ruff gate + pytest smoke). If a hook
  fails, the commit does not happen — read its output before retrying.

## 6. Future todos (nothing here is urgent; §10 is closed)

**T1 — Burn down the repo-wide lint baseline.** `ruff check . --statistics` reports
~1,049 findings. Top rules: `SIM105`, `I001`, `E741`, `W291`, `B904`, `B905`.
Cheap policy: whenever you touch a file for another reason, `ruff check --fix` it and
hand-fix the rest, so the number only ever goes down. `core/orchestrator.py` is already
at zero (this session) — use it as the reference.

**T2 — God files (deliberately parked, do NOT schedule).** The table lives in
`ARCHITECTURE.md` → §9 "Parked debt", with ready-made directions for
`orchestrator.py` (~2,360), `commands.py` (~2,190), `agent_mesh.py` (~990),
`telegram.py` (~865), `skills/code_agent_skills.py` (~680). Rule from that section:
only split when a real feature or bug forces it.

**T3 — Make failed job registrations visible in the product, not just the console.**
`Orchestrator._add_job()` now prints and returns `False`, but its 12 call sites ignore
the return value. If a boot job ever fails to register, the dashboard/HUD and the
`/tasks` view still show nothing. Follow-up: on `False`, publish an `events.db` event
(or add a `scheduler_last_error` field surfaced by `stats()`), plus a test.

**T4 — Keep the new orchestrator invariants guarded.** `tests/test_orchestrator_wave7.py`
asserts (a) the section map exists, (b) `start()` never calls `scheduler.add_task`
directly again, (c) `__init__` uses `_add_job`. Extend these when the boot table grows.

**T5 — Decide the fate of stray files in the repo root.** Untracked at handoff:
`_toolnames.txt`, `chaining_out.txt`, `chaining_err.txt`,
`jarvis_upgrade_recommendations.md`, `stark_surprise_for_shyam.txt`. Either delete,
move to `docs/`, or add to `.gitignore` — they are clutter, not code.

**T6 — Idea, only if it ever bites:** `knowledge._get_embedder()` announces "FTS-only"
once on first use; if search quality silently regressed after a `fastembed` break,
`/health` or `Orchestrator.stats()` could carry an `embeddings: off` flag.

**T7 — Teammate delegation was unavailable this whole session** (`INFERENCE_CAP_ERROR`,
HTTP 429, ~22 h reset). Wave 7 was done solo. If you want parallel work again, check
`team_status` and retry delegation at the start of the next session.

## 7. Where to look next

1. `ARCHITECTURE.md` → §10 wave table (what was hardened, and why) and §9 "Parked debt".
2. The **module docstring** of whatever you are about to edit — every module now starts
   with its contract, and `core/orchestrator.py` also carries a section map so you can
   jump straight to the right region.
3. `tests/test_*_wave7.py` — the failure-path tests from this program are the fastest
   way to learn what each module promises when things go wrong.

"""JARVIS MARK 3 — CLI entry point (the boot spine).

Contract:
  - Boot order: venv re-launch guard -> config -> orchestrator -> optional
    greeter -> unified bring-up (API child + HUD + WhatsApp bridge under
    core.supervisor, then Telegram ownership) -> REPL. Every optional
    subsystem fails soft with a printed reason; a failed optional NEVER
    aborts boot or the chat loop.
  - Single-owner rules: the API child claims Telegram via
    ``core.single_owner``; this CLI polls only if the child failed to come
    up. Exactly one poller process — never dual getUpdates.
  - Shutdown (finally): terminate ALL supervised children (every watchdog
    generation via ``API_PROC_REFS``), stop the telegram bot, release the
    ownership lock, close the orchestrator. No orphan processes, no
    tracebacks on Ctrl+C.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path

# ── Venv guard: re-launch in the project venv if started with another Python ──
# NOTE: os.execv breaks console ownership on Windows (the shell prompt returns
# while the child keeps running, stealing stdin). subprocess.run hands the
# console to the child properly and the parent waits for it.
_VENV_PYTHON = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
if (
    os.name == "nt"
    and _VENV_PYTHON.exists()
    and os.environ.get("JARVIS_VENV_RELAUNCHED") != "1"
    and Path(sys.executable).resolve() != _VENV_PYTHON.resolve()
):
    os.environ["JARVIS_VENV_RELAUNCHED"] = "1"
    import signal
    import subprocess

    # Parent shields SIGINT while waiting: on Windows Ctrl+C goes to EVERY
    # process on the console, so without this the parent's WaitForSingleObject
    # raises KeyboardInterrupt with a full traceback even though the child
    # already shut down gracefully ([Mesh] Stopped). Child installs its own
    # SIGINT handler at startup, so it still receives Ctrl+C normally.
    _prev_sigint = signal.getsignal(signal.SIGINT)
    try:
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        rc = subprocess.run([str(_VENV_PYTHON)] + sys.argv).returncode
        raise SystemExit(rc)
    except KeyboardInterrupt:
        # Paranoia net (e.g. SIGBREAK): never dump a traceback for Ctrl+C.
        raise SystemExit(130) from None
    finally:
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, _prev_sigint)

from rich.console import Console
from rich.panel import Panel

from core.commands import (
    COMMAND_HELP,
    HELP,
    dispatch_slash,
    print_chain,
    print_keys,
    resolve_alias,
    set_voice_session_getter,
)
from core.config import load_config
from core.logging_config import get_logger, setup_logging
from core.orchestrator import Orchestrator
from core.router import AllProvidersDown
from core.supervisor import (
    API_PROC_REFS,
    api_watchdog,
    aterminate_children,
    reclaim_api_port,
    spawn_api_child,
    spawn_hud_child,
    spawn_wa_child,
    wa_wait_ready,
    wa_watchdog,
    wait_port_bound,
)
from skills.nl_shortcuts import try_mail_shortcut, try_pc_shortcut, try_routine_shortcut

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.enums import EditingMode
    from prompt_toolkit.history import FileHistory

    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

console = Console()
logger = get_logger("jarvis")


def create_prompt_session():
    """Create a prompt_toolkit session with history, autocomplete, and key bindings."""
    if not HAS_PROMPT_TOOLKIT:
        return None
    try:
        # NOTE: history + completer are constructed inline in the PromptSession
        # call below (FileHistory + CommandCompleter) — no locals needed here.

        # Create key bindings for vi/emacs mode (EditingMode from top-level import)
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        # Add vi mode toggle (Alt+V)
        @kb.add("escape")
        def _(event):
            event.app.vi_state.input_mode = event.app.vi_state.input_mode ^ 1

        # Add emacs mode toggle (Alt+E)
        @kb.add("escape", "e")
        def _(event):
            event.app.editing_mode = EditingMode.EMACS

        # Add vi mode toggle (Alt+V)
        @kb.add("escape", "v")
        def _(event):
            event.app.editing_mode = EditingMode.VI

        # Add Ctrl+L to clear screen
        @kb.add("c-l")
        def _(event):
            event.app.renderer.clear()

        # Add Ctrl+D to exit
        @kb.add("c-d")
        def _(event):
            event.app.exit()

        # Add Ctrl+C to interrupt
        @kb.add("c-c")
        def _(event):
            event.app.exit(exception=KeyboardInterrupt())

        # Add Alt+Enter for multi-line mode
        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.newline()

        # Add Ctrl+Space for auto-completion
        @kb.add("c-space")
        def _(event):
            b = event.current_buffer
            if b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=False)

        # Enhanced completer with command descriptions
        from prompt_toolkit.completion import Completer, Completion

        class CommandCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor.lower()
                words = text.split()

                # If we're at the first word, complete commands
                if " " not in text or text.endswith(" "):
                    for cmd in COMMAND_HELP:
                        if cmd.startswith(text):
                            yield Completion(
                                cmd,
                                start_position=-len(text),
                                display_meta=COMMAND_HELP.get(cmd, ""),
                            )
                else:
                    # Subcommand completion for specific commands
                    first_word = words[0] if words else ""
                    if first_word in (
                        "/plugin",
                        "/screenshot",
                        "/ocr",
                        "/readscreen",
                        "/analyze",
                    ):
                        # Could add subcommand completion here
                        pass

        # Use enhanced completer
        cmd_completer = CommandCompleter()

        session = PromptSession(
            history=FileHistory(Path.home() / ".jarvis_history"),
            completer=cmd_completer,
            auto_suggest=AutoSuggestFromHistory(),
            complete_while_typing=True,
            key_bindings=kb,
            editing_mode=EditingMode.VI,  # Default to vi mode
            enable_history_search=True,
            mouse_support=False,
            patch_stdout=True,  # background agent prints render above the prompt safely
        )
        return session
    except Exception:
        # Fallback if prompt_toolkit can't initialize (e.g., no console)
        return None


async def chat_turn(orch: Orchestrator, text: str, surface: str = "cli") -> str:
    current: str | None = None
    raw: list[str] = []  # verbatim chunks (return value: speech tags intact)
    shown: list[str] = []  # display chunks (speech tags filtered)
    yielded_any = False
    # Voice surface: the brain appends a single-shot <spoken> tag for TTS —
    # speak it, don't print it.
    tag_filter = None
    if surface == "voice":
        try:
            from voice.distiller import SpokenTagFilter as _TagFilter

            tag_filter = _TagFilter()
        except Exception:
            tag_filter = None
    try:
        from cli_render import render_reply, render_tool_chunk
    except Exception:
        render_reply = None  # type: ignore[no-redef]
        render_tool_chunk = None  # type: ignore[no-redef]
    _turn_start = time.perf_counter()
    try:
        with console.status("[cyan]thinking…[/]", spinner="dots") as _status:
            async for provider, chunk in orch.process(text, surface=surface):
                yielded_any = True
                if provider == "bg_dispatch":
                    console.print(f"[cyan]{chunk}[/]")
                    continue
                if provider == "tool":
                    if isinstance(chunk, str) and render_tool_chunk is not None:
                        render_tool_chunk(console, chunk)
                    else:
                        console.print(f"[dim]{chunk}[/]")
                    continue
                if provider in ("proactive", "ml_proactive", "auto"):
                    continue
                if provider != current:
                    if current is not None:
                        console.print()
                        console.print(f"[yellow]↻ rerouted → {provider}[/]")
                    console.print(f"[dim]‹{provider}›[/]", end=" ")
                    current = provider
                if isinstance(chunk, str):
                    raw.append(chunk)
                    if tag_filter is not None:
                        shown.append(tag_filter.feed(chunk))
                    else:
                        shown.append(chunk)
                try:
                    _elapsed = time.perf_counter() - _turn_start
                    _status.update(f"[cyan]thinking… {_elapsed:.0f}s[/]")
                except Exception:
                    pass
        if tag_filter is not None:
            shown.append(tag_filter.flush())
        display = "".join(shown).strip()
        if display:
            if render_reply is not None:
                render_reply(console, display)
            else:
                print(display)
        if not yielded_any:
            console.print("[dim]empty reply — providers gave nothing. Try again or check /chain[/]")
    except AllProvidersDown as exc:
        print()
        console.print(f"[red]all brains unavailable →[/] {exc}")
        try:
            from core.storm_queue import StormQueue

            res = StormQueue(orch.cfg.db_path.parent).enqueue("cli", text)
            if res.get("queued"):
                console.print(
                    f"[cyan]⛈ queued ({res['id']}) — I'll deliver to Telegram + voice when quota returns.[/]"
                )
        except Exception:
            pass
        try:
            _draft = orch.offline_draft(text)
            console.print(f"[yellow]{_draft}[/]")
            return _draft
        except Exception:
            pass
        console.print("[dim]Tip: Try again in a moment, or check your API keys with /keys[/]")
        return ""
    except Exception as exc:
        logger.exception("Error during chat turn")
        console.print(f"[red]Unexpected error:[/] {exc}")
        console.print("[dim]Tip: Try /reset to clear session, or check /keys for API key status[/]")
    return "".join(raw).strip()


async def main() -> int:
    wake_mode = "--wake" in sys.argv
    cfg = load_config()

    # Setup logging
    log_level = "DEBUG" if "--debug" in sys.argv else "INFO"
    setup_logging(level=log_level, log_file="jarvis.log")
    logger.info("Starting JARVIS MARK 3")

    console.print(
        Panel.fit(
            f"[bold cyan]{cfg.jarvis_name}[/]\n"
            "the brain that never runs dry\n"
            f"[dim]{time.strftime('%a %d %b %Y')} · type /help for commands[/]",
            border_style="cyan",
            title="◈ online",
            title_align="right",
        )
    )
    if not cfg.providers:
        console.print("[red]no usable API keys found.[/]")
        print_keys()
        console.print("1) copy [bold].env.example[/] to [bold].env[/]")
        console.print(
            "2) paste free keys from opencode.ai/auth · build.nvidia.com · console.groq.com · aistudio.google.com"
        )
        return 1
    orch = Orchestrator(
        cfg,
        on_event=lambda name, err: console.print(f"[dim]· hop: {name} → {str(err)[:140]}[/]"),
    )
    await orch.start()
    voice_session = None
    telegram_bot = None

    # Unlock greeting (roadmap step 1): "welcome back, sir" on every
    # workstation unlock, rate-limited, quiet-hours aware. Best-effort.
    _greeter = None
    try:
        if getattr(cfg.voice, "greeting_enabled", False):
            from core.session_greeting import UnlockGreeter, greet_once

            _greeter = UnlockGreeter(
                on_unlock=lambda: greet_once(
                    orch, console, quiet_spec=getattr(cfg.voice, "greeting_quiet_hours", "22-7")
                ),
                min_interval_min=getattr(cfg.voice, "greeting_min_interval_min", 60.0),
                quiet_spec=getattr(cfg.voice, "greeting_quiet_hours", "22-7"),
            )
            _greeter.start(asyncio.get_running_loop())
    except Exception as exc:
        console.print(f"[dim]unlock greeting disabled: {exc}[/]")

    def get_voice_session():
        nonlocal voice_session
        if voice_session is None:
            from voice.session import VoiceSession

            v = cfg.voice
            voice_session = VoiceSession(
                orch,
                console,
                whisper_model=v.whisper_model,
                tts_voice=getattr(v, "piper_voice", "en_GB-alan-medium"),
                edge_voice=v.tts_voice,
                tts_enabled=v.tts_enabled,
                tts_engine=getattr(v, "tts_engine", "auto"),
                silence_s=v.silence_timeout_s,
                max_record_s=v.max_record_s,
                speak_max_chars=v.speak_max_chars,
                wake_threshold=v.wake_threshold,
                stt_language=v.stt_language,
                tts_persona=getattr(v, "tts_persona", ""),
                gemini_voice=getattr(v, "gemini_voice", "Kore"),
            )
        return voice_session

    set_voice_session_getter(get_voice_session)

    telegram_bot = None

    def get_telegram_bot():
        nonlocal telegram_bot
        if telegram_bot is None:
            from pathlib import Path

            from core.telegram import TelegramBot

            tg_cfg = cfg.telegram
            if tg_cfg.enabled and tg_cfg.bot_token and tg_cfg.chat_id:
                telegram_bot = TelegramBot(
                    bot_token=tg_cfg.bot_token,
                    orch=orch,
                    state_path=Path(cfg.db_path).parent / "telegram_bot.json",
                    poll_timeout=30,
                )
            return telegram_bot
        return telegram_bot

    print_keys()
    print_chain(orch)
    # One status line (was three scattered dims): brains · background · session.
    try:
        _rows = list(orch.chain_status())
        _ready = sum(1 for r in _rows if len(r) > 3 and r[3] == "READY")
        _brains = f"{_ready}/{len(_rows)} brains"
    except Exception:
        _brains = "brains"
    # Auto-resume last conversation (count used in the status line below).
    last_msgs = orch.load_latest_session()
    _bg = "ON" if orch.auto_background else "OFF"
    _resumed = f" · resumed {len(last_msgs)} msgs" if last_msgs else ""
    console.print(f"[dim]● {_brains} · background {_bg}{_resumed} · /bgmode to toggle[/]")

    # ── Unified bring-up: Telegram + API server + website, all in THIS process ──
    # One brain, one Telegram poller. The embedded API server shares the
    # orchestrator and serves the jarvis-web dashboard on :8080.
    api_proc = None  # stays None in brain-only mode; keeps the finally-block safe
    api_owns_telegram = False
    _api_port = int(os.environ.get("JARVIS_API_PORT", "8080"))
    _root = Path(__file__).resolve().parent
    try:
        import socket as _sock

        with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
            _s.settimeout(0.5)
            api_owns_telegram = _s.connect_ex(("127.0.0.1", _api_port)) == 0
    except Exception:
        api_owns_telegram = False

    if api_owns_telegram:
        # Port conflict recovery: if the holder is one of OUR orphaned
        # uvicorn children (no parent CLI), take the port back; otherwise
        # this CLI stays brain-only and defers.
        reclaimed = reclaim_api_port(console)
        api_owns_telegram = not reclaimed

    # Boot services at a glance: bring-up appends rows, one table prints
    # before HELP (display-only — no behavior change).
    svc: list = []
    if api_owns_telegram:
        console.print(f"[yellow]Another service holds :{_api_port}.[/]")
        svc.append(
            (
                "API + dashboard",
                None,
                "held by another instance — Telegram + website stay there; this CLI runs brain-only",
            )
        )
    else:
        # P1 single-owner: spawn API child FIRST, then decide Telegram owner.
        # Old order (CLI bot -> child) caused dual getUpdates + 409 Conflict.
        # New: child binds :8080 and claims telegram_owner.lock; CLI only
        # polls if child failed to come up.
        # 1) API server + website on :8080 as managed CHILD PROCESS.
        # Embedded-mode threading proved unreliable under the CLI's REPL
        # (Windows console loop starves the daemon thread), so we run the
        # API as a managed CHILD PROCESS instead. The child owns Telegram
        # polling; this CLI then skips its own poller (single owner).
        # Phase B: the child is SUPERVISED — a watchdog restarts it if it
        # dies (crash, OOM, port hiccup) with exponential backoff.
        try:
            api_proc = spawn_api_child(_root)
            API_PROC_REFS.append(api_proc)
            bound = await wait_port_bound(_api_port)
            if bound:
                svc.append(("API + dashboard", True, "http://127.0.0.1:8080"))
            else:
                svc.append(("API + dashboard", None, "child did not bind in time"))

            # Desktop HUD overlay (step B): lightweight tkinter card reading
            # shared SQLite state — no auth, no second orchestrator. Tracked
            # in API_PROC_REFS so it dies with the CLI.
            spawn_hud_child(_root, console, enabled=getattr(cfg.tray, "hud", True))

            # Watchdog: keep the API child alive. Restarts on death with
            # backoff (2s -> 4s -> 8s -> 30s cap); gives up after 5 tries
            # in a 10-min window (repeated crashes = something deeper).
            # NOTE: the CURRENT child is tracked via this dict — the
            # original api_proc local goes stale after the first restart.
            api_state = {"proc": api_proc, "restarts": 0, "window_start": time.time()}
            asyncio.create_task(api_watchdog(console, api_state, _api_port, _root))
        except Exception as exc:
            svc.append(("API + dashboard", False, str(exc)[:100]))

        # WhatsApp bridge as a managed CHILD PROCESS (same supervision as API).
        # Skipped when: already bound (user runs it manually), dir missing,
        # or JARVIS_NO_WA_BRIDGE=1. Dies with the CLI (tracked in API_PROC_REFS).
        try:
            import socket as _wsock

            _wa_port = int(os.environ.get("WA_BRIDGE_PORT", "3101"))
            _wa_bound = False
            try:
                with _wsock.socket(_wsock.AF_INET, _wsock.SOCK_STREAM) as _s:
                    _s.settimeout(0.5)
                    _wa_bound = _s.connect_ex(("127.0.0.1", _wa_port)) == 0
            except Exception:
                pass
            _wa_dir = _root / "wa-bridge"
            _wa_server = _wa_dir / "server.js"
            _wa_skip = os.environ.get("JARVIS_NO_WA_BRIDGE", "") == "1"

            if _wa_bound:
                svc.append(("WhatsApp bridge", None, "already running (external)"))
            elif _wa_skip:
                svc.append(("WhatsApp bridge", None, "supervision skipped (JARVIS_NO_WA_BRIDGE=1)"))
            elif not _wa_server.exists():
                svc.append(
                    ("WhatsApp bridge", False, "not installed (wa-bridge/server.js missing)")
                )
            else:
                _wa_proc = spawn_wa_child(_wa_dir, _wa_port)
                API_PROC_REFS.append(_wa_proc)
                if await wa_wait_ready(_wa_port):
                    svc.append(("WhatsApp bridge", True, ":3101 (scan QR on first run)"))
                else:
                    svc.append(
                        ("WhatsApp bridge", None, "did not answer (check logs/wa_bridge.log)")
                    )
                _wa_state = {"proc": _wa_proc, "restarts": 0, "window_start": time.time()}
                asyncio.create_task(wa_watchdog(console, _wa_state, _wa_dir, _wa_port))
        except Exception as exc:
            svc.append(("WhatsApp bridge", False, str(exc)[:100]))

        # Telegram single-owner (P1): API child claimed lock at boot.
        # CLI polls only if child failed — never dual-poll.
        api_up = api_proc is not None and api_proc.poll() is None
        if api_up:
            svc.append(("Telegram", True, "polled by the API server (single owner)"))
        else:
            telegram_bot = get_telegram_bot()
            if telegram_bot:
                console.print("[dim]API child down — CLI taking Telegram ownership...[/]")
                try:
                    from pathlib import Path as _P

                    from core.single_owner import acquire

                    if acquire(_P(cfg.db_path).parent, "cli"):
                        await telegram_bot.start()
                        svc.append(("Telegram", True, "bot online (CLI owner)"))
                    else:
                        svc.append(("Telegram", None, "owned elsewhere — polling skipped"))
                        telegram_bot = None
                except Exception as exc:
                    svc.append(("Telegram", False, str(exc)[:100]))
                    telegram_bot = None

    from cli_render import print_help_table, print_services

    print_services(console, svc)
    print_help_table(console, HELP)
    # Always-on wake word (step A): run the "Hey Jarvis" listener as a
    # background task so the terminal REPL stays fully usable. --wake still
    # runs the classic foreground mode.
    if not wake_mode and getattr(cfg.voice, "wake_autostart", False):

        async def _wake_bg():
            try:
                await get_voice_session().run_wake_mode()
            except Exception as exc:
                console.print(f"[dim]wake listener off: {str(exc)[:120]}[/]")

        try:
            asyncio.create_task(_wake_bg())
            console.print("[dim]wake word armed — say 'Hey Jarvis' anytime[/]")
        except Exception as exc:
            console.print(f"[dim]wake autostart failed: {str(exc)[:120]}[/]")

    if wake_mode:
        try:
            await get_voice_session().run_wake_mode()
        except ImportError as exc:
            console.print(f"[red]voice deps missing:[/] {exc} → pip install -r requirements.txt")
        except Exception as exc:
            console.print(f"[red]wake mode failed:[/] {exc}")

    # Create prompt session for better input handling
    prompt_session = create_prompt_session() if HAS_PROMPT_TOOLKIT else None

    # Background agent job completion notifications (patch_stdout keeps the prompt safe)
    async def _notify_job_complete(task) -> None:
        icon = {"done": "✅", "failed": "❌"}.get(task.status, "·")
        dur = f"{task.duration_s:.0f}s"
        if task.status == "done":
            body = (task.result or "").strip()
            console.print(f"\n[green]{icon} job {task.task_id} ({task.agent}, {dur}) finished:[/]")
            # Print the full result inline — no /result round-trip needed.
            # Cap only truly huge outputs (whole-file dumps etc).
            if len(body) > 4000:
                console.print(body[:4000], markup=False, highlight=False)
                console.print(
                    f"[dim]... truncated ({len(body)} chars) — /result {task.task_id} shows the same[/]"
                )
            else:
                console.print(body or "(empty)", markup=False, highlight=False)
        else:
            console.print(
                f"\n[red]{icon} job {task.task_id} ({task.agent}, {dur}) FAILED:[/] {task.error[:200]}"
            )
        # Feed the result back into the main brain whichever way it ended.
        # Single-delivery: the print above IS the delivery — note_bg_completion
        # marks it shown so the next turn files a short receipt, never the
        # full text again.
        with contextlib.suppress(Exception):
            orch.note_bg_completion(task)

    orch.agent_pool.on_complete = _notify_job_complete

    try:
        while True:
            try:
                if prompt_session:
                    user = await prompt_session.prompt_async("You › ")
                else:
                    # Non-blocking input: a raw input() on piped stdin would
                    # freeze the whole event loop (mesh/API handlers included).
                    user = await asyncio.to_thread(console.input, "[bold green]You › [/]")
            except EOFError:
                break
            cmd = user.strip()
            if not cmd:
                continue

            # Resolve command alias
            first_word = cmd.split()[0].lower() if cmd.split() else ""
            canonical_cmd = resolve_alias(first_word)

            # Reconstruct command with canonical first word
            low = cmd.lower()
            if first_word != canonical_cmd:
                cmd = canonical_cmd + cmd[len(first_word) :]
                low = cmd.lower()

            if low in ("/exit", "/quit", "/bye"):
                break
            # A slash handler must never kill the session: a raising command
            # used to escape this loop straight into teardown ("every slash
            # command stops the mesh"). Fail loud, stay alive.
            try:
                if await dispatch_slash(orch, cmd):
                    continue
            except Exception as exc:
                console.print(f"[red]command failed:[/] {type(exc).__name__}: {str(exc)[:200]}")
                continue

            # NL shortcut: mail questions bypass the LLM tool-use lottery
            # (103 schemas; small models answer "no access" instead of calling).
            _mail_hit = await try_mail_shortcut(orch, cmd, console)
            if _mail_hit:
                continue

            # NL shortcut: one-tap routines (on-demand bundles).
            _routine_hit = await try_routine_shortcut(orch, cmd, console)
            if _routine_hit:
                continue

            # NL shortcut: PC deep control ("open chrome", "demo prep", ...).
            _pc_hit = await try_pc_shortcut(orch, cmd, console)
            if _pc_hit:
                continue

            # Voice surface when the speaker is on: single-shot <spoken> tag
            # (no second distill call) + pipelined speech. Looked up BEFORE
            # the turn so generation itself carries the instruction.
            try:
                _vs_pre = get_voice_session()
                _voice_on = bool(getattr(getattr(_vs_pre, "speaker", None), "enabled", False))
            except Exception:
                _vs_pre, _voice_on = None, False
            reply = await chat_turn(orch, cmd, surface="voice" if _voice_on else "cli")
            # Budget guard: once-per-day low-tier nudge (cheap SQLite SUMs).
            try:
                _alert = await orch.maybe_budget_alert()
                if _alert:
                    console.print(f"[yellow]{_alert}[/]")
            except Exception:
                pass
            # /tts mode: speak the reply (single-shot tag, else distilled).
            # Never breaks chat.
            if reply:
                try:
                    _vs = _vs_pre if _vs_pre is not None else get_voice_session()
                    if _voice_on or getattr(getattr(_vs, "speaker", None), "enabled", False):
                        await _vs._speak_reply(reply, user_text=cmd)
                        _verr = getattr(getattr(_vs, "speaker", None), "last_error", "")
                        if _verr:
                            console.print(
                                f"[dim]voice: {_verr} (reply shown above; /tts off to mute)[/]"
                            )
                    else:
                        if not getattr(_vs, "_muted_hinted", False):
                            _vs._muted_hinted = True
                            console.print("[dim]voice muted (/tts on to enable speech)[/]")
                except Exception as _e:
                    console.print(f"[dim]tts skipped: {_e}[/]")
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted — shutting down (fast teardown)…[/]")
    finally:
        # Timed phased teardown (shutdown-hang fix): every phase is capped
        # and loud. A wedged phase is reported and skipped — never a silent
        # 30s stare, never forever. Second Ctrl+C keeps cleaning up instead
        # of aborting mid-teardown (CancelledError is contained per phase).
        async def _phase(name, coro, timeout_s):
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(coro, timeout_s)
            except asyncio.TimeoutError:
                console.print(
                    f"[yellow]shutdown: {name} stalled (>{timeout_s:.0f}s) — moving on[/]"
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
            except Exception as exc:
                console.print(f"[dim]shutdown: {name}: {type(exc).__name__}[/]")
            else:
                dt = time.monotonic() - t0
                if dt > 2.0:
                    console.print(f"[dim]shutdown: {name} took {dt:.1f}s[/]")

        # Stop ALL supervised children in parallel (one shared cap, not 3x5s).
        await _phase("children", aterminate_children(api_proc), 10.0)
        if telegram_bot:
            await _phase("telegram", telegram_bot.stop(), 8.0)
        try:
            from pathlib import Path as _P2

            from core.single_owner import release as _rel

            with contextlib.suppress(Exception):
                _rel(_P2(cfg.db_path).parent)
        except Exception:
            pass
        await _phase("brain", orch.aclose(), 15.0)
    console.print("[dim]JARVIS MARK 3 signing off — standing by.[/]")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        console.print("\n[dim]JARVIS MARK 3 signing off — standing by.[/]")
        raise SystemExit(130) from None

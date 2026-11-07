"""JARVIS MARK 3 API — FastAPI app assembly + lifespan (boot contract).

Two run modes, chosen at lifespan start:

  * embedded  — ``api.state._embedded`` set via ``configure_from_external``:
    share the CLI's live orchestrator + telegram bot. We NEVER close them on
    shutdown; the CLI owns their lifecycle.
  * service   — standalone ``uvicorn api.main:app`` (spawned by the CLI with
    ``JARVIS_SERVICE_INSTANCE=1``): builds its own orchestrator, stops the
    scheduler + auto-background (the CLI brain owns scheduling), forwards
    chat to the CLI brain via ``UnifiedBrainProxy``, and claims Telegram
    polling through the ``core.single_owner`` lock.

Invariants (regression-tested in tests/test_boot.py):
  - Exactly ONE ``_bg_event_poller`` task per process, started inside the
    mode branch AFTER ``state.config`` exists. (It was once created above
    the branch too — the service mode ended up with two pollers and every
    bg_job_done event was broadcast twice.)
  - Telegram has exactly one owner process; the lock loser skips polling,
    never dual-polls (409 Conflict).
  - Optional-subsystem failures are fail-soft with a printed reason; a
    failed Telegram start releases the ownership lock again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api import state
from api.auth import AuthManager
from api.routes import ALL_ROUTERS
from api.state import configure_from_external  # noqa: F401  (back-compat re-export)
from api.websocket import ConnectionManager
from core.config import load_config
from core.orchestrator import Orchestrator


async def _bg_event_poller():
    """Poll the shared events.db for bg_job_done events; push to web clients.

    Works in both embedded and separate-process mode: the orchestrator
    publishes the event synchronously to SQLite (WAL), and we read + broadcast
    on our own loop — no cross-loop websocket sends, no missed events while
    the user isn't mid-chat. Starts from the current max id (no history replay
    on boot).
    """
    import sqlite3 as _sq

    db = None
    last_id = 0
    try:
        db_path = state.config.db_path.parent / "events.db"
    except Exception:
        return
    while True:
        try:
            await asyncio.sleep(3)
            if state.ws_manager is None or not state.ws_manager.active:
                continue
            if db is None:
                if not db_path.exists():
                    continue
                db = _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                row = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
                last_id = row[0] if row else 0
            rows = db.execute(
                "SELECT id, payload FROM events WHERE type = 'bg_job_done' AND id > ? ORDER BY id",
                (last_id,),
            ).fetchall()
            for eid, payload in rows:
                last_id = eid
                try:
                    data = json.loads(payload)
                except Exception:
                    data = {}
                await state.ws_manager.broadcast({"type": "bg_job_done", **data})
        except asyncio.CancelledError:
            raise
        except Exception:
            # DB locked / replaced / etc. — reconnect next cycle.
            try:
                if db is not None:
                    db.close()
            except Exception:
                pass
            db = None
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if state._embedded and state._external_orchestrator is not None:
        # Embedded mode: share the CLI's orchestrator + telegram bot.
        # We do NOT close them on shutdown — the CLI owns their lifecycle.
        state.orch = state._external_orchestrator
        state.config = state.orch.cfg
        state.auth_manager = AuthManager(state.config.db_path.parent / "api_tokens.db")
    state.ws_manager = ConnectionManager(state.orch)

    # Kokoro warmup removed (2026-09-20): kokoro is explicit-only in the
    # voice chain now (edge -> gemini -> piper -> sapi), so preloading the
    # 325MB model at every API boot just burned 5-11s of startup and held
    # the model in RAM for an engine that never speaks unless the user
    # pins tts_engine: kokoro.
    if state._embedded and state._external_orchestrator is not None:
        # Embedded mode: the CLI owns the orchestrator + telegram lifecycle.
        state.telegram_bot = state._external_telegram
        from core.logging_config import get_logger as _get_logger

        _get_logger("api").info("embedded mode: sharing CLI orchestrator on :8080")
        # Background-job push: poll the shared events.db for bg_job_done
        # events (published by the orchestrator when a background agent
        # finishes) and broadcast to all web clients. Started HERE — inside
        # the branch, after state.config exists — so each process runs
        # exactly ONE poller (see module docstring).
        asyncio.get_running_loop().create_task(_bg_event_poller())
        yield
        return

    state.config = load_config()
    # Phase C log split: the API child writes to its own api.log instead of
    # interleaving into the CLI's jarvis.log.
    if os.environ.get("JARVIS_SERVICE_INSTANCE") == "1":
        from core.logging_config import setup_logging

        setup_logging(level="INFO", log_file="api.log")
    state.auth_manager = AuthManager(state.config.db_path.parent / "api_tokens.db")
    state.orch = Orchestrator(state.config)
    await state.orch.start()
    # Service instance (spawned by the CLI): the CLI brain owns scheduling
    # and self-improvement triggers — this instance serves API + Telegram only.
    if os.environ.get("JARVIS_SERVICE_INSTANCE") == "1":
        with contextlib.suppress(Exception):
            await state.orch.scheduler.stop()  # kills the run loop (no duplicate reminders)
        with contextlib.suppress(Exception):
            state.orch.scheduler._running = False  # keep stop() idempotent
        state.orch.auto_background = False
        # Unified brain: forward chat to the CLI's live brain over the mesh.
        from core.unified_brain import UnifiedBrainProxy

        state.brain_proxy = UnifiedBrainProxy(state.orch)
        await state.brain_proxy.start()
    else:
        state.brain_proxy = None
    state.ws_manager = ConnectionManager(state.orch)

    # Background-job push: forward bg_job_done events (published by the
    # orchestrator when a background agent finishes) to all web clients.
    # This is the non-embedded branch (separate API process): the child's own
    # orch has auto_background disabled, so bg jobs are run by the CLI brain,
    # whose events land in the shared events.db — poll it and broadcast.
    # Exactly ONE poller per process: the embedded branch above starts its
    # own — never create one outside the two mode branches.
    if state.ws_manager is not None and state.config is not None:
        asyncio.get_running_loop().create_task(_bg_event_poller())

    state.telegram_bot = None
    tg_cfg = state.config.telegram
    if tg_cfg.enabled and tg_cfg.bot_token:
        from core.single_owner import acquire, read_owner
        from core.telegram import TelegramBot

        owner_base = state.config.db_path.parent
        got_lock = acquire(owner_base, "api")
        if not got_lock:
            cur = read_owner(owner_base)
            print(f"[telegram] owned by {cur} — API polling skipped (single owner).")
        else:
            _chat_factory = state.brain_proxy.chat_stream if state.brain_proxy is not None else None
            state.telegram_bot = TelegramBot(
                bot_token=tg_cfg.bot_token,
                orch=state.orch,
                state_path=state.config.db_path.parent / "telegram_bot.json",
                poll_timeout=30,
                chat_stream_factory=_chat_factory,
            )
            try:
                await state.telegram_bot.start()
            except Exception as exc:
                print(f"[telegram] bot failed to start: {exc}")
                try:
                    from core.single_owner import release

                    release(owner_base)
                except Exception:
                    pass
                state.telegram_bot = None

    yield

    if state.telegram_bot:
        await state.telegram_bot.stop()
        try:
            from core.single_owner import release

            release(state.config.db_path.parent)
        except Exception:
            pass
    if state.brain_proxy is not None:
        with contextlib.suppress(Exception):
            await state.brain_proxy.stop()
    await state.orch.aclose()


app = FastAPI(title="JARVIS MARK 3 API", lifespan=lifespan)


# Rate limiting middleware
class RateLimitMiddleware:
    def __init__(self, app: FastAPI):
        self.app = app
        self.requests: dict[str, list[float]] = defaultdict(list)
        self._loaded = False
        self.enabled = False
        self.rpm = 60
        self.rph = 1000
        self.burst = 10

    def _ensure_config(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            rl = state.config.api.rate_limit
            self.enabled = rl.enabled
            self.rpm = rl.requests_per_minute
            self.rph = rl.requests_per_hour
            self.burst = rl.burst
        except Exception:
            pass

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        self._ensure_config()
        if not self.enabled:
            await self.app(scope, receive, send)
            return

        # Get client IP
        client_ip = "unknown"
        for header, value in scope.get("headers", []):
            if header == b"x-forwarded-for":
                client_ip = value.decode().split(",")[0].strip()
                break
            elif header == b"x-real-ip":
                client_ip = value.decode()
                break
        if client_ip == "unknown":
            client = scope.get("client")
            if client:
                client_ip = client[0]

        now = time.time()
        minute_ago = now - 60
        hour_ago = now - 3600

        # Clean old entries
        self.requests[client_ip] = [ts for ts in self.requests[client_ip] if ts > hour_ago]

        # Check limits
        recent_minute = sum(1 for ts in self.requests[client_ip] if ts > minute_ago)
        recent_hour = len(self.requests[client_ip])

        if recent_minute >= self.rpm or recent_hour >= self.rph:
            # Rate limited
            response = Response(
                content=json.dumps({"error": "Rate limit exceeded", "retry_after": 60}),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return

        # Record request
        self.requests[client_ip].append(now)

        await self.app(scope, receive, send)


# Add rate limiting middleware (config read lazily on first request)
app.add_middleware(RateLimitMiddleware)

# CORS: loopback only (local HUD). No LAN origins by default — expose via
# Tailscale/cloudflared tunnel instead of opening firewall ports.
CORS_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

# New JARVIS web dashboard (jarvis-web production build), if present.
# The app uses a hash router, so a single index.html serves every route.
_WEB_DIST = ROOT.parent / "jarvis-web" / "dist"
if (_WEB_DIST / "index.html").exists():
    # The built index.html references /assets/*, so mount at that exact path.
    app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="web-assets")

    @app.get("/favicon.svg")
    async def web_favicon():
        from fastapi.responses import FileResponse

        return FileResponse(_WEB_DIST / "favicon.svg")

    @app.get("/icons.svg")
    async def web_icons():
        from fastapi.responses import FileResponse

        return FileResponse(_WEB_DIST / "icons.svg")

    @app.get("/dashboard")
    @app.get("/dashboard/{rest:path}")
    async def web_dashboard(rest: str = ""):
        from fastapi.responses import HTMLResponse

        html = (_WEB_DIST / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/")
@app.get("/app")
@app.get("/ui")
async def root():
    from fastapi.responses import HTMLResponse

    # Prefer the new web dashboard when built
    if (_WEB_DIST / "index.html").exists():
        html = (_WEB_DIST / "index.html").read_text(encoding="utf-8")
    else:
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


# Domain routers (api/routes/*) — registered after the app-level web routes,
# matching the original single-file ordering.
for _router in ALL_ROUTERS:
    app.include_router(_router)


# Back-compat: legacy ``api.main.orch`` / ``api.main.auth_manager`` style
# access (e.g. scripts/web_exercise.py) forwards to the shared state module.
_SHARED_NAMES = {
    "orch",
    "auth_manager",
    "ws_manager",
    "config",
    "brain_proxy",
    "telegram_bot",
    "_BOOT_TS",
}


def __getattr__(name: str):  # PEP 562 module-level attribute fallback
    if name in _SHARED_NAMES:
        return getattr(state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

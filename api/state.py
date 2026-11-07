"""Shared runtime state for the JARVIS API.

Routers under ``api/routes/`` must read these attributes at *request time*
via ``from api import state`` + ``state.orch`` — never
``from api.state import orch`` — because the values are ``None`` until the
app's lifespan (or ``configure_from_external``) populates them.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parent.parent

orch = None
auth_manager = None
ws_manager = None
config = None
# Set when the CLI embeds the API server: reuse its orchestrator instead of
# building a second one (one brain, one Telegram poller, one scheduler).
_external_orchestrator = None
_external_telegram = None
_external_loop: asyncio.AbstractEventLoop | None = None
_embedded = False
# Unified-brain proxy (service instance): routes chat to the CLI brain
brain_proxy = None
telegram_bot = None
# Phase B: boot timestamp for /api/health uptime
_BOOT_TS = time.time()


def configure_from_external(external_orch, telegram_bot=None, loop=None) -> None:
    """Called by jarvis.py to embed the API server over its live orchestrator.

    `loop` is the CLI's event loop — async orchestrator calls made from the
    API's own thread/loop are marshalled onto it with run_coroutine_threadsafe.
    """
    global _external_orchestrator, _external_telegram, _external_loop, _embedded
    _external_orchestrator = external_orch
    _external_telegram = telegram_bot
    _external_loop = loop or asyncio.get_running_loop()
    _embedded = True


def _run_on_orch_loop(coro, timeout_s: float = 120.0):
    """Await a CLI-loop coroutine from the API thread safely. Sync bridging."""
    fut = asyncio.run_coroutine_threadsafe(coro, _external_loop)
    return fut.result(timeout_s)


def get_auth_token(request: Request):
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = auth[7:]
    if not auth_manager or not auth_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    return token

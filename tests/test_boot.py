"""Wave 1 boot smoke tests — pins the boot-path contracts of jarvis.py and
api/main.py:

  - exactly ONE ``_bg_event_poller`` task per process, in BOTH lifespan
    modes (regression: it was once created unconditionally AND in the
    service branch, so service mode double-broadcast every bg_job_done);
  - telegram single-owner wiring in service mode (acquire -> start ->
    stop -> release; lock loser skips polling);
  - jarvis.py imports cleanly and stops the telegram bot exactly once
    (regression: two locals, ``tg_bot`` + ``telegram_bot``, aliased the
    same bot and stop() ran twice in the finally-block);
  - supervisor ``wait_port_bound`` true/false probes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.main as api_main
from api import state

ROOT = Path(__file__).resolve().parent.parent

_STATE_NAMES = (
    "orch",
    "auth_manager",
    "ws_manager",
    "config",
    "brain_proxy",
    "telegram_bot",
    "_external_orchestrator",
    "_external_telegram",
    "_external_loop",
    "_embedded",
)


@pytest.fixture
def clean_state():
    """Snapshot and restore the api.state globals the lifespan mutates."""
    saved = {n: getattr(state, n) for n in _STATE_NAMES}
    yield
    for n, v in saved.items():
        setattr(state, n, v)


def _poller_recorder(calls: list):
    """Replacement for _bg_event_poller that records each task creation."""

    def fake():
        calls.append(1)

        async def _noop():
            return None

        return _noop()

    return fake


class _StubOrch:
    """Minimal orchestrator for the service-mode lifespan."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def aclose(self):
        self.closed = True


# ── jarvis.py boot spine ────────────────────────────────────────────────


def test_jarvis_module_imports_cleanly():
    import jarvis

    assert callable(jarvis.main)
    assert callable(jarvis.chat_turn)
    assert isinstance(jarvis.HAS_PROMPT_TOOLKIT, bool)


def test_jarvis_single_telegram_bot_variable():
    """One bot variable, stopped exactly once in the finally-block."""
    src = (ROOT / "jarvis.py").read_text(encoding="utf-8")
    assert "tg_bot" not in src
    assert src.count("telegram_bot.stop()") == 1
    assert "import time as _time" not in src


# ── supervisor probes ───────────────────────────────────────────────────


async def test_wait_port_bound_true_for_listener():
    from core.supervisor import wait_port_bound

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert await wait_port_bound(port, timeout_s=3.0)
    finally:
        srv.close()


async def test_wait_port_bound_false_for_closed_port():
    from core.supervisor import wait_port_bound

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert not await wait_port_bound(port, timeout_s=0.05)


# ── api.main lifespan: exactly one event poller per process ─────────────


async def test_lifespan_embedded_single_poller(tmp_path, monkeypatch, clean_state):
    calls = []
    monkeypatch.setattr(api_main, "_bg_event_poller", _poller_recorder(calls))
    cfg = SimpleNamespace(db_path=tmp_path / "jarvis.db")
    fake_orch = SimpleNamespace(cfg=cfg)
    state.configure_from_external(fake_orch, telegram_bot=None, loop=asyncio.get_running_loop())
    async with api_main.lifespan(api_main.app):
        assert state.orch is fake_orch
        assert state.config is cfg
        assert state.auth_manager is not None
        assert state.ws_manager is not None
        # Embedded mode shares the CLI brain — no proxy of its own.
        assert state.brain_proxy is None
    assert len(calls) == 1, "embedded mode must start exactly one event poller"


def _service_cfg(tmp_path, **tg):
    return SimpleNamespace(
        db_path=tmp_path / "jarvis.db",
        telegram=SimpleNamespace(enabled=False, bot_token=None, **tg),
    )


async def test_lifespan_service_single_poller(tmp_path, monkeypatch, clean_state):
    calls = []
    monkeypatch.setattr(api_main, "_bg_event_poller", _poller_recorder(calls))
    monkeypatch.delenv("JARVIS_SERVICE_INSTANCE", raising=False)
    monkeypatch.setattr(api_main, "load_config", lambda: _service_cfg(tmp_path))
    monkeypatch.setattr(api_main, "Orchestrator", _StubOrch)
    async with api_main.lifespan(api_main.app):
        assert state.orch.started
        assert state.brain_proxy is None
        assert state.telegram_bot is None
    assert state.orch.closed
    assert len(calls) == 1, "service mode must start exactly one event poller"


# ── api.main lifespan: telegram single-owner wiring ─────────────────────


async def test_lifespan_service_telegram_acquire_start_stop_release(
    tmp_path, monkeypatch, clean_state
):
    monkeypatch.setattr(api_main, "_bg_event_poller", _poller_recorder([]))
    monkeypatch.delenv("JARVIS_SERVICE_INSTANCE", raising=False)
    cfg = SimpleNamespace(
        db_path=tmp_path / "jarvis.db",
        telegram=SimpleNamespace(enabled=True, bot_token="tok"),
    )
    monkeypatch.setattr(api_main, "load_config", lambda: cfg)
    monkeypatch.setattr(api_main, "Orchestrator", _StubOrch)

    events = []

    class _StubBot:
        def __init__(self, **kw):
            events.append(("init", kw.get("bot_token")))

        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr("core.telegram.TelegramBot", _StubBot)
    from core.single_owner import read_owner

    async with api_main.lifespan(api_main.app):
        assert state.telegram_bot is not None
        owner = read_owner(tmp_path)
        assert owner is not None and owner.get("role") == "api"
    assert events == [("init", "tok"), "start", "stop"]
    assert read_owner(tmp_path) is None, "shutdown must release the owner lock"


async def test_lifespan_service_telegram_lock_loser_skips_polling(
    tmp_path, monkeypatch, clean_state
):
    monkeypatch.setattr(api_main, "_bg_event_poller", _poller_recorder([]))
    monkeypatch.delenv("JARVIS_SERVICE_INSTANCE", raising=False)
    cfg = SimpleNamespace(
        db_path=tmp_path / "jarvis.db",
        telegram=SimpleNamespace(enabled=True, bot_token="tok"),
    )
    monkeypatch.setattr(api_main, "load_config", lambda: cfg)
    monkeypatch.setattr(api_main, "Orchestrator", _StubOrch)

    constructed = []

    class _StubBot:
        def __init__(self, **kw):
            constructed.append(kw)

    monkeypatch.setattr("core.telegram.TelegramBot", _StubBot)

    # Simulate another live process holding the owner lock (parent PID is
    # alive for the whole test run; fresh timestamp => not stale).
    lock = tmp_path / "telegram_owner.lock"
    lock.write_text(
        json.dumps({"pid": os.getppid(), "role": "cli", "ts": time.time()}),
        encoding="utf-8",
    )
    async with api_main.lifespan(api_main.app):
        assert state.telegram_bot is None, "lock loser must not poll"
    assert constructed == [], "lock loser must not even build a bot"
    assert lock.exists(), "lock loser must not release someone else's lock"

"""Regression tests for core/supervisor.py (extracted from jarvis.py).

Covers spawn command construction, port-reclaim decisions, watchdog
restart / give-up policy, and shutdown — all with fake processes, so no
real subprocess, socket, or network is touched.
"""

import asyncio
import contextlib
import sys

import pytest

import core.supervisor as sup

_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(_seconds):
    await _REAL_SLEEP(0)


async def _true_async(*_a, **_k):
    return True


class FakeConsole:
    def __init__(self):
        self.lines = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))

    def joined(self):
        return "\n".join(self.lines)


class FakeProc:
    def __init__(self, pid=1234, alive=True, returncode=None):
        self.pid = pid
        self._alive = alive
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else (self.returncode if self.returncode is not None else 0)

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return self.poll()


class FakePopenFactory:
    def __init__(self, proc=None):
        self.calls = []
        self.proc = proc or FakeProc()

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.proc


class FakeSocket:
    def __init__(self, connect_result):
        self.connect_result = connect_result

    def settimeout(self, _t):
        pass

    def connect_ex(self, _addr):
        return self.connect_result

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture(autouse=True)
def clean_refs():
    sup.API_PROC_REFS.clear()
    yield
    sup.API_PROC_REFS.clear()


def test_spawn_api_child_builds_uvicorn_command(tmp_path, monkeypatch):
    factory = FakePopenFactory()
    monkeypatch.setattr(sup.subprocess, "Popen", factory)
    monkeypatch.delenv("JARVIS_API_PORT", raising=False)
    sup.spawn_api_child(tmp_path)
    args, kwargs = factory.calls[0]
    kwargs["stdout"].close()  # real log handle opened against tmp_path
    assert args[0] == sys.executable  # no .venv under tmp_path
    assert args[1:4] == ["-m", "uvicorn", "api.main:app"]
    assert args[args.index("--port") + 1] == "8080"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["JARVIS_SERVICE_INSTANCE"] == "1"
    assert (tmp_path / "logs").is_dir()


def test_spawn_api_child_prefers_venv_python(tmp_path, monkeypatch):
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("")
    factory = FakePopenFactory()
    monkeypatch.setattr(sup.subprocess, "Popen", factory)
    sup.spawn_api_child(tmp_path)
    factory.calls[0][1]["stdout"].close()
    assert factory.calls[0][0][0] == str(venv_py)


def test_spawn_wa_child_command(tmp_path, monkeypatch):
    wa_dir = tmp_path / "wa-bridge"
    wa_dir.mkdir()
    factory = FakePopenFactory()
    monkeypatch.setattr(sup.subprocess, "Popen", factory)
    sup.spawn_wa_child(wa_dir, 3101)
    args, kwargs = factory.calls[0]
    kwargs["stdout"].close()
    assert args[-1] == "server.js"
    assert kwargs["cwd"] == str(wa_dir)
    assert kwargs["env"]["WA_BRIDGE_PORT"] == "3101"
    assert (tmp_path / "logs").is_dir()


def test_rotate_log_if_big_rotates_when_over_threshold(tmp_path):
    log = tmp_path / "wa_bridge.log"
    log.write_bytes(b"x" * 2048)
    sup._rotate_log_if_big(log, max_bytes=1024, backups=5)
    assert not log.exists()
    assert (tmp_path / "wa_bridge.log.1").read_bytes() == b"x" * 2048


def test_rotate_log_if_big_leaves_small_log_alone(tmp_path):
    log = tmp_path / "wa_bridge.log"
    log.write_bytes(b"tiny")
    sup._rotate_log_if_big(log, max_bytes=1024)
    assert log.read_bytes() == b"tiny"
    assert not (tmp_path / "wa_bridge.log.1").exists()


def test_rotate_log_chain_shifts_and_drops_oldest(tmp_path):
    log = tmp_path / "wa_bridge.log"
    log.write_bytes(b"new" + b"x" * 2048)
    (tmp_path / "wa_bridge.log.1").write_bytes(b"old1")
    (tmp_path / "wa_bridge.log.2").write_bytes(b"old2")
    sup._rotate_log_if_big(log, max_bytes=1024, backups=3)
    assert (tmp_path / "wa_bridge.log.1").read_bytes().startswith(b"new")
    assert (tmp_path / "wa_bridge.log.2").read_bytes() == b"old1"
    assert (tmp_path / "wa_bridge.log.3").read_bytes() == b"old2"
    assert not (tmp_path / "wa_bridge.log.4").exists()


def test_spawn_wa_child_rotates_oversized_log(tmp_path, monkeypatch):
    wa_dir = tmp_path / "wa-bridge"
    wa_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "wa_bridge.log"
    big.write_bytes(b"x" * (11 * 1024 * 1024))
    factory = FakePopenFactory()
    monkeypatch.setattr(sup.subprocess, "Popen", factory)
    sup.spawn_wa_child(wa_dir, 3101)
    factory.calls[0][1]["stdout"].close()
    assert (logs / "wa_bridge.log.1").stat().st_size == 11 * 1024 * 1024
    assert big.stat().st_size == 0, "fresh log must start empty after rotation"


def test_spawn_hud_child_spawns_when_enabled(tmp_path, monkeypatch):
    (tmp_path / "hud.py").write_text("")
    factory = FakePopenFactory()
    monkeypatch.setattr(sup.subprocess, "Popen", factory)
    con = FakeConsole()
    sup.spawn_hud_child(tmp_path, con, enabled=True)
    assert factory.calls[0][0][1] == "hud.py"
    assert factory.calls[0][1]["cwd"] == str(tmp_path)
    assert "HUD overlay on" in con.joined()
    assert len(sup.API_PROC_REFS) == 1


def test_spawn_hud_child_skips_when_disabled(tmp_path, monkeypatch):
    (tmp_path / "hud.py").write_text("")
    factory = FakePopenFactory()
    monkeypatch.setattr(sup.subprocess, "Popen", factory)
    sup.spawn_hud_child(tmp_path, FakeConsole(), enabled=False)
    assert factory.calls == []
    assert sup.API_PROC_REFS == []


def test_reclaim_port_free_returns_true(monkeypatch):
    class R:
        stdout = ""

    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        return R()

    monkeypatch.setattr(sup.subprocess, "run", fake_run)
    assert sup.reclaim_api_port(FakeConsole()) is True
    assert len(run_calls) == 1  # only the owner-PID probe; nothing killed


def test_reclaim_port_foreign_process_left_alone(monkeypatch):
    outs = iter(["4321\n", "C:\\Windows\\notepad.exe"])

    class R:
        def __init__(self, out):
            self.stdout = out

    monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: R(next(outs)))
    con = FakeConsole()
    assert sup.reclaim_api_port(con) is False
    assert "foreign process" in con.joined()


def test_reclaim_port_kills_orphaned_uvicorn(monkeypatch):
    outs = iter(["4321\n", "python -m uvicorn api.main:app --port 8080", ""])

    class R:
        def __init__(self, out):
            self.stdout = out

    run_cmds = []

    def fake_run(args, **kwargs):
        run_cmds.append(" ".join(str(x) for x in args))
        return R(next(outs))

    monkeypatch.setattr(sup.subprocess, "run", fake_run)
    # port probe after the kill: connection refused => port is free
    monkeypatch.setattr(sup.socket, "socket", lambda *a, **k: FakeSocket(111))
    monkeypatch.setattr(sup.time, "sleep", lambda _s: None)
    con = FakeConsole()
    assert sup.reclaim_api_port(con) is True
    assert any("Stop-Process" in c and "4321" in c for c in run_cmds)
    assert "Reclaimed" in con.joined()


def test_reclaim_port_ignores_own_live_child(monkeypatch):
    ours = FakeProc(pid=4321, alive=True)
    sup.API_PROC_REFS.append(ours)

    class R:
        stdout = "4321\n"

    monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: R())
    con = FakeConsole()
    assert sup.reclaim_api_port(con) is False  # our own child holds it
    assert "foreign" not in con.joined()


async def test_wait_port_bound_true_then_false(monkeypatch):
    monkeypatch.setattr(sup.socket, "socket", lambda *a, **k: FakeSocket(0))
    assert await sup.wait_port_bound(8080, timeout_s=2) is True
    monkeypatch.setattr(sup.socket, "socket", lambda *a, **k: FakeSocket(111))
    monkeypatch.setattr(sup.asyncio, "sleep", _fast_sleep)
    assert await sup.wait_port_bound(8080, timeout_s=0.2) is False


async def test_api_watchdog_restarts_dead_child(tmp_path, monkeypatch):
    dead = FakeProc(pid=111, alive=False, returncode=1)
    new = FakeProc(pid=222, alive=True)
    state = {"proc": dead, "restarts": 0, "window_start": sup.time.time()}
    monkeypatch.setattr(sup.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(sup, "spawn_api_child", lambda base_dir: new)
    monkeypatch.setattr(sup, "wait_port_bound", _true_async)
    con = FakeConsole()
    task = asyncio.create_task(sup.api_watchdog(con, state, 8080, tmp_path))
    try:
        for _ in range(100):
            await _REAL_SLEEP(0)
            if state["proc"] is new:
                break
        assert state["proc"] is new
        assert state["restarts"] == 1
        assert new in sup.API_PROC_REFS
        assert "restarted" in con.joined()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_api_watchdog_reclaims_port_on_exit_code_3(tmp_path, monkeypatch):
    dead = FakeProc(alive=False, returncode=3)
    new = FakeProc(alive=True)
    state = {"proc": dead, "restarts": 0, "window_start": sup.time.time()}
    reclaimed = []
    monkeypatch.setattr(sup.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(sup, "reclaim_api_port", lambda console: reclaimed.append(True) or True)
    monkeypatch.setattr(sup, "spawn_api_child", lambda base_dir: new)
    monkeypatch.setattr(sup, "wait_port_bound", _true_async)
    con = FakeConsole()
    task = asyncio.create_task(sup.api_watchdog(con, state, 8080, tmp_path))
    try:
        for _ in range(100):
            await _REAL_SLEEP(0)
            if reclaimed:
                break
        assert reclaimed, "exit code 3 should trigger port reclaim before respawn"
        assert "port conflict" in con.joined()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_api_watchdog_gives_up_after_five_restarts(tmp_path, monkeypatch):
    dead = FakeProc(alive=False, returncode=1)
    state = {"proc": dead, "restarts": 5, "window_start": sup.time.time()}
    monkeypatch.setattr(sup.asyncio, "sleep", _fast_sleep)
    con = FakeConsole()
    await asyncio.wait_for(sup.api_watchdog(con, state, 8080, tmp_path), timeout=5)
    assert "giving up" in con.joined()


async def test_wa_watchdog_restarts_dead_bridge(tmp_path, monkeypatch):
    dead = FakeProc(alive=False, returncode=1)
    new = FakeProc(alive=True)
    state = {"proc": dead, "restarts": 0, "window_start": sup.time.time()}
    monkeypatch.setattr(sup.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(sup, "spawn_wa_child", lambda wa_dir, port: new)
    monkeypatch.setattr(sup, "wa_wait_ready", _true_async)
    con = FakeConsole()
    task = asyncio.create_task(sup.wa_watchdog(con, state, tmp_path, 3101))
    try:
        for _ in range(100):
            await _REAL_SLEEP(0)
            if state["proc"] is new:
                break
        assert state["proc"] is new
        assert state["restarts"] == 1
        assert new in sup.API_PROC_REFS
        assert "back online" in con.joined()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_terminate_children_stops_all_generations():
    a, b = FakeProc(alive=True), FakeProc(alive=True)
    extra = FakeProc(alive=True)
    sup.API_PROC_REFS.extend([a, b])
    sup.terminate_children(extra)
    assert a.terminated and b.terminated and extra.terminated
    assert sup.API_PROC_REFS == []


def test_terminate_children_skips_dead_and_tolerates_none():
    dead = FakeProc(alive=False, returncode=0)
    sup.API_PROC_REFS.append(dead)
    sup.terminate_children(None)  # brain-only mode: api_proc is None
    assert not dead.terminated
    assert sup.API_PROC_REFS == []

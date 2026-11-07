"""Child-process supervision for the JARVIS CLI (extracted from jarvis.py).

Owns the managed child processes started by ``main()``:
  - the API server + website (``uvicorn api.main:app`` on :8080 / JARVIS_API_PORT)
  - the desktop HUD overlay (``hud.py`` via pythonw)
  - the WhatsApp bridge (``node wa-bridge/server.js`` on :3101 / WA_BRIDGE_PORT)

Every spawn is tracked in :data:`API_PROC_REFS` so shutdown leaves no
orphans; watchdog tasks restart dead children with exponential backoff and
give up after repeated crashes. Pure refactor — behavior unchanged.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Supervised API children (watchdog restarts replace entries) — module
# level so the REPL's finally-block can terminate every generation.
API_PROC_REFS: list = []


def reclaim_api_port(console) -> bool:
    """Port-conflict recovery for the API port (:8080 or JARVIS_API_PORT).

    If the current holder is an orphaned JARVIS API child (uvicorn started
    by a CLI that has since exited), kill it and free the port so THIS
    instance can own the API + website. Returns True when the port is free.
    Foreign processes and OUR OWN live children are left untouched.
    """
    api_port = os.environ.get("JARVIS_API_PORT", "8080")
    try:
        conns = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-NetTCPConnection -LocalPort {api_port} -State Listen -ErrorAction SilentlyContinue "
                "| Select-Object -ExpandProperty OwningProcess -Unique",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pids = [int(p) for p in conns.stdout.split() if p.strip().isdigit()]
        if not pids:
            return True  # port is actually free
        our_pids = {pr.pid for pr in API_PROC_REFS if pr.poll() is None}
        for pid in pids:
            if pid in our_pids:
                # one of OUR live children holds it — nothing to reclaim
                return False
            try:
                proc = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                cmdline = (proc.stdout or "").strip()
            except Exception:
                cmdline = ""
            if "uvicorn" in cmdline and "api.main:app" in cmdline:
                # our command line + not one of our live children => orphan
                # (its parent CLI exited without cleaning up)
                try:
                    subprocess.run(
                        [
                            "powershell",
                            "-NoProfile",
                            "-Command",
                            f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
                        ],
                        capture_output=True,
                        timeout=10,
                    )
                    console.print(
                        f"[yellow]Reclaimed :{api_port} from orphaned API child (pid {pid})[/]"
                    )
                except Exception:
                    pass
            else:
                console.print(
                    f"[yellow]Port :{api_port} is held by a foreign process (pid {pid}); leaving it alone.[/]"
                )
                return False
        # wait for the port to actually free
        for _ in range(15):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                    _s.settimeout(0.5)
                    if _s.connect_ex(("127.0.0.1", int(api_port))) != 0:
                        return True
            except Exception:
                return True
            time.sleep(1)
        return False
    except Exception:
        return False


def spawn_api_child(base_dir: Path):
    """Spawn the API server + website as a managed child process."""
    venv_py = base_dir / ".venv" / "Scripts" / "python.exe"
    py = str(venv_py) if venv_py.exists() else sys.executable
    api_port = os.environ.get("JARVIS_API_PORT", "8080")
    # Child logs go to their own file (Phase C split) — stdout
    # AND stderr, append mode so restarts keep history.
    log_dir = base_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    child_log = open(log_dir / "api_child.log", "ab")  # noqa: SIM115 — handle stays open for the child's lifetime by design
    return subprocess.Popen(
        [
            py,
            "-m",
            "uvicorn",
            "api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            api_port,
            "--log-level",
            "warning",
        ],
        cwd=str(base_dir),
        env={**os.environ, "JARVIS_SERVICE_INSTANCE": "1"},
        stdin=subprocess.DEVNULL,
        stdout=child_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


async def wait_port_bound(port: int, timeout_s: float = 75.0) -> bool:
    """Poll 127.0.0.1:<port> until it accepts connections or times out."""
    dl = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < dl:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                _s.settimeout(0.5)
                if _s.connect_ex(("127.0.0.1", port)) == 0:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


def spawn_hud_child(base_dir: Path, console, enabled: bool = True) -> None:
    """Desktop HUD overlay: lightweight tkinter card reading shared SQLite
    state — no auth, no second orchestrator. Tracked in API_PROC_REFS so it
    dies with the CLI. Best-effort: prints status, never raises."""
    try:
        if enabled and (base_dir / "hud.py").exists():
            _venv_w = base_dir / ".venv" / "Scripts" / "pythonw.exe"
            _hud_py = str(_venv_w) if _venv_w.exists() else sys.executable
            _hud = subprocess.Popen(
                [_hud_py, "hud.py"],
                cwd=str(base_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            API_PROC_REFS.append(_hud)
            console.print("[green]HUD overlay on (Esc or X to close)[/]")
    except Exception as _he:
        console.print(f"[dim]HUD unavailable: {str(_he)[:100]}[/]")


async def api_watchdog(console, state: dict, port: int, base_dir: Path) -> None:
    """Keep the API child alive. Restarts on death with backoff
    (2s -> 4s -> 8s -> 30s cap); gives up after 5 tries in a 10-min
    window (repeated crashes = something deeper).

    NOTE: the CURRENT child is tracked via the ``state`` dict — the
    original ``api_proc`` local in main() goes stale after the first
    restart.
    """
    backoff = 2.0
    while True:
        await asyncio.sleep(3)
        cur = state["proc"]
        if cur is None:
            return
        if cur.poll() is not None:
            now = time.time()
            # reset the counter if we've been stable for 10 min
            if now - state["window_start"] > 600:
                state["restarts"] = 0
                state["window_start"] = now
            if state["restarts"] >= 5:
                console.print(
                    "[red]API child keeps crashing — watchdog giving up (check logs/api_child.log)[/]"
                )
                return
            state["restarts"] += 1
            # Exit code 3 = bind failure (EADDRINUSE): a dead
            # previous child orphaned its uvicorn on :8080 and
            # holds the port. Blind respawn just loops — reclaim
            # the port from the orphan first, then restart.
            if cur.returncode == 3:
                console.print(
                    "[yellow]child died on port conflict — reclaiming :8080 from orphan[/]"
                )
                reclaim_api_port(console)
            console.print(
                f"[yellow]API child died (code {cur.returncode}) — restarting "
                f"(attempt {state['restarts']}, {backoff:.0f}s backoff)[/]"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            try:
                new_proc = spawn_api_child(base_dir)
                API_PROC_REFS.append(new_proc)
                state["proc"] = new_proc
                console.print(f"[dim]watchdog: spawned child pid={new_proc.pid}[/]")
                bound = await wait_port_bound(port, 45)
                if bound:
                    console.print("[green]API child restarted — website back online[/]")
                    backoff = 2.0
                else:
                    console.print(
                        f"[red]restarted child failed to bind (pid={new_proc.pid} "
                        f"alive={new_proc.poll() is None} code={new_proc.poll()})[/]"
                    )
                    # keep watching the new child — it may die too
            except Exception as exc:
                console.print(f"[red]restart failed:[/] {str(exc)[:140]}")


def _rotate_log_if_big(path: Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> None:
    """Size-cap an append-only log family (wa_bridge.log).

    The bridge's stdout is redirected to this file by the OS — there is no
    in-process handler to attach a RotatingFileHandler to, so rotate at spawn
    time instead: path → path.1, path.1 → path.2, …, oldest dropped.
    Housekeeping must never crash a spawn: never raises.
    """
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i + 1}")
            newer = path.with_name(f"{path.name}.{i}")
            if older.exists():
                older.unlink()
            if newer.exists():
                newer.rename(older)
        path.rename(path.with_name(f"{path.name}.1"))
    except Exception:
        pass


def spawn_wa_child(wa_dir: Path, port: int):
    """Spawn the WhatsApp bridge (node wa-bridge/server.js) as a managed
    child process."""
    _node = shutil.which("node") or "node"
    _log_dir = wa_dir.parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _log_path = _log_dir / "wa_bridge.log"
    _rotate_log_if_big(_log_path)
    _log = open(_log_path, "ab")  # noqa: SIM115 — handle stays open for the bridge lifetime by design
    return subprocess.Popen(
        [_node, "server.js"],
        cwd=str(wa_dir),
        env={**os.environ, "WA_BRIDGE_PORT": str(port)},
        stdin=subprocess.DEVNULL,
        stdout=_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )


async def wa_wait_ready(port: int, timeout_s: float = 30.0) -> bool:
    """Poll the bridge /status endpoint until it answers 200 or times out."""
    dl = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < dl:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as _r:
                if _r.status == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


async def wa_watchdog(console, state: dict, wa_dir: Path, port: int) -> None:
    """Keep the WhatsApp bridge alive; same supervision policy as the API
    child (restart with backoff, give up after 5 crashes in 10 min)."""
    _backoff = 5.0
    while True:
        await asyncio.sleep(10)
        _cur = state["proc"]
        if _cur is None:
            return
        if _cur.poll() is not None:
            _now = time.time()
            if _now - state["window_start"] > 600:
                state["restarts"] = 0
                state["window_start"] = _now
            if state["restarts"] >= 5:
                console.print(
                    "[red]wa-bridge keeps dying — watchdog giving up (check logs/wa_bridge.log)[/]"
                )
                return
            state["restarts"] += 1
            console.print(f"[yellow]wa-bridge died — restarting (attempt {state['restarts']})[/]")
            await asyncio.sleep(_backoff)
            try:
                _new = spawn_wa_child(wa_dir, port)
                API_PROC_REFS.append(_new)
                state["proc"] = _new
                if await wa_wait_ready(port, 30):
                    console.print("[green]wa-bridge back online[/]")
                    _backoff = 5.0
            except Exception as _exc:
                console.print(f"[red]wa-bridge restart failed:[/] {str(_exc)[:120]}")


def terminate_children(extra_proc=None) -> None:
    """Stop ALL supervised children (watchdog restarts create new
    generations; every entry gets terminated so no orphan survives).

    ``extra_proc`` is the original ``api_proc`` local from main() — normally
    already covered via API_PROC_REFS, terminated again for parity (no-op
    when already dead).

    NOTE: sequential 5s waits (up to 15s+ for 3 children). The CLI shutdown
    path uses aterminate_children() instead — same guarantee, one shared cap.
    """
    for child in list(API_PROC_REFS):
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except Exception:
                    child.kill()
        except Exception:
            pass
    API_PROC_REFS.clear()
    if extra_proc is not None:
        try:
            if extra_proc.poll() is None:
                extra_proc.terminate()
                try:
                    extra_proc.wait(timeout=5)
                except Exception:
                    extra_proc.kill()
        except Exception:
            pass


async def aterminate_children(extra_proc=None, total_timeout: float = 8.0) -> dict:
    """Fast parallel teardown for the Ctrl+C path (shutdown-hang fix).

    Signals EVERY child at once, then waits with ONE shared cap instead of
    sequential 5s waits. Survivors past the cap are killed. Returns
    {pid_or_label: "exited"|"terminated"|"killed"|"already-dead"|"error"} and
    prints one line per slow child so a wedged process is visible instead of
    a silent 15s stare. Never raises.
    """
    import contextlib as _cl

    targets: list = []
    seen: set = set()
    for child in list(API_PROC_REFS):
        pid = getattr(child, "pid", id(child))
        if pid not in seen:
            seen.add(pid)
            targets.append(child)
    if extra_proc is not None:
        pid = getattr(extra_proc, "pid", id(extra_proc))
        if pid not in seen:
            targets.append(extra_proc)

    outcome: dict = {}
    live: list = []
    for child in targets:
        label = str(getattr(child, "pid", "?"))
        try:
            if child.poll() is None:
                with _cl.suppress(Exception):
                    child.terminate()
                live.append((label, child))
            else:
                outcome[label] = "already-dead"
        except Exception as exc:
            outcome[label] = f"error: {type(exc).__name__}"

    if live:
        deadline = time.monotonic() + max(1.0, total_timeout)
        while live and time.monotonic() < deadline:
            still = [(label, p) for label, p in live if p.poll() is None]
            if not still:
                break
            live = still
            await asyncio.sleep(0.2)
        for label, p in live:
            try:
                if p.poll() is None:
                    with _cl.suppress(Exception):
                        p.kill()
                    outcome[label] = "killed"
                    print(f"[shutdown] child pid={label} ignored terminate — killed")
                else:
                    outcome[label] = "terminated"
            except Exception as exc:
                outcome[label] = f"error: {type(exc).__name__}"
        for _p in targets:
            _label = str(getattr(_p, "pid", "?"))
            if _label not in outcome:
                outcome[_label] = "terminated"
    with _cl.suppress(Exception):
        API_PROC_REFS.clear()
    return outcome

"""System tools (sys_*): run commands, manage processes/windows, send keys,
system stats, environment/disk/network info, wallpaper, lock, shutdown.
Fail-soft per action (error strings).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

from core.tools import Tool, ToolBox


async def run_command(cmd: str, cwd: str = "", timeout: int = 60, capture: bool = True) -> str:
    """Execute a shell command with timeout and optional output capture."""
    cwd_obj = Path(cwd).expanduser().resolve() if cwd else Path.cwd()

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd_obj,
            stdout=asyncio.subprocess.PIPE if capture else None,
            stderr=asyncio.subprocess.PIPE if capture else None,
        )

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"error: command timed out after {timeout}s"

        parts = [f"Command: {cmd}", f"Exit code: {proc.returncode}"]
        if capture:
            if out:
                parts.append(f"Output:\n{out.decode(errors='replace')[:5000]}")
            if err:
                parts.append(f"Errors:\n{err.decode(errors='replace')[:2000]}")
        return "\n".join(parts)
    except Exception as e:
        return f"error: {e}"


async def run_command_async(cmd: str, cwd: str = "") -> str:
    """Start a command in the background and return immediately with PID."""
    cwd_obj = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd_obj,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return f"started background process (PID: {proc.pid})"
    except Exception as e:
        return f"error: {e}"


async def kill_process(pid: int, force: bool = False) -> str:
    """Kill a process by PID."""
    try:
        if sys.platform == "win32":
            if force:
                os.system(f"taskkill /F /PID {pid} >nul 2>&1")
            else:
                os.system(f"taskkill /PID {pid} >nul 2>&1")
        else:
            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
        return f"sent {'SIGKILL' if force else 'SIGTERM'} to PID {pid}"
    except ProcessLookupError:
        return f"error: process {pid} not found"
    except PermissionError:
        return f"error: permission denied to kill PID {pid}"
    except Exception as e:
        return f"error: {e}"


async def list_processes(filter: str = "") -> str:
    """List running processes with optional filter."""
    try:
        if sys.platform == "win32":
            cmd = "tasklist /FO CSV /NH"
        else:
            cmd = "ps aux"

        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return f"error: {err.decode()[:200]}"

        output = out.decode(errors="replace")
        if filter:
            lines = [line for line in output.split("\n") if filter.lower() in line.lower()]
            output = "\n".join(lines[:50])
        else:
            output = "\n".join(output.split("\n")[:50])
        return output if output else "no matching processes"
    except Exception as e:
        return f"error: {e}"


async def launch_app(name: str, args: list[str] = None) -> str:
    """Launch an application by name or path."""
    args = args or []
    try:
        if sys.platform == "win32":
            cmd = [name] + args
            proc = await asyncio.create_subprocess_exec(
                *cmd, creationflags=0x00000008
            )  # DETACHED_PROCESS
        else:
            cmd = [name] + args
            proc = await asyncio.create_subprocess_exec(*cmd)
        return f"launched {name} (PID: {proc.pid})"
    except FileNotFoundError:
        # Try shell on Windows
        if sys.platform == "win32":
            try:
                cmd = f'start "" "{name}" {" ".join(args)}'
                proc = await asyncio.create_subprocess_shell(cmd)
                return f"launch request issued for '{name}' via shell"
            except Exception as e:
                return f"error: {e}"
        return f"error: {name} not found"
    except Exception as e:
        return f"error: {e}"


async def get_windows() -> str:
    """Get list of visible windows with titles and process IDs."""
    try:
        import psutil
        import win32gui
        import win32process
    except ImportError:
        return "error: pywin32 and psutil required (pip install pywin32 psutil)"

    windows = []

    def enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    proc = psutil.Process(pid)
                    name = proc.name()
                except Exception:
                    name = "unknown"
                windows.append(f"[{pid}] {name}: {title}")

    win32gui.EnumWindows(enum_handler, None)
    return "\n".join(windows[:50]) if windows else "no visible windows"


async def focus_window(title_regex: str = "", pid: int = 0) -> str:
    """Bring a window to front by title regex or PID."""
    try:
        import win32gui
        import win32process
    except ImportError:
        return "error: pywin32 required (pip install pywin32)"

    found = []

    def enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                if (pid and wpid == pid) or (title_regex and title_regex.lower() in title.lower()):
                    found.append((hwnd, title, wpid))

    win32gui.EnumWindows(enum_handler, None)

    if not found:
        return "no matching window found"

    hwnd, title, wpid = found[0]
    win32gui.SetForegroundWindow(hwnd)
    return f"focused: [{wpid}] {title}"


async def move_resize_window(
    title_regex: str = "", x: int = 0, y: int = 0, w: int = 800, h: int = 600, pid: int = 0
) -> str:
    """Move and resize a window."""
    try:
        import win32gui
        import win32process
    except ImportError:
        return "error: pywin32 required (pip install pywin32)"

    found = []

    def enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                if (pid and wpid == pid) or (title_regex and title_regex.lower() in title.lower()):
                    found.append((hwnd, title))

    win32gui.EnumWindows(enum_handler, None)

    if not found:
        return "no matching window found"

    hwnd, title = found[0]
    win32gui.MoveWindow(hwnd, x, y, w, h, True)
    return f"moved/resized: {title} to ({x},{y}) {w}x{h}"


async def send_keys(keys: str, target_window: str = "") -> str:
    """Send keystrokes globally or to a specific window."""
    try:
        import pyautogui
    except ImportError:
        return "error: pyautogui required (pip install pyautogui)"

    if target_window:
        # Focus first then type
        await focus_window(title_regex=target_window)
        await asyncio.sleep(0.1)

    pyautogui.typewrite(keys)
    return f"sent keys: {keys}"


async def get_mouse_position() -> str:
    """Get current mouse position."""
    try:
        import pyautogui

        x, y = pyautogui.position()
        return f"mouse at ({x}, {y})"
    except ImportError:
        return "error: pyautogui required (pip install pyautogui)"


async def system_stats() -> str:
    """Get detailed system statistics."""
    try:
        import psutil
    except ImportError:
        return "error: psutil required (pip install psutil)"

    cpu = psutil.cpu_percent(interval=0.1)
    cpu_per_core = psutil.cpu_percent(interval=0.1, percpu=True)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\" if sys.platform == "win32" else "/")
    net = psutil.net_io_counters()

    lines = [
        f"CPU: {cpu}% total ({', '.join(f'{c}%' for c in cpu_per_core)})",
        f"RAM: {mem.percent}% ({mem.used / 1e9:.1f} / {mem.total / 1e9:.1f} GB)",
        f"Disk: {disk.percent}% ({disk.used / 1e9:.1f} / {disk.total / 1e9:.1f} GB)",
        f"Net: sent {net.bytes_sent / 1e6:.1f} MB, recv {net.bytes_recv / 1e6:.1f} MB",
    ]

    try:
        battery = psutil.sensors_battery()
        if battery:
            lines.append(
                f"Battery: {battery.percent}% {'charging' if battery.power_plugged else 'discharging'}"
            )
    except Exception:
        pass

    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name, entries in temps.items():
                for e in entries[:2]:
                    lines.append(f"Temp {name}: {e.current}°C")
    except Exception:
        pass

    return "\n".join(lines)


async def get_environment() -> str:
    """Get environment variables (filtered for safety)."""
    safe_vars = {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "SHELL",
        "LANG",
        "TERM",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
    }
    lines = []
    for k, v in os.environ.items():
        if any(k.startswith(s) for s in safe_vars) or k in safe_vars:
            lines.append(f"{k}={v[:200]}")
    return "\n".join(sorted(lines))


async def get_disk_usage(path: str = ".") -> str:
    """Get disk usage for a path."""
    try:
        import psutil
    except ImportError:
        return "error: psutil required (pip install psutil)"

    path_obj = Path(path).expanduser().resolve()
    try:
        usage = psutil.disk_usage(str(path_obj))
        return "\n".join(
            [
                f"Path: {path_obj}",
                f"Total: {usage.total / 1e9:.2f} GB",
                f"Used: {usage.used / 1e9:.2f} GB",
                f"Free: {usage.free / 1e9:.2f} GB",
                f"Usage: {usage.percent}%",
            ]
        )
    except Exception as e:
        return f"error: {e}"


async def get_network_info() -> str:
    """Get network interface information."""
    try:
        import psutil
    except ImportError:
        return "error: psutil required (pip install psutil)"

    lines = []
    for name, addrs in psutil.net_if_addrs().items():
        lines.append(f"Interface: {name}")
        for addr in addrs:
            if addr.family == 2:  # AF_INET
                lines.append(f"  IPv4: {addr.address} (netmask: {addr.netmask})")
            elif addr.family == 10:  # AF_INET6
                lines.append(f"  IPv6: {addr.address}")
    return "\n".join(lines) if lines else "no network interfaces"


async def set_wallpaper(image_path: str) -> str:
    """Set desktop wallpaper (Windows only)."""
    if sys.platform != "win32":
        return "error: Windows only"

    path_obj = Path(image_path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: file not found: {image_path}"

    try:
        import ctypes

        ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path_obj), 3)
        return f"wallpaper set to {image_path}"
    except Exception as e:
        return f"error: {e}"


async def lock_screen() -> str:
    """Lock the screen."""
    if sys.platform == "win32":
        os.system("rundll32.exe user32.dll,LockWorkStation")
        return "screen locked"
    else:
        return "error: Windows only"


async def shutdown(restart: bool = False, delay: int = 0) -> str:
    """Shutdown or restart the system."""
    if sys.platform == "win32":
        cmd = "shutdown /r /t" if restart else "shutdown /s /t"
        cmd += f" {delay}"
        os.system(cmd)
        return f"system {'restart' if restart else 'shutdown'} scheduled in {delay}s"
    else:
        return "error: Windows only"


def register_system(box: ToolBox) -> None:
    box.register(
        Tool(
            name="sys_run_command",
            description="Execute a shell command with timeout and output capture",
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "cwd": {"type": "string", "default": ""},
                    "timeout": {"type": "integer", "default": 60},
                    "capture": {"type": "boolean", "default": True},
                },
                "required": ["cmd"],
            },
            handler=run_command,
        )
    )
    box.register(
        Tool(
            name="sys_run_command_async",
            description="Start a command in background and return immediately with PID",
            parameters={
                "type": "object",
                "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string", "default": ""}},
                "required": ["cmd"],
            },
            handler=run_command_async,
        )
    )
    box.register(
        Tool(
            name="sys_kill_process",
            description="Kill a process by PID",
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer"},
                    "force": {"type": "boolean", "default": False},
                },
                "required": ["pid"],
            },
            handler=kill_process,
        )
    )
    box.register(
        Tool(
            name="sys_list_processes",
            description="List running processes with optional filter",
            parameters={
                "type": "object",
                "properties": {"filter": {"type": "string", "default": ""}},
                "required": [],
            },
            handler=list_processes,
        )
    )
    box.register(
        Tool(
            name="sys_launch_app",
            description="Launch an application by name or path",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}, "default": []},
                },
                "required": ["name"],
            },
            handler=launch_app,
        )
    )
    box.register(
        Tool(
            name="sys_get_windows",
            description="Get list of visible windows with titles and PIDs",
            parameters={"type": "object", "properties": {}},
            handler=get_windows,
        )
    )
    box.register(
        Tool(
            name="sys_focus_window",
            description="Bring a window to front by title regex or PID",
            parameters={
                "type": "object",
                "properties": {
                    "title_regex": {"type": "string", "default": ""},
                    "pid": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            handler=focus_window,
        )
    )
    box.register(
        Tool(
            name="sys_move_resize_window",
            description="Move and resize a window",
            parameters={
                "type": "object",
                "properties": {
                    "title_regex": {"type": "string", "default": ""},
                    "x": {"type": "integer", "default": 0},
                    "y": {"type": "integer", "default": 0},
                    "w": {"type": "integer", "default": 800},
                    "h": {"type": "integer", "default": 600},
                    "pid": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            handler=move_resize_window,
        )
    )
    box.register(
        Tool(
            name="sys_send_keys",
            description="Send keystrokes globally or to a specific window",
            parameters={
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                    "target_window": {"type": "string", "default": ""},
                },
                "required": ["keys"],
            },
            handler=send_keys,
        )
    )
    box.register(
        Tool(
            name="sys_get_mouse_position",
            description="Get current mouse position",
            parameters={"type": "object", "properties": {}},
            handler=get_mouse_position,
        )
    )
    box.register(
        Tool(
            name="sys_system_stats",
            description="Get detailed system statistics (CPU, RAM, Disk, Net, Battery, Temps)",
            parameters={"type": "object", "properties": {}},
            handler=system_stats,
        )
    )
    box.register(
        Tool(
            name="sys_get_environment",
            description="Get environment variables (filtered for safety)",
            parameters={"type": "object", "properties": {}},
            handler=get_environment,
        )
    )
    box.register(
        Tool(
            name="sys_get_disk_usage",
            description="Get disk usage for a path",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
            handler=get_disk_usage,
        )
    )
    box.register(
        Tool(
            name="sys_get_network_info",
            description="Get network interface information",
            parameters={"type": "object", "properties": {}},
            handler=get_network_info,
        )
    )
    box.register(
        Tool(
            name="sys_set_wallpaper",
            description="Set desktop wallpaper (Windows only)",
            parameters={
                "type": "object",
                "properties": {"image_path": {"type": "string"}},
                "required": ["image_path"],
            },
            handler=set_wallpaper,
        )
    )
    box.register(
        Tool(
            name="sys_lock_screen",
            description="Lock the screen (Windows only)",
            parameters={"type": "object", "properties": {}},
            handler=lock_screen,
        )
    )
    box.register(
        Tool(
            name="sys_shutdown",
            description="Shutdown or restart the system (Windows only)",
            parameters={
                "type": "object",
                "properties": {
                    "restart": {"type": "boolean", "default": False},
                    "delay": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            handler=shutdown,
        )
    )

"""PC control tools: open apps/URLs, set volume, screenshot, file search,
system info. Fail-soft per action (error strings).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from core.tools import Tool, ToolBox

SHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "shots"


async def open_app(name: str) -> str:
    name = name.strip()
    exe = shutil.which(name)
    if exe:
        subprocess.Popen([exe], close_fds=True)
        return f"launched {name} ({exe})"
    subprocess.Popen(f'start "" "{name}"', shell=True)
    return f"launch request issued for '{name}' via shell"


async def open_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    os.startfile(url)
    return f"opened {url} in default browser"


async def set_volume(percent: int) -> str:
    try:
        from ctypes import POINTER, cast

        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        return "error: pycaw not installed (pip install pycaw comtypes)"
    percent = max(0, min(100, int(percent)))
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    vol = cast(interface, POINTER(IAudioEndpointVolume))
    vol.SetMasterVolumeLevelScalar(percent / 100.0, None)
    return f"system volume set to {percent}%"


async def screenshot() -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"shot_{datetime.now():%Y%m%d_%H%M%S}.png"
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b=[System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height
$g=[System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size)
$bmp.Save('{path}')
"""
    proc = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-Command",
        ps,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0 or not path.exists():
        return f"error: screenshot failed: {err.decode()[:200]}"
    return f"screenshot saved: {path}"


async def search_files(pattern: str, root: str = "", max_results: int = 10) -> str:
    base = Path(root) if root else Path.home()
    dirs = (
        [base / d for d in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")]
        if not root
        else [base]
    )
    found = []
    scanned = 0
    for d in dirs:
        if not d.exists():
            continue
        for p in d.rglob(pattern):
            found.append(str(p))
            scanned += 1
            if len(found) >= max(1, int(max_results)) or scanned > 500:
                break
        if len(found) >= max_results:
            break
    return "\n".join(found) if found else f"no files matching '{pattern}'"


async def system_info() -> str:
    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpu=(Get-CimInstance Win32_Processor).LoadPercentage;"
        "$free=[math]::Round($os.FreePhysicalMemory/1MB,1);"
        "$tot=[math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        '"CPU load: $cpu%";'
        '"RAM: $free GB free / $tot GB";'
        "$b=Get-CimInstance Win32_Battery;"
        'if($b){"Battery: $($b.EstimatedChargeRemaining)%"}'
    )
    proc = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-Command",
        ps,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        return f"error: {err.decode()[:200]}"
    return out.decode().strip()


def register_pc(box: ToolBox) -> None:
    box.register(
        Tool(
            name="open_app",
            description="Launch an application by name (e.g., 'notepad', 'chrome', 'calc', 'code')",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            handler=open_app,
        )
    )
    box.register(
        Tool(
            name="open_url",
            description="Open a URL in the default browser",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=open_url,
        )
    )
    box.register(
        Tool(
            name="set_volume",
            description="Set system master volume (0-100)",
            parameters={
                "type": "object",
                "properties": {"percent": {"type": "integer", "minimum": 0, "maximum": 100}},
                "required": ["percent"],
            },
            handler=set_volume,
        )
    )
    box.register(
        Tool(
            name="screenshot",
            description="Take a full-screen screenshot and save it",
            parameters={"type": "object", "properties": {}},
            handler=screenshot,
        )
    )
    box.register(
        Tool(
            name="search_files",
            description="Search for files by glob pattern in common user folders (Desktop, Documents, Downloads, Pictures, Videos, Music)",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10},
                },
                "required": ["pattern"],
            },
            handler=search_files,
        )
    )
    box.register(
        Tool(
            name="system_info",
            description="Get current CPU load, RAM usage, and battery percentage",
            parameters={"type": "object", "properties": {}},
            handler=system_info,
        )
    )

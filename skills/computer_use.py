"""Computer-use tools: mouse, keyboard, screen capture, window management,
image wait/record. Each action fails soft (error string) so one UI hiccup
doesn't kill the agent turn.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from pathlib import Path

import pyautogui
import pygetwindow as gw
from PIL import Image

from core.config import load_config
from core.tools import Tool, ToolBox

SHOTS_DIR = Path(__file__).resolve().parent.parent / "data" / "shots"

# Initialize pyautogui with config
_cfg = load_config()
if _cfg.computer_use.enabled:
    pyautogui.FAILSAFE = _cfg.computer_use.failsafe
    pyautogui.PAUSE = _cfg.computer_use.pause
else:
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05


@dataclass
class WindowInfo:
    title: str
    left: int
    top: int
    width: int
    height: int
    active: bool


def _get_window(title: str) -> gw.Win32Window | None:
    wins = gw.getWindowsWithTitle(title)
    return wins[0] if wins else None


async def mouse_move(x: int, y: int, duration: float = 0.3) -> str:
    await asyncio.to_thread(pyautogui.moveTo, x, y, duration=duration)
    return f"mouse moved to ({x}, {y})"


async def mouse_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    interval: float = 0.1,
) -> str:
    if x is not None and y is not None:
        await asyncio.to_thread(pyautogui.moveTo, x, y, duration=0.2)
    await asyncio.to_thread(pyautogui.click, clicks=clicks, interval=interval, button=button)
    pos = pyautogui.position()
    return f"clicked {button} {clicks}x at ({pos.x}, {pos.y})"


async def mouse_drag(
    from_x: int, from_y: int, to_x: int, to_y: int, duration: float = 0.5, button: str = "left"
) -> str:
    await asyncio.to_thread(pyautogui.moveTo, from_x, from_y, duration=0.1)
    await asyncio.to_thread(pyautogui.dragTo, to_x, to_y, duration=duration, button=button)
    return f"dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"


async def mouse_scroll(clicks: int, x: int | None = None, y: int | None = None) -> str:
    if x is not None and y is not None:
        await asyncio.to_thread(pyautogui.moveTo, x, y, duration=0.1)
    await asyncio.to_thread(pyautogui.scroll, clicks)
    return f"scrolled {clicks} clicks"


async def keyboard_type(text: str, interval: float = 0.02) -> str:
    await asyncio.to_thread(pyautogui.write, text, interval=interval)
    return f"typed {len(text)} characters"


async def keyboard_press(key: str, presses: int = 1, interval: float = 0.1) -> str:
    await asyncio.to_thread(pyautogui.press, key, presses=presses, interval=interval)
    return f"pressed {key} {presses}x"


async def keyboard_hotkey(keys: list[str]) -> str:
    await asyncio.to_thread(pyautogui.hotkey, *keys)
    return f"hotkey: {'+'.join(keys)}"


async def keyboard_key_down(key: str) -> str:
    await asyncio.to_thread(pyautogui.keyDown, key)
    return f"key down: {key}"


async def keyboard_key_up(key: str) -> str:
    await asyncio.to_thread(pyautogui.keyUp, key)
    return f"key up: {key}"


async def screenshot(region: list[int] | None = None, save: bool = True) -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time())
    path = SHOTS_DIR / f"screen_{timestamp}.png"

    if region and len(region) == 4:
        img = await asyncio.to_thread(pyautogui.screenshot, region=tuple(region))
    else:
        img = await asyncio.to_thread(pyautogui.screenshot)

    if save:
        await asyncio.to_thread(img.save, path)
        return f"screenshot saved: {path}"

    buffered = await asyncio.to_thread(lambda: _img_to_base64(img))
    return f"data:image/png;base64,{buffered}"


def _img_to_base64(img: Image.Image) -> str:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def find_on_screen(
    image_path: str, confidence: float = 0.8, region: list[int] | None = None
) -> str:
    try:
        if region and len(region) == 4:
            loc = await asyncio.to_thread(
                pyautogui.locateOnScreen, image_path, confidence=confidence, region=tuple(region)
            )
        else:
            loc = await asyncio.to_thread(
                pyautogui.locateOnScreen, image_path, confidence=confidence
            )
        if loc:
            return f"found at ({loc.left}, {loc.top}, {loc.width}, {loc.height})"
        return "not found"
    except Exception as e:
        return f"error: {e}"


async def find_text_on_screen(text: str, region: list[int] | None = None) -> str:
    try:
        import pytesseract

        img = await screenshot(region=region, save=False)
        if img.startswith("data:image"):
            import base64

            img_data = base64.b64decode(img.split(",")[1])
            import io

            from PIL import Image

            pil_img = Image.open(io.BytesIO(img_data))
            data = await asyncio.to_thread(
                pytesseract.image_to_data, pil_img, output_type=pytesseract.Output.DICT
            )
            for i, word in enumerate(data["text"]):
                if text.lower() in word.lower() and int(data["conf"][i]) > 30:
                    x = data["left"][i] + data["width"][i] // 2
                    y = data["top"][i] + data["height"][i] // 2
                    if region:
                        x += region[0]
                        y += region[1]
                    return f"found '{text}' at ({x}, {y})"
        return f"text '{text}' not found"
    except ImportError:
        return "error: pytesseract not installed"
    except Exception as e:
        return f"error: {e}"


async def list_windows(filter_title: str = "") -> str:
    wins = gw.getAllWindows()
    result = []
    for w in wins:
        if w.title and (not filter_title or filter_title.lower() in w.title.lower()):
            result.append(
                f"{w.title} | pos=({w.left},{w.top}) size=({w.width},{w.height}) active={w.isActive}"
            )
    return "\n".join(result) if result else "no windows found"


async def focus_window(title: str) -> str:
    win = _get_window(title)
    if not win:
        return f"window '{title}' not found"
    try:
        await asyncio.to_thread(win.activate)
        return f"focused window: {title}"
    except Exception:
        await asyncio.to_thread(win.minimize)
        await asyncio.sleep(0.1)
        await asyncio.to_thread(win.restore)
        return f"focused window (via restore): {title}"


async def move_window(
    title: str, x: int, y: int, width: int | None = None, height: int | None = None
) -> str:
    win = _get_window(title)
    if not win:
        return f"window '{title}' not found"
    await asyncio.to_thread(win.moveTo, x, y)
    if width and height:
        await asyncio.to_thread(win.resizeTo, width, height)
    return f"moved window '{title}' to ({x}, {y})"


async def get_mouse_position() -> str:
    x, y = await asyncio.to_thread(pyautogui.position)
    return f"mouse at ({x}, {y})"


async def wait_for_image(image_path: str, timeout: float = 10.0, confidence: float = 0.8) -> str:
    start = time.time()
    while time.time() - start < timeout:
        try:
            loc = await asyncio.to_thread(
                pyautogui.locateOnScreen, image_path, confidence=confidence
            )
            if loc:
                return f"found at ({loc.left}, {loc.top}) after {time.time() - start:.1f}s"
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return f"timeout: image not found within {timeout}s"


async def record_mouse(duration: float = 5.0) -> str:
    positions = []
    start = time.time()
    while time.time() - start < duration:
        x, y = await asyncio.to_thread(pyautogui.position)
        positions.append((x, y, time.time() - start))
        await asyncio.sleep(0.1)
    return f"recorded {len(positions)} positions over {duration}s: {positions[:5]}..."


def register_computer_use(box: ToolBox) -> None:
    box.register(
        Tool(
            name="mouse_move",
            description="Move mouse to absolute coordinates (x, y)",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "duration": {"type": "number", "default": 0.3},
                },
                "required": ["x", "y"],
            },
            handler=mouse_move,
        )
    )
    box.register(
        Tool(
            name="mouse_click",
            description="Click at current position or specified coordinates",
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "default": "left",
                    },
                    "clicks": {"type": "integer", "default": 1},
                    "interval": {"type": "number", "default": 0.1},
                },
            },
            handler=mouse_click,
        )
    )
    box.register(
        Tool(
            name="mouse_drag",
            description="Drag from one point to another",
            parameters={
                "type": "object",
                "properties": {
                    "from_x": {"type": "integer"},
                    "from_y": {"type": "integer"},
                    "to_x": {"type": "integer"},
                    "to_y": {"type": "integer"},
                    "duration": {"type": "number", "default": 0.5},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "default": "left",
                    },
                },
                "required": ["from_x", "from_y", "to_x", "to_y"],
            },
            handler=mouse_drag,
        )
    )
    box.register(
        Tool(
            name="mouse_scroll",
            description="Scroll mouse wheel (positive = up, negative = down)",
            parameters={
                "type": "object",
                "properties": {
                    "clicks": {"type": "integer"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                },
                "required": ["clicks"],
            },
            handler=mouse_scroll,
        )
    )
    box.register(
        Tool(
            name="keyboard_type",
            description="Type text at current cursor position",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "interval": {"type": "number", "default": 0.02},
                },
                "required": ["text"],
            },
            handler=keyboard_type,
        )
    )
    box.register(
        Tool(
            name="keyboard_press",
            description="Press a key (e.g., 'enter', 'tab', 'escape', 'f1', 'ctrl', 'alt')",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "presses": {"type": "integer", "default": 1},
                    "interval": {"type": "number", "default": 0.1},
                },
                "required": ["key"],
            },
            handler=keyboard_press,
        )
    )
    box.register(
        Tool(
            name="keyboard_hotkey",
            description="Press a key combination (e.g., 'ctrl+c', 'alt+tab', 'win+d')",
            parameters={
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}, "minItems": 2}
                },
                "required": ["keys"],
            },
            handler=keyboard_hotkey,
        )
    )
    box.register(
        Tool(
            name="keyboard_key_down",
            description="Hold a key down (use with key_up)",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=keyboard_key_down,
        )
    )
    box.register(
        Tool(
            name="keyboard_key_up",
            description="Release a held key",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=keyboard_key_up,
        )
    )
    box.register(
        Tool(
            name="screen_capture",
            description="Take screenshot (full screen or region [x,y,w,h]) using pyautogui",
            parameters={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "save": {"type": "boolean", "default": True},
                },
            },
            handler=screenshot,
        )
    )
    box.register(
        Tool(
            name="find_on_screen",
            description="Find an image on screen (template matching)",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string"},
                    "confidence": {"type": "number", "default": 0.8},
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
                "required": ["image_path"],
            },
            handler=find_on_screen,
        )
    )
    box.register(
        Tool(
            name="find_text_on_screen",
            description="Find text on screen using OCR (requires tesseract)",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
                "required": ["text"],
            },
            handler=find_text_on_screen,
        )
    )
    box.register(
        Tool(
            name="list_windows",
            description="List all windows (optionally filter by title)",
            parameters={"type": "object", "properties": {"filter_title": {"type": "string"}}},
            handler=list_windows,
        )
    )
    box.register(
        Tool(
            name="focus_window",
            description="Focus/activate a window by title",
            parameters={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            handler=focus_window,
        )
    )
    box.register(
        Tool(
            name="move_window",
            description="Move and optionally resize a window",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
                "required": ["title", "x", "y"],
            },
            handler=move_window,
        )
    )
    box.register(
        Tool(
            name="get_mouse_position",
            description="Get current mouse coordinates",
            parameters={"type": "object", "properties": {}},
            handler=get_mouse_position,
        )
    )
    box.register(
        Tool(
            name="wait_for_image",
            description="Wait for an image to appear on screen",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string"},
                    "timeout": {"type": "number", "default": 10},
                    "confidence": {"type": "number", "default": 0.8},
                },
                "required": ["image_path"],
            },
            handler=wait_for_image,
        )
    )
    box.register(
        Tool(
            name="record_mouse",
            description="Record mouse positions for duration (debugging)",
            parameters={
                "type": "object",
                "properties": {"duration": {"type": "number", "default": 5.0}},
            },
            handler=record_mouse,
        )
    )

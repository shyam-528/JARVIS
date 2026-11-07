"""PC deep control: matcher + demo/downloads/screenshot-note flows."""

import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import Tool, ToolBox
from skills.pc_flows import match_pc_command, register_pc_flows


def test_matcher_open_variants():
    assert match_pc_command("open chrome") == ("open_app", {"name": "chrome"})
    assert match_pc_command("Launch VS Code!") == ("open_app", {"name": "VS Code"})
    assert match_pc_command("start notepad") == ("open_app", {"name": "notepad"})
    assert match_pc_command("/tasks") is None
    assert match_pc_command("") is None
    assert match_pc_command("hello there") is None
    assert match_pc_command("x") is None


def test_matcher_flows():
    tool, args = match_pc_command("demo prep with chrome, vscode")
    assert tool == "demo_prep" and "chrome" in args["apps"]
    assert match_pc_command("prep my screen for demo")[0] == "demo_prep"
    assert match_pc_command("organize my downloads")[0] == "downloads_organize"
    assert match_pc_command("clean up the downloads")[0] == "downloads_organize"
    assert match_pc_command("take a screenshot note")[0] == "screenshot_note"
    assert match_pc_command("remember this screen")[0] == "screenshot_note"


def _orch(calls=None, memos=None):
    box = ToolBox()

    async def open_app(name=""):
        calls.append(("open_app", name))
        return f"launched {name}"

    async def focus_window(title=""):
        calls.append(("focus_window", title))
        return f"focused {title}"

    async def screen_capture():
        calls.append(("screen_capture", ""))
        return "shot.png"

    for name, fn in (
        ("open_app", open_app),
        ("focus_window", focus_window),
        ("screen_capture", screen_capture),
    ):
        box.register(
            Tool(
                name=name,
                description="x",
                parameters={"type": "object", "properties": {}},
                handler=fn,
            )
        )
    mem = SimpleNamespace(add=lambda t: memos.append(t) or "stored")
    return SimpleNamespace(toolbox=box, memory=mem)


async def test_demo_prep_calls_and_verifies(monkeypatch):
    calls, memos = [], []

    class FakeWin:
        def __init__(self, title):
            self.title = title
            self.minimized = False

        def minimize(self):
            self.minimized = True

    wins = [FakeWin("Chrome - demo"), FakeWin("Random App")]
    fake_gw = types.SimpleNamespace(getAllWindows=lambda: wins)
    monkeypatch.setitem(sys.modules, "pygetwindow", fake_gw)

    box = ToolBox()
    register_pc_flows(box, _orch(calls, memos))
    assert {"demo_prep", "downloads_organize", "screenshot_note"} <= set(box.names())
    out = await box.execute("demo_prep", '{"apps": "chrome"}')
    assert "launched chrome" in out and "minimized 1 other" in out and "shot.png" in out
    assert ("open_app", "chrome") in calls and ("focus_window", "chrome") in calls
    assert wins[1].minimized is True and wins[0].minimized is False


async def test_downloads_organize_moves_and_flags(tmp_path, monkeypatch):

    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / "doc.pdf").write_text("x")
    (dl / "app.exe").write_text("x")
    import time as _t

    _old = _t.time() - 40 * 86400
    os.utime(dl / "app.exe", (_old, _old))
    big = dl / "movie.mp4"
    big.write_bytes(b"\0" * (11 * 1024 * 1024))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    box = ToolBox()
    register_pc_flows(box, SimpleNamespace(toolbox=ToolBox(), memory=None))
    out = await box.execute("downloads_organize", '{"big_mb": 10}')
    assert "3 moved" in out or "moved" in out
    assert (dl / "_sorted_docs" / "doc.pdf").exists()
    assert (dl / "_sorted_installers" / "app.exe").exists()
    assert "Stale installers" in out and "app.exe" in out
    assert "Disk hogs" in out and "movie.mp4" in out


async def test_downloads_already_tidy(tmp_path, monkeypatch):
    (tmp_path / "Downloads" / "_sorted_images").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    box = ToolBox()
    register_pc_flows(box, SimpleNamespace(toolbox=ToolBox(), memory=None))
    out = await box.execute("downloads_organize", "{}")
    assert "already tidy" in out and "_sorted_images" in out


async def test_screenshot_note_stored(monkeypatch):
    import core.vision as _v

    calls, memos = [], []

    class FakeMgr:
        def capture_monitor(self, n):
            return SimpleNamespace(image=object())

        def close(self):
            pass

    class FakeOCR:
        def extract_text(self, img):
            return "ocr words"

    class FakeAnalyzer:
        def __init__(self, orch):
            pass

        async def analyze_with_model(self, image, prompt, ocr_text=""):
            assert "ocr words" in ocr_text
            return {"provider": "fake", "analysis": "a browser window"}

    monkeypatch.setattr(_v, "ScreenCaptureManager", FakeMgr)
    monkeypatch.setattr(_v, "OCREngine", FakeOCR)
    monkeypatch.setattr(_v, "ImageAnalyzer", FakeAnalyzer)
    box = ToolBox()
    register_pc_flows(box, _orch(calls, memos))
    out = await box.execute("screenshot_note", "{}")
    assert "a browser window" in out and "ocr words" in out
    assert memos and "a browser window" in memos[0]


async def test_flows_never_raise_on_broken_orch(monkeypatch):
    box = ToolBox()
    broken = SimpleNamespace(toolbox=SimpleNamespace(execute=None), memory=None)
    register_pc_flows(box, broken)
    monkeypatch.setitem(sys.modules, "pygetwindow", types.SimpleNamespace(getAllWindows=lambda: []))
    # toolbox.execute=None raises TypeError when called: flows must survive
    out = await box.execute("demo_prep", '{"apps": "x"}')
    assert "Demo prep" in out

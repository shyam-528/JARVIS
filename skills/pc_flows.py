"""PC deep control (roadmap #5): computer-use + vision as daily flows.

Free click/type policy (owner-approved): these flows act immediately with
no confirmation gates. Four flows:

  demo_prep           tidy the desktop for a demo (launch/focus apps,
                      minimize the rest, screenshot to verify)
  downloads_organize  beyond triage: live-sort Downloads, flag stale
                      installers, surface disk hogs
  screenshot_note     capture -> OCR + vision summary -> memory note
  open_app            (existing tool) launched by voice/text shortcut

match_pc_command() is the shared NL matcher used by the CLI shortcut
(and covered by tests); Telegram uses explicit /commands.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime
from pathlib import Path

from core.tools import Tool

# ---------- NL matcher (pure, testable) ----------

_OPEN_RE = re.compile(r"^(?:open|launch|start)\s+(?:up\s+)?(.+?)[.!]*$", re.IGNORECASE)
_DEMO_RE = re.compile(
    r"demo\s*prep|prep(?:are)?(?:\s+(?:the|my))?\s+(?:screen|desktop)(?:\s+for(?:\s+a)?\s+demo)?"
    r"|demo\s*mode|get ready to demo",
    re.IGNORECASE,
)
_DOWNLOADS_RE = re.compile(
    r"(?:organiz|sort|clean)(?:e|ing)?(?:\s+up|\s+out)?\s+(?:my\s+|the\s+)?downloads",
    re.IGNORECASE,
)
_SHOTNOTE_RE = re.compile(
    r"screenshot.*(?:note|remember)|(?:note|remember).*(?:screen|this)|"
    r"snap.*note|capture.*note",
    re.IGNORECASE,
)


def match_pc_command(cmd: str) -> tuple[str, dict] | None:
    """Map NL to (tool, args). None when no flow matches. Never raises."""
    try:
        text = " ".join(str(cmd or "").split()).strip()
        if not text or text.startswith("/"):
            return None
        low = text.lower()
        if _SHOTNOTE_RE.search(low):
            return ("screenshot_note", {})
        if _DOWNLOADS_RE.search(low):
            return ("downloads_organize", {})
        if _DEMO_RE.search(low):
            apps = ""
            m = re.search(r"with\s+(.+)$", text, re.IGNORECASE)
            if m:
                apps = m.group(1).strip().rstrip(".!")
            return ("demo_prep", {"apps": apps})
        m = _OPEN_RE.match(text)
        if m:
            target = " ".join(m.group(1).split())
            if len(target) >= 2 and not target.startswith(("http://", "https://")):
                return ("open_app", {"name": target})
    except Exception:
        pass
    return None


# ---------- flows ----------


def register_pc_flows(box, orch) -> None:
    async def demo_prep(apps: str = "", minimize_others: bool = True) -> str:
        """Tidy the desktop for a demo: launch/focus apps, minimize the
        rest, screenshot to verify."""
        lines: list[str] = []
        names = [a.strip() for a in str(apps or "").split(",") if a.strip()][:5]
        for name in names:
            try:
                import json as _jd

                res = await orch.toolbox.execute("open_app", _jd.dumps({"name": name}))
                lines.append(f"🚀 {res[:100]}")
            except Exception as exc:
                lines.append(f"🚀 {name}: failed ({str(exc)[:80]})")
        if names:
            try:
                import json as _jf

                res = await orch.toolbox.execute("focus_window", _jf.dumps({"title": names[0]}))
                lines.append(f"🎯 {res[:100]}")
            except Exception as exc:
                lines.append(f"🎯 focus failed ({str(exc)[:80]})")
        if minimize_others:
            try:
                import pygetwindow as _gw

                keep = names[0].lower() if names else ""
                n = 0
                for w in _gw.getAllWindows():
                    try:
                        title = (w.title or "").strip()
                        if not title or (keep and keep in title.lower()):
                            continue
                        w.minimize()
                        n += 1
                    except Exception:
                        continue
                lines.append(f"🧹 minimized {n} other window(s)")
            except Exception as exc:
                lines.append(f"🧹 minimize skipped ({type(exc).__name__})")
        try:
            shot = await orch.toolbox.execute("screen_capture", "{}")
            lines.append(f"📸 verify: {str(shot)[:160]}")
        except Exception as exc:
            lines.append(f"📸 verify skipped ({str(exc)[:80]})")
        return "Demo prep:\n" + (
            "\n".join(lines) if lines else "(nothing to do — pass apps like 'chrome, vscode')"
        )

    async def downloads_organize(stale_days: int = 30, big_mb: int = 100, limit: int = 50) -> str:
        """Beyond triage: LIVE-sort Downloads by type, flag stale installers
        and disk hogs. Moves files (owner-approved free policy)."""
        import shutil
        import time

        try:
            from skills.triage import RULES
        except Exception:
            return "error: triage rules unavailable"
        inbox = Path.home() / "Downloads"
        if not inbox.is_dir():
            return "Downloads folder not found."
        try:
            stale_days = max(1, int(stale_days or 30))
            big_mb = max(10, int(big_mb or 100))
            limit = max(1, min(int(limit or 50), 200))
        except (ValueError, TypeError):
            stale_days, big_mb, limit = 30, 100, 50
        moved, skipped, stale, hogs = [], 0, [], []
        try:
            files = sorted([f for f in inbox.iterdir() if f.is_file()])
        except Exception as exc:
            return f"error reading Downloads: {exc}"
        if not files:
            try:
                folders = sorted([f.name for f in inbox.iterdir() if f.is_dir()])
            except Exception:
                folders = []
            detail = f" (folders: {', '.join(folders[:5])})" if folders else ""
            return f"Downloads is already tidy — no loose files to sort{detail}."
        now = time.time()
        for f in files:
            if len(moved) >= limit:
                break
            try:
                size_mb = f.stat().st_size / (1024 * 1024)
                age_days = (now - f.stat().st_mtime) / 86400
            except Exception:
                skipped += 1
                continue
            cat = next((c for c, exts in RULES.items() if f.suffix.lower() in exts), None)
            if cat == "installers" and age_days >= stale_days:
                stale.append(f"{f.name} ({age_days:.0f}d old)")
            if size_mb >= big_mb:
                hogs.append(f"{f.name} ({size_mb:.0f} MB)")
            if cat is None:
                skipped += 1
                continue
            dest = inbox / f"_sorted_{cat}" / f.name
            try:
                dest.parent.mkdir(exist_ok=True)
                if dest.exists():
                    skipped += 1
                    continue
                shutil.move(str(f), str(dest))
                moved.append(f"{f.name} -> _sorted_{cat}/")
            except Exception:
                skipped += 1
        lines = [f"Downloads organized: {len(moved)} moved, {skipped} skipped."]
        if stale:
            lines.append(f"Stale installers (>{stale_days}d): " + "; ".join(stale[:5]))
        if hogs:
            lines.append(f"Disk hogs (>{big_mb}MB): " + "; ".join(hogs[:5]))
        if moved:
            lines += moved[:10]
        return "\n".join(lines)[:3000]

    async def screenshot_note(prompt: str = "") -> str:
        """Capture the screen, summarize with vision + OCR, store as a
        searchable memory note. Returns the note."""
        try:
            from core.vision import ImageAnalyzer, OCREngine, ScreenCaptureManager

            mgr = ScreenCaptureManager()
            try:
                cap = mgr.capture_monitor(1)
            finally:
                with contextlib.suppress(Exception):
                    mgr.close()
            ocr = ""
            with contextlib.suppress(Exception):
                ocr = OCREngine().extract_text(cap.image)
            analyzer = ImageAnalyzer(orch)
            result = await analyzer.analyze_with_model(
                cap.image,
                prompt or "Summarize what is on this screen; capture actionable items.",
                ocr_text=ocr,
            )
            if "error" in result:
                return f"Screenshot note failed: {result['error']}"
            note = (
                f"Screen note {datetime.now():%Y-%m-%d %H:%M} "
                f"[{result.get('provider', '?')}]: {result.get('analysis', '')[:1200]}"
            )
            if ocr.strip():
                note += f"\nOCR: {ocr[:500]}"
            with contextlib.suppress(Exception):
                orch.memory.add(note[:1500])
            return note[:2000]
        except Exception as exc:
            return f"Screenshot note failed: {type(exc).__name__}: {str(exc)[:150]}"

    box.register(
        Tool(
            name="demo_prep",
            description="Tidy the desktop for a demo: launch/focus apps (comma list), minimize other windows, screenshot to verify.",
            parameters={
                "type": "object",
                "properties": {
                    "apps": {"type": "string", "default": ""},
                    "minimize_others": {"type": "boolean", "default": True},
                },
            },
            handler=demo_prep,
        )
    )
    box.register(
        Tool(
            name="downloads_organize",
            description="LIVE-sort Downloads by type into _sorted_<cat>/ folders, flag stale installers and big files. Moves files immediately.",
            parameters={
                "type": "object",
                "properties": {
                    "stale_days": {"type": "integer", "default": 30},
                    "big_mb": {"type": "integer", "default": 100},
                    "limit": {"type": "integer", "default": 50},
                },
            },
            handler=downloads_organize,
        )
    )
    box.register(
        Tool(
            name="screenshot_note",
            description="Capture the screen, summarize it with vision + OCR, and store it as a searchable memory note.",
            parameters={
                "type": "object",
                "properties": {"prompt": {"type": "string", "default": ""}},
            },
            handler=screenshot_note,
        )
    )

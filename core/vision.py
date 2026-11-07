"""Screen capture, OCR, and vision-model analysis.

Contract: failures are reported, never invisible. Capture/OCR/provider errors
come back as {"success": False, "error": ...} dicts, {"error": ...} dicts, or
"OCR error: ..." strings so callers (tools, CLI, HUD) can surface them.
The shared capture/OCR helpers are created lazily — importing this module
never touches the display stack, so headless boot can't fail here.
`get_clipboard_image` fail-softs to None (an empty or image-less clipboard is
a normal state), but unexpected read errors are logged.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import mss
import mss.tools
import pytesseract
from PIL import Image

log = logging.getLogger(__name__)


@dataclass
class ScreenCapture:
    image: Image.Image
    monitor: dict
    timestamp: str


class ScreenCaptureManager:
    def __init__(self):
        self._sct = mss.mss()

    def capture_full_screen(self) -> ScreenCapture:
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # all monitors combined
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            return ScreenCapture(
                image=img,
                monitor=monitor,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            )

    def capture_monitor(self, monitor_index: int = 1) -> ScreenCapture:
        with mss.mss() as sct:
            if monitor_index >= len(sct.monitors):
                raise ValueError(f"Monitor {monitor_index} not found")
            monitor = sct.monitors[monitor_index]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            return ScreenCapture(
                image=img,
                monitor=monitor,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            )

    def capture_region(self, left: int, top: int, width: int, height: int) -> ScreenCapture:
        with mss.mss() as sct:
            region = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(region)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            return ScreenCapture(
                image=img,
                monitor=region,
                timestamp=__import__("datetime").datetime.now().isoformat(),
            )

    def save_capture(self, capture: ScreenCapture, path: str | None = None) -> str:
        if path is None:
            path = f"screenshot_{capture.timestamp.replace(':', '-')}.png"
        capture.image.save(path)
        return path

    def list_monitors(self) -> list[dict]:
        return self._sct.monitors[1:]  # skip "all monitors" entry

    def close(self):
        self._sct.close()


class OCREngine:
    def __init__(self, tesseract_cmd: str | None = None):
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        else:
            # Try common Windows install locations
            for path in [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]:
                if os.path.exists(path):
                    pytesseract.pytesseract.tesseract_cmd = path
                    break

    def extract_text(self, image: Image.Image, lang: str = "eng") -> str:
        try:
            return pytesseract.image_to_string(image, lang=lang).strip()
        except Exception as exc:
            return f"OCR error: {exc}"

    def extract_data(self, image: Image.Image, lang: str = "eng") -> dict:
        try:
            return pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)
        except Exception as exc:
            return {"error": str(exc)}


class ImageAnalyzer:
    """Real AI vision: sends images as OpenAI-compatible multimodal content parts."""

    # ONLY multimodal models can accept images — text-only models reject them.
    VISION_PREFERENCE = ["gemini-3.5-flash", "gemini-3.6-flash"]

    def __init__(self, orchestrator=None):
        self.orchestrator = orchestrator

    def image_to_base64(self, image: Image.Image, format: str = "PNG") -> str:
        buffer = io.BytesIO()
        image.save(buffer, format=format)
        return base64.b64encode(buffer.getvalue()).decode()

    def thumbnail_b64(self, image: Image.Image, max_side: int = 640, quality: int = 72) -> str:
        """Downscaled JPEG thumbnail for HUD display (keeps payloads small)."""
        img = image.copy()
        img.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        img.convert("RGB").save(buffer, format="JPEG", quality=quality)
        return base64.b64encode(buffer.getvalue()).decode()

    def _pick_provider(self):
        if not self.orchestrator:
            return None
        by_name = {p.name: p for p in self.orchestrator.cfg.providers}
        # 1) Preferred vision models first
        for name in self.VISION_PREFERENCE:
            p = by_name.get(name)
            if p and p.api_key:
                return p
        # Any gemini-* provider is multimodal even without explicit tags
        for p in self.orchestrator.cfg.providers:
            if p.api_key and p.name.startswith("gemini"):
                return p
        # 2) Any provider tagged vision/multimodal
        for p in self.orchestrator.cfg.providers:
            tags = getattr(p, "tags", []) or []
            if p.api_key and ("vision" in tags or "multimodal" in tags):
                return p
        # 3) Fall back to a tools-capable provider (OpenAI-compatible image_url)
        for p in self.orchestrator.cfg.providers:
            if p.api_key and p.supports_tools:
                return p
        # 4) Any provider with a key as last resort
        for p in self.orchestrator.cfg.providers:
            if p.api_key:
                return p
        return None

    async def analyze_with_model(
        self,
        image: Image.Image,
        prompt: str,
        ocr_text: str = "",
        max_side: int = 1280,
    ) -> dict:
        """Send image + prompt to a vision-capable provider. Returns {analysis, provider} or {error}."""
        provider = self._pick_provider()
        if not provider:
            return {
                "error": "no vision-capable model available (needs a multimodal model like gemini-3.5-flash with an API key)"
            }

        img = image.copy()
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=82)
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

        user_content = [{"type": "text", "text": prompt}]
        if ocr_text.strip():
            user_content.append(
                {"type": "text", "text": "OCR text extracted from the image:\n" + ocr_text[:4000]}
            )
        user_content.append({"type": "image_url", "image_url": {"url": data_url}})

        payload = {
            "model": provider.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are JARVIS, analyzing a screenshot for your owner. Be precise, concise, and technical. Describe what matters: apps, code, errors, UI state.",
                },
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 700,
        }
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{provider.base_url.rstrip('/')}/chat/completions"

        import httpx

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return {"analysis": text.strip(), "provider": provider.name}
        except Exception as exc:
            return {"error": f"vision call failed ({provider.name}): {exc}"}

    def get_image_info(self, image: Image.Image) -> dict:
        return {
            "size": image.size,
            "mode": image.mode,
            "format": image.format,
            "width": image.width,
            "height": image.height,
        }


def get_clipboard_image() -> Image.Image | None:
    """Get image from clipboard if available."""
    try:
        # Windows: use PIL's ImageGrab
        from PIL import ImageGrab

        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
        elif isinstance(img, list) and img and isinstance(img[0], str):
            # File paths in clipboard
            for path in img:
                if Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
                    return Image.open(path)
        return None
    except Exception as exc:
        log.warning("clipboard image read failed: %s", exc)
        return None


# Convenience functions — shared helpers are created lazily so that importing
# this module never opens the display stack (mss at import time can crash a
# headless boot).
_capture_mgr: ScreenCaptureManager | None = None
_ocr: OCREngine | None = None


def _capture_manager() -> ScreenCaptureManager:
    global _capture_mgr
    if _capture_mgr is None:
        _capture_mgr = ScreenCaptureManager()
    return _capture_mgr


def _ocr_engine() -> OCREngine:
    global _ocr
    if _ocr is None:
        _ocr = OCREngine()
    return _ocr


def screenshot(monitor: int = 1, region: tuple | None = None, save: bool = False) -> dict:
    """Take a screenshot and optionally save it."""
    try:
        mgr = _capture_manager()
        if region:
            left, top, width, height = region
            cap = mgr.capture_region(left, top, width, height)
        else:
            cap = mgr.capture_monitor(monitor)
        path = mgr.save_capture(cap) if save else None
        return {"success": True, "path": path, "size": cap.image.size, "monitor": cap.monitor}
    except Exception as e:
        return {"success": False, "error": str(e)}


def ocr_image(image_path: str = None, image: Image.Image | None = None, lang: str = "eng") -> str:
    """Extract text from image via OCR."""
    try:
        if image is None and image_path:
            image = Image.open(image_path)
        if image is None:
            return "Error: no image provided"
        return _ocr_engine().extract_text(image, lang=lang)
    except Exception as e:
        return f"OCR error: {e}"


async def analyze_screen(prompt: str = "Describe what you see on screen") -> dict:
    """Capture screen and analyze with vision model.

    Async: `ImageAnalyzer.analyze_with_model` is a coroutine and must be
    awaited — the old sync wrapper returned the coroutine itself (an
    "analysis" that was never computed).
    """
    try:
        cap = _capture_manager().capture_full_screen()
    except Exception as e:
        return {"success": False, "error": f"capture failed: {e}"}
    analyzer = ImageAnalyzer()
    analysis = await analyzer.analyze_with_model(cap.image, prompt)
    if "error" in analysis:
        return {"success": False, "error": analysis["error"]}
    return {
        "success": True,
        "analysis": analysis,
        "image_info": analyzer.get_image_info(cap.image),
    }

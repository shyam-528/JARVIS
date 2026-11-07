"""Vision tools: screenshot, OCR, image analysis, screen reading, clipboard
image, monitor list. Fail-soft when vision providers are unavailable.
"""

from __future__ import annotations

from core.tools import Tool, ToolBox
from core.vision import (
    ImageAnalyzer,
    OCREngine,
    ScreenCaptureManager,
    get_clipboard_image,
    ocr_image,
    screenshot,
)


def register_vision(box: ToolBox):
    # NOTE: no shared ScreenCaptureManager/OCREngine instances here — each
    # tool below constructs what it needs (or uses module-level helpers), so
    # nothing unused shadows the `ocr` tool function.

    async def take_screenshot(monitor: int = 1, region: str = "", save: bool = False) -> str:
        """Take a screenshot. monitor: monitor number (1-based). region: 'left,top,width,height' for region capture."""
        try:
            if region:
                left, top, width, height = map(int, region.split(","))
                result = screenshot(monitor=1, region=(left, top, width, height), save=True)
            else:
                result = screenshot(monitor=monitor, save=True)
            if result["success"]:
                return f"Screenshot saved to {result['path']} ({result['size'][0]}x{result['size'][1]})"
            return f"Error: {result['error']}"
        except Exception as e:
            return f"Screenshot error: {e}"

    async def ocr(image_path: str = "", lang: str = "eng") -> str:
        """Extract text from an image file via OCR. Provide image_path or leave empty to use clipboard."""
        try:
            if image_path:
                text = ocr_image(image_path=image_path, lang=lang)
            else:
                # Try clipboard
                img = get_clipboard_image()
                if img:
                    text = ocr_image(image=img, lang=lang)
                else:
                    return "No image provided and clipboard is empty"
            return text if text else "No text found in image"
        except Exception as e:
            return f"OCR error: {e}"

    async def analyze_image(prompt: str = "Describe this image", image_path: str = "") -> str:
        """Analyze an image with a vision model. Provide image_path or leave empty for full screen capture."""
        try:
            analyzer = ImageAnalyzer()
            if image_path:
                from PIL import Image

                img = Image.open(image_path)
            else:
                cap = ScreenCaptureManager().capture_full_screen()
                img = cap.image
            return await analyzer.analyze_with_model(img, prompt)
        except Exception as e:
            return f"Analysis error: {e}"

    async def read_screen(prompt: str = "What text is visible on screen?") -> str:
        """Capture full screen and run OCR + analysis."""
        try:
            cap = ScreenCaptureManager().capture_full_screen()
            ocr_engine = OCREngine()
            text = ocr_engine.extract_text(cap.image)
            analyzer = ImageAnalyzer()
            analysis = await analyzer.analyze_with_model(
                cap.image, f"{prompt}\n\nOCR text found:\n{text}"
            )
            return f"OCR text:\n{text}\n\nAnalysis:\n{analysis}"
        except Exception as e:
            return f"Screen reading error: {e}"

    async def clipboard_image() -> str:
        """Get image from clipboard and save it."""
        try:
            img = get_clipboard_image()
            if img:
                import time

                path = f"clipboard_{int(time.time())}.png"
                img.save(path)
                return f"Clipboard image saved to {path} ({img.size[0]}x{img.size[1]})"
            return "No image in clipboard"
        except Exception as e:
            return f"Clipboard error: {e}"

    async def list_monitors() -> str:
        """List available monitors."""
        try:
            mgr = ScreenCaptureManager()
            monitors = mgr.list_monitors()
            mgr.close()
            if not monitors:
                return "No monitors detected"
            lines = []
            for i, m in enumerate(monitors, 1):
                lines.append(f"Monitor {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    box.register(
        Tool(
            name="screenshot",
            description="Take a screenshot. monitor: monitor number (1-based). region: 'left,top,width,height' for region capture. save: true to save file.",
            parameters={
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer", "default": 1},
                    "region": {"type": "string", "description": "left,top,width,height"},
                    "save": {"type": "boolean", "default": False},
                },
            },
            handler=take_screenshot,
        )
    )

    box.register(
        Tool(
            name="ocr",
            description="Extract text from an image via OCR. Provide image_path or leave empty to use clipboard.",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "default": ""},
                    "lang": {"type": "string", "default": "eng"},
                },
            },
            handler=ocr,
        )
    )

    box.register(
        Tool(
            name="analyze_image",
            description="Analyze an image with a vision model. Provide image_path or leave empty for full screen capture.",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "default": "Describe this image"},
                    "image_path": {"type": "string", "default": ""},
                },
            },
            handler=analyze_image,
        )
    )

    box.register(
        Tool(
            name="read_screen",
            description="Capture screen and extract text via OCR, then analyze with vision model.",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "default": "What text is visible on screen?"},
                },
            },
            handler=read_screen,
        )
    )

    box.register(
        Tool(
            name="clipboard_image",
            description="Get image from clipboard and save it as PNG.",
            parameters={"type": "object", "properties": {}},
            handler=clipboard_image,
        )
    )

    box.register(
        Tool(
            name="list_monitors",
            description="List available monitors with their resolutions.",
            parameters={"type": "object", "properties": {}},
            handler=list_monitors,
        )
    )

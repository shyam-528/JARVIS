from __future__ import annotations

import base64

from PIL import Image

from core.vision import ImageAnalyzer


def make_img(w=800, h=600, color=(30, 60, 120)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def test_thumbnail_b64_is_jpeg_and_downscaled():
    a = ImageAnalyzer()
    b64 = a.thumbnail_b64(make_img(1920, 1080), max_side=640)
    raw = base64.b64decode(b64)
    assert raw[:2] == b"\xff\xd8"  # JPEG magic
    img = Image.open(__import__("io").BytesIO(raw))
    assert max(img.size) <= 640


def test_pick_provider_none_without_orchestrator():
    assert ImageAnalyzer(None)._pick_provider() is None


def test_pick_provider_prefers_gemini():
    from core.config import ProviderConfig

    class FakeOrch:
        class cfg:
            providers = [
                ProviderConfig(name="ox-alpha", base_url="x", model="m", api_key="k1"),
                ProviderConfig(name="gemini-flash", base_url="x", model="m", api_key="k2"),
            ]

    a = ImageAnalyzer(FakeOrch())
    assert a._pick_provider().name == "gemini-flash"


def test_pick_provider_falls_back_to_tools_provider():
    from core.config import ProviderConfig

    class FakeOrch:
        class cfg:
            providers = [
                ProviderConfig(name="ox-alpha", base_url="x", model="m", api_key="k1"),
                ProviderConfig(
                    name="groq-gpt-oss-120b",
                    base_url="x",
                    model="m",
                    api_key="k2",
                    supports_tools=True,
                ),
            ]

    assert ImageAnalyzer(FakeOrch())._pick_provider().name == "groq-gpt-oss-120b"


def test_analyze_requires_provider():
    import asyncio

    async def run():
        return await ImageAnalyzer(None).analyze_with_model(make_img(), "describe")

    result = asyncio.run(run())
    assert "error" in result

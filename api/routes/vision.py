"""Screen-capture / OCR / vision-analysis endpoints."""

import contextlib

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import state
from api.state import ROOT, get_auth_token

router = APIRouter()

_shots: dict[str, dict] = {}

_SHOTS_DIR = ROOT / "data" / "shots"


class CaptureRequest(BaseModel):
    monitor: int = 1


class VisionRequest(BaseModel):
    prompt: str = "Describe what you see on this screen"
    shot_id: str | None = None
    ocr: bool = True


@router.get("/api/vision/monitors")
async def vision_monitors(token: str = Depends(get_auth_token)):
    try:
        from core.vision import ScreenCaptureManager

        mgr = ScreenCaptureManager()
        monitors = mgr.list_monitors()
        mgr.close()
        return [
            {"index": i + 1, "width": m["width"], "height": m["height"]}
            for i, m in enumerate(monitors)
        ]
    except Exception as exc:
        return {"error": str(exc)}


@router.post("/api/vision/capture")
async def vision_capture(request: CaptureRequest, token: str = Depends(get_auth_token)):
    import time as _time
    import uuid as _uuid

    try:
        from core.vision import ScreenCaptureManager

        mgr = ScreenCaptureManager()
        cap = mgr.capture_monitor(max(1, request.monitor))
        mgr.close()
        _SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        shot_id = _uuid.uuid4().hex[:10]
        path = _SHOTS_DIR / f"shot_{shot_id}.png"
        cap.image.save(path)
        from core.vision import ImageAnalyzer

        analyzer = ImageAnalyzer()
        thumb = analyzer.thumbnail_b64(cap.image)
        _shots[shot_id] = {
            "id": shot_id,
            "path": str(path),
            "ts": _time.strftime("%H:%M:%S"),
            "w": cap.image.size[0],
            "h": cap.image.size[1],
            "thumb": thumb,
        }
        return {
            "id": shot_id,
            "path": str(path),
            "ts": _shots[shot_id]["ts"],
            "width": cap.image.size[0],
            "height": cap.image.size[1],
            "thumb": thumb,
        }
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/vision/shots")
async def vision_shots(token: str = Depends(get_auth_token)):
    return [{"id": s["id"], "ts": s["ts"], "w": s["w"], "h": s["h"]} for s in _shots.values()]


@router.post("/api/vision/ocr")
async def vision_ocr(shot_id: str, token: str = Depends(get_auth_token)):
    try:
        shot = _shots.get(shot_id)
        if not shot:
            from fastapi import HTTPException

            raise HTTPException(404, "shot not found — capture first")
        from PIL import Image

        from core.vision import OCREngine

        text = OCREngine().extract_text(Image.open(shot["path"]))
        shot["ocr"] = text
        return {"id": shot_id, "text": text, "chars": len(text)}
    except Exception as exc:
        return {"error": str(exc)}


@router.post("/api/vision/analyze")
async def vision_analyze(request: VisionRequest, token: str = Depends(get_auth_token)):
    try:
        from core.vision import ImageAnalyzer, OCREngine, ScreenCaptureManager

        analyzer = ImageAnalyzer(state.orch)
        shot = _shots.get(request.shot_id) if request.shot_id else None
        if shot:
            from PIL import Image

            img = Image.open(shot["path"])
        else:
            mgr = ScreenCaptureManager()
            cap = mgr.capture_monitor(1)
            mgr.close()
            img = cap.image
        ocr_text = ""
        if request.ocr:
            with contextlib.suppress(Exception):
                ocr_text = OCREngine().extract_text(img)
        result = await analyzer.analyze_with_model(img, request.prompt, ocr_text=ocr_text)
        if "error" in result:
            return result
        return {
            "analysis": result["analysis"],
            "provider": result.get("provider", ""),
            "ocr_chars": len(ocr_text),
            "ocr_preview": ocr_text[:600],
        }
    except Exception as exc:
        return {"error": str(exc)}

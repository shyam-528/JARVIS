"""Web-UI support endpoints: usage sparkline + persisted settings."""

import contextlib

from fastapi import APIRouter, Depends, Request

from api import state
from api.state import get_auth_token

router = APIRouter()


@router.get("/api/web/usage")
async def web_usage(days: int = 14, token: str = Depends(get_auth_token)):
    """Messages + tool events per day from the events DB (for dashboard sparkline)."""
    import time as _time

    out = []
    try:
        cutoff = _time.time() - days * 86400
        rows = state.orch.event_bus.conn.execute(
            "SELECT timestamp, event_type FROM events WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,),
        ).fetchall()
        per_day: dict = {}
        for ts, etype in rows:
            day = _time.strftime("%Y-%m-%d", _time.localtime(ts))
            if day not in per_day:
                per_day[day] = {"date": day, "messages": 0, "toolCalls": 0}
            if etype in ("command", "chat"):
                per_day[day]["messages"] += 1
            elif etype == "tool_result":
                per_day[day]["toolCalls"] += 1
        out = sorted(per_day.values(), key=lambda d: d["date"])
    except Exception:
        pass
    if not out:
        import time as _t

        out = [{"date": _t.strftime("%Y-%m-%d"), "messages": 0, "toolCalls": 0}]
    return out


@router.get("/api/web/settings")
async def web_get_settings(token: str = Depends(get_auth_token)):
    """Web UI settings (persisted to data/web_settings.json)."""
    import json as _json

    path = state.config.db_path.parent / "web_settings.json"
    defaults = {
        "voiceEnabled": True,
        "ttsVoice": "",
        "ttsRate": 1.0,
        "sttLanguage": "en-US",
        "addressAs": "sir",
        "persona": "professional",
        "sendOnEnter": True,
        "streamingReplies": True,
    }
    if path.exists():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            defaults.update(data)
        except Exception:
            pass
    return defaults


@router.put("/api/web/settings")
async def web_put_settings(request: Request, token: str = Depends(get_auth_token)):
    """Persist web UI settings; persona changes apply to the live brain."""
    import json as _json

    body = await request.json()
    path = state.config.db_path.parent / "web_settings.json"
    current = {}
    if path.exists():
        with contextlib.suppress(Exception):
            current = _json.loads(path.read_text(encoding="utf-8"))
    current.update(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(current, indent=2), encoding="utf-8")

    # Apply persona to the live orchestrator
    persona = body.get("persona")
    if persona:
        try:
            state.orch.persona.set_active(persona)
            state.orch.history[0]["content"] = state.orch.persona.build_system_prompt(
                state.orch.cfg.system_prompt
            )
        except Exception:
            pass

    # Voice settings apply live: distiller threshold + voice enable
    try:
        if "distillThreshold" in body:
            state.orch.cfg.voice.distill_threshold = int(body["distillThreshold"])
        if "distillSpeech" in body:
            state.orch.cfg.voice.distill_speech = bool(body["distillSpeech"])
        if "ttsEnabled" in body:
            state.orch.cfg.voice.tts_enabled = bool(body["ttsEnabled"])
    except Exception:
        pass
    return {"ok": True, "settings": current}

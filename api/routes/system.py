"""System + auth endpoints: tokens, status, health, metrics, telegram, tools."""

import contextlib
import os
import time
from urllib.parse import urlparse as _urlparse

from fastapi import APIRouter, Depends, HTTPException, Request

from api import state
from api.state import get_auth_token

router = APIRouter()


@router.post("/api/auth/token")
async def create_token(request: Request):
    # P0: bootstrap-only mint. If any active token exists, require auth.
    # Prevents LAN-wide unauthenticated minting (was: anyone could POST hud/365d).
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    name = str(data.get("name") or "").strip()[:64]
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    # Default lifetime comes from config (api.token_expiry_days, default 30).
    cfg_days = 30
    try:
        cfg_days = int(getattr(getattr(state.config, "api", None), "token_expiry_days", 30) or 30)
    except Exception:
        cfg_days = 30
    try:
        expires_days = int(data.get("expires_days") or cfg_days)
    except Exception:
        expires_days = cfg_days
    expires_days = max(1, min(90, expires_days))
    try:
        # Expiry-aware: an all-expired token DB must NOT lock the user out —
        # bootstrap minting re-opens so a fresh token can be minted (refresh).
        has_active = state.auth_manager.has_valid_token()
    except Exception:
        has_active = False
    if has_active:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not state.auth_manager.verify_token(auth[7:]):
            raise HTTPException(status_code=401, detail="Auth required to mint additional tokens")
    token = state.auth_manager.create_token(name, expires_days)
    return {"token": token, "name": name}


@router.get("/api/auth/tokens", dependencies=[Depends(get_auth_token)])
async def list_tokens():
    return state.auth_manager.list_tokens()


@router.delete("/api/auth/token", dependencies=[Depends(get_auth_token)])
async def revoke_token(token: str):
    state.auth_manager.revoke_token(token)
    return {"ok": True}


@router.get("/api/status")
async def status(token: str = Depends(get_auth_token)):
    return {
        "name": state.config.jarvis_name,
        "providers": len(state.config.providers),
        "active": sum(1 for p in state.orch.router.available()),
        "memory_count": state.orch.memory.count(),
        "tasks": len(state.orch.scheduler.list_tasks()),
    }


@router.get("/api/health")
async def health(token: str = Depends(get_auth_token)):
    """One-place system health: brain link, provider chain, TTS, uptime.

    Phase B supervision surface — the website and any watchdog script can
    poll this to know exactly what state JARVIS is in.
    """
    # brain link state (service instance relays chat to the CLI)
    brain = None
    if state.brain_proxy is not None:
        brain = {
            "connected": state.brain_proxy.connected,
            "brain_id": state.brain_proxy.brain_id,
        }

    # provider chain: ready / cooling / over-budget counts
    providers_total = len(state.orch.router.providers)
    providers_ready = sum(1 for p in state.orch.router.available())
    chain = []
    for p in state.orch.router.providers[:10]:
        entry = {
            "name": p.name,
            "model": p.model,
            "ready": p in state.orch.router.available(),
            "success": getattr(p, "_success_count", 0),
            "failure": getattr(p, "_failure_count", 0),
        }
        chain.append(entry)

    # TTS engine state
    tts = {
        "engine_pref": state.orch.cfg.voice.tts_engine,
        "gemini_voice": getattr(state.orch.cfg.voice, "gemini_voice", "Kore"),
        "edge_voice": state.orch.cfg.voice.tts_voice,
        "enabled": state.orch.cfg.voice.tts_enabled,
    }

    # uptime + memory + session info
    uptime_s = round(time.time() - state._BOOT_TS, 1)
    try:
        from core.single_owner import read_owner as _read_owner

        tg_owner = _read_owner(state.config.db_path.parent)
    except Exception:
        tg_owner = None
    try:
        mesh_bound = bool(
            getattr(state.orch, "mesh", None) is not None
            and getattr(state.orch.mesh, "running", False)
        )
    except Exception:
        mesh_bound = False
    try:
        sched_tasks = state.orch.scheduler.list_tasks()
        sched_info = {
            "total": len(sched_tasks),
            "enabled": sum(1 for t in sched_tasks if t.enabled),
            "next_runs": sorted(
                [t.next_run or "" for t in sched_tasks if t.enabled and t.next_run]
            )[:3],
            "errors": [
                f"{t.name}: {t.last_error}" for t in sched_tasks if getattr(t, "last_error", None)
            ][:5],
        }
    except Exception:
        sched_info = {}
    # T6 (ROADMAP P2): vector-search state — "off" means FTS-only and must
    # be loud on any health consumer. Never breaks the health payload.
    try:
        from core.orchestrator import _embeddings_status as _emb_status

        embeddings = _emb_status(getattr(state.orch, "knowledge_store", None))
    except Exception:
        embeddings = {"state": "unknown"}
    return {
        "ok": True,
        "uptime_s": uptime_s,
        "instance": os.environ.get("JARVIS_SERVICE_INSTANCE", "cli"),
        "brain": brain,
        "telegram_owner": tg_owner,
        "mesh_bound": mesh_bound,
        "scheduler": sched_info,
        "embeddings": embeddings,
        "providers": {
            "total": providers_total,
            "ready": providers_ready,
            "cooling": providers_total - providers_ready,
            "chain": chain,
        },
        "tts": tts,
        "memory": state.orch.memory.count(),
        "session_msgs": len(state.orch.history),
    }


@router.get("/api/metrics")
async def metrics(token: str = Depends(get_auth_token)):
    import time as _time

    import psutil

    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\" if os.name == "nt" else "/")
    proc = psutil.Process()
    return {
        "cpu": round(cpu, 1),
        "cpu_cores": psutil.cpu_count(),
        "ram_used_gb": round(mem.used / 1e9, 2),
        "ram_total_gb": round(mem.total / 1e9, 2),
        "ram_pct": round(mem.percent, 1),
        "disk_pct": round(disk.percent, 1),
        "jarvis_mem_mb": round(proc.memory_info().rss / 1e6, 1),
        "jarvis_cpu": round(proc.cpu_percent(interval=None), 1),
        "uptime_s": int(_time.time() - psutil.boot_time()),
        "net_sent_mb": round(psutil.net_io_counters().bytes_sent / 1e6, 1),
        "net_recv_mb": round(psutil.net_io_counters().bytes_recv / 1e6, 1),
        # P2: router section — token burn, live TPM, latency, breaker states,
        # and per-key pools (proves multi-key splits like the 2-key XKIRO
        # setup actually balance). Every subsection is best-effort: metrics
        # must never break the HUD that polls them.
        "router": _router_metrics(),
    }


def _router_metrics() -> dict:
    """Failover-router snapshot for /api/metrics. Never raises."""
    out: dict = {
        "usage_today": [],
        "tpm": {},
        "latency": {},
        "breakers": {},
        "key_pools": [],
        "total_hops": 0,
        "last_brain": None,
    }
    try:
        orch = state.orch
        router = orch.router
        ledger = router.ledger
    except Exception:
        return out
    with contextlib.suppress(Exception):
        out["usage_today"] = [{"provider": n, "tokens": t} for n, t in ledger.summary()]
    with contextlib.suppress(Exception):
        out["tpm"] = {
            p.name: ledger.tpm_estimate(p.name)
            for p in router.providers
            if ledger.tpm_estimate(p.name) > 0
        }
    with contextlib.suppress(Exception):
        out["latency"] = ledger.latency_stats()
    with contextlib.suppress(Exception):
        out["breakers"] = {
            p.name: {
                "state": router.breakers.describe(p.name)[0],
                "wait_s": round(router.breakers.describe(p.name)[1], 1),
            }
            for p in router.providers
        }
    with contextlib.suppress(Exception):
        out["key_pools"] = _key_pool_usage(router, ledger)
    with contextlib.suppress(Exception):
        out["total_hops"] = router.total_hops
        out["last_brain"] = router.last_provider
    return out


def _key_pool_usage(router, ledger) -> list[dict]:
    """Today's token burn grouped by (host, distinct-key-index).

    Never emits key material — pools are labeled `host#1`, `host#2`.
    A healthy 2-key XKIRO split shows two pools sharing the load instead
    of one pool taking all 35 rows of failover hammering.
    """
    host_keys: dict[str, list] = {}
    for p in router.providers:
        try:
            host = (_urlparse(getattr(p, "base_url", "")).hostname or "?").split(".")[-2]
        except Exception:
            host = "?"
        host_keys.setdefault(host, [])
        if p.api_key not in host_keys[host]:
            host_keys[host].append(p.api_key)
    pools: dict[str, int] = {}
    for p in router.providers:
        try:
            host = (_urlparse(getattr(p, "base_url", "")).hostname or "?").split(".")[-2]
        except Exception:
            host = "?"
        idx = host_keys[host].index(p.api_key) + 1
        label = f"{host}#{idx}"
        pools[label] = pools.get(label, 0) + ledger.today(p.name)
    return [{"pool": label, "tokens": tokens} for label, tokens in sorted(pools.items())]


@router.get("/api/telegram/status")
async def telegram_status(token: str = Depends(get_auth_token)):
    """Get Telegram bot status"""
    from core.config import load_config

    config = load_config()
    tg_cfg = config.telegram
    return {
        "enabled": tg_cfg.enabled,
        "configured": bool(tg_cfg.bot_token and tg_cfg.chat_id),
        "bot_token_set": bool(tg_cfg.bot_token),
        "chat_id_set": bool(tg_cfg.chat_id),
    }


@router.get("/api/tools")
async def tools_list(token: str = Depends(get_auth_token)):
    return state.orch.toolbox.names()


@router.get("/api/audio")
async def audio_devices(token: str = Depends(get_auth_token)):
    try:
        from voice.audio import list_devices

        return list_devices()
    except Exception as exc:
        return {"error": str(exc)}

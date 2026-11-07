"""P2 tests: T6 embedder flag + router metrics surface.

T6: vector-search state must be product-visible (stats/health/HUD), because
an "off" embedder otherwise degrades every search to FTS-only invisibly.
Metrics: /api/metrics carries a best-effort router section (usage, TPM,
latency, breakers, per-key pools) that never breaks the polling HUD —
including when the orch is partial or missing pieces.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import api.state
from api.routes import agents as agents_routes
from api.routes import system as system_routes
from core.knowledge import KnowledgeStore
from core.orchestrator import Orchestrator, _embeddings_status


def _store(tmp_path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "k.db")


# ---------- T6: embedder_status ----------


def test_embedder_not_tried_yet_before_first_search(tmp_path):
    assert _store(tmp_path).embedder_status() == {"state": "not-tried-yet"}


def test_embedder_off_after_failed_load(tmp_path):
    st = _store(tmp_path)
    st._embedder_loaded = True
    st._embedder = None
    assert st.embedder_status() == {"state": "off"}


def test_embedder_on_after_successful_load(tmp_path):
    st = _store(tmp_path)
    st._embedder_loaded = True
    st._embedder = object()  # any non-None value means "loaded ok"
    assert st.embedder_status() == {"state": "on"}


def test_embedder_status_never_raises():
    assert _embeddings_status(None) == {"state": "unknown"}
    assert _embeddings_status(SimpleNamespace()) == {"state": "unknown"}
    assert _embeddings_status(SimpleNamespace(embedder_status=lambda: "garbage")) == {
        "state": "unknown"
    }


def test_stats_carries_embeddings():
    orch = SimpleNamespace(
        ledger=SimpleNamespace(summary=lambda: []),
        router=SimpleNamespace(total_hops=0, last_provider=None),
        history=[],
        context_budget_tokens=24000,
        _job_errors=[],
        knowledge_store=SimpleNamespace(embedder_status=lambda: {"state": "off"}),
    )
    assert Orchestrator.stats(orch)["embeddings"] == {"state": "off"}


def test_stats_without_store_reports_unknown():
    orch = SimpleNamespace(
        ledger=SimpleNamespace(summary=lambda: []),
        router=SimpleNamespace(total_hops=0, last_provider=None),
        history=[],
        context_budget_tokens=24000,
    )
    assert Orchestrator.stats(orch)["embeddings"] == {"state": "unknown"}


def test_api_stats_passes_embeddings_through():
    orch = SimpleNamespace(
        stats=lambda: {
            "usage": [],
            "total_hops": 0,
            "last_brain": None,
            "context_messages": 0,
            "context_budget": 0,
            "embeddings": {"state": "on"},
        }
    )
    old = api.state.orch
    try:
        api.state.orch = orch
        out = asyncio.run(agents_routes.stats(token="t"))
        assert out["embeddings"] == {"state": "on"}
    finally:
        api.state.orch = old


# ---------- metrics ----------


def _providers():
    return [
        SimpleNamespace(name="xkiro-a", base_url="https://api.xkiro.com/v1", api_key="K1"),
        SimpleNamespace(name="xkiro-b", base_url="https://api.xkiro.com/v1", api_key="K1"),
        SimpleNamespace(name="xkiro-c", base_url="https://api.xkiro.com/v1", api_key="K2"),
        SimpleNamespace(name="groq-a", base_url="https://api.groq.com/openai/v1", api_key="G1"),
    ]


def _ledger(today=None, tpm=None, lat=None):
    today = today or {}
    tpm = tpm or {}
    return SimpleNamespace(
        summary=lambda: [(n, t) for n, t in today.items()],
        today=lambda n: today.get(n, 0),
        tpm_estimate=lambda n: tpm.get(n, 0),
        latency_stats=lambda: lat or {},
    )


def _breakers(states=None):
    states = states or {}
    return SimpleNamespace(
        describe=lambda n: states.get(n, ("ok", 0.0)),
    )


def _orch(**kw):
    providers = _providers()
    kw.setdefault("ledger", _ledger())
    router = SimpleNamespace(
        providers=providers,
        ledger=kw.pop("ledger"),
        breakers=kw.pop("breakers", _breakers()),
        total_hops=kw.pop("total_hops", 0),
        last_provider=kw.pop("last_brain", None),
    )
    return SimpleNamespace(router=router, **kw)


def test_metrics_router_section_shape():
    orch = _orch(
        ledger=_ledger(
            today={"xkiro-a": 1000, "xkiro-c": 500},
            tpm={"xkiro-a": 200},
            lat={"xkiro-a": {"calls": 2, "first_token_avg_ms": 900, "first_token_p95_ms": 1200}},
        ),
        breakers=_breakers({"xkiro-b": ("rate", 45.0)}),
        total_hops=7,
        last_brain="xkiro-a",
    )
    old = api.state.orch
    try:
        api.state.orch = orch
        out = asyncio.run(system_routes.metrics(token="t"))
    finally:
        api.state.orch = old
    assert "cpu" in out and "router" in out  # host section untouched
    r = out["router"]
    assert r["usage_today"] == [
        {"provider": "xkiro-a", "tokens": 1000},
        {"provider": "xkiro-c", "tokens": 500},
    ]
    assert r["total_hops"] == 7 and r["last_brain"] == "xkiro-a"
    assert r["tpm"] == {"xkiro-a": 200}
    assert r["latency"]["xkiro-a"]["first_token_p95_ms"] == 1200
    assert r["breakers"]["xkiro-b"] == {"state": "rate", "wait_s": 45.0}
    assert r["breakers"]["xkiro-a"] == {"state": "ok", "wait_s": 0.0}
    pools = {p["pool"]: p["tokens"] for p in r["key_pools"]}
    assert pools["xkiro#1"] == 1000 and pools["xkiro#2"] == 500
    # no key material ever leaves the server
    assert "K1" not in json.dumps(out) and "K2" not in json.dumps(out)


def test_metrics_never_breaks_on_partial_orch():
    for orch in (
        SimpleNamespace(),  # nothing at all
        SimpleNamespace(router=SimpleNamespace()),  # router without pieces
    ):
        old = api.state.orch
        try:
            api.state.orch = orch
            out = asyncio.run(system_routes.metrics(token="t"))
        finally:
            api.state.orch = old
        assert out["router"]["usage_today"] == []
        assert out["router"]["key_pools"] == []
        assert "cpu" in out  # host section still served


def test_health_carries_embeddings():
    orch = SimpleNamespace(
        router=SimpleNamespace(providers=[], available=lambda: []),
        cfg=SimpleNamespace(
            voice=SimpleNamespace(
                tts_engine="auto", gemini_voice="Kore", tts_voice="v", tts_enabled=True
            ),
        ),
        scheduler=SimpleNamespace(list_tasks=lambda: []),
        memory=SimpleNamespace(count=lambda: 0),
        history=[],
        mesh=None,
        knowledge_store=SimpleNamespace(embedder_status=lambda: {"state": "off"}),
    )
    cfg = SimpleNamespace(db_path=SimpleNamespace(parent=SimpleNamespace()))
    old_orch, old_cfg, old_proxy = api.state.orch, api.state.config, api.state.brain_proxy
    try:
        api.state.orch, api.state.config, api.state.brain_proxy = orch, cfg, None
        out = asyncio.run(system_routes.health(token="t"))
    finally:
        api.state.orch, api.state.config, api.state.brain_proxy = old_orch, old_cfg, old_proxy
    assert out["embeddings"] == {"state": "off"}
    assert out["ok"] is True

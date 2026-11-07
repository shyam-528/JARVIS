"""Regression tests for the api/main.py -> api/routes/* split (arch step 4).

Locks the exact pre-split route table (61 routes), the shared-state access
pattern (routers must read api/state.py attributes at request time), and the
back-compat surface of api.main (app object, configure_from_external,
module __getattr__ shim used by scripts/web_exercise.py).
"""

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import api.main
import api.state
from api.routes import ALL_ROUTERS
from api.routes import memory as memory_routes
from api.routes import tasks as tasks_routes

# Exact route table captured from the monolithic api/main.py before the split.
EXPECTED_ROUTES = {
    "GET /api/agents",
    "GET /api/audio",
    "DELETE /api/auth/token",
    "POST /api/auth/token",
    "GET /api/auth/tokens",
    "GET /api/automation",
    "GET /api/brains",
    "GET /api/chain",
    "POST /api/chat",
    "GET /api/conversations",
    "GET /api/conversations/current",
    "POST /api/conversations/new",
    "GET /api/conversations/search",
    "DELETE /api/conversations/{session_id}",
    "GET /api/conversations/{session_id}",
    "POST /api/conversations/{session_id}/resume",
    "GET /api/health",
    "DELETE /api/knowledge/clear",
    "POST /api/knowledge/index",
    "POST /api/knowledge/search",
    "GET /api/knowledge/sources",
    "GET /api/knowledge/stats",
    "DELETE /api/memory",
    "GET /api/memory",
    "POST /api/memory",
    "GET /api/metrics",
    "GET /api/ml/stats",
    "GET /api/ml/suggestions",
    "GET /api/plugins",
    "DELETE /api/preferences",
    "GET /api/preferences",
    "PUT /api/preferences",
    "GET /api/proactive",
    "GET /api/stats",
    "GET /api/status",
    "GET /api/tasks",
    "POST /api/tasks",
    "GET /api/tasks/errors",
    "DELETE /api/tasks/{task_id}",
    "POST /api/tasks/{task_id}/toggle",
    "GET /api/telegram/status",
    "GET /api/tools",
    "POST /api/vision/analyze",
    "POST /api/vision/capture",
    "GET /api/vision/monitors",
    "POST /api/vision/ocr",
    "GET /api/vision/shots",
    "GET /api/voice/config",
    "POST /api/voice/distill",
    "POST /api/voice/say",
    "POST /api/voice/say/stream",
    "POST /api/voice/test",
    "GET /api/web/insights",
    "GET /api/web/settings",
    "PUT /api/web/settings",
    "GET /api/web/usage",
    "GET /api/workflows",
    "GET /api/workflows/runs",
    "DELETE /api/workflows/{wf_id}",
    "POST /api/workflows/{wf_id}/run",
    "POST /api/workflows/{wf_id}/toggle",
    "WS /ws",
}


def _flatten(app):
    """app.routes with this FastAPI version's lazy _IncludedRouter resolved."""
    out = []
    for r in app.routes:
        inner = getattr(r, "original_router", None)
        out.extend(inner.routes if inner is not None else [r])
    return out


def _route_set(app):
    result = set()
    for r in _flatten(app):
        path = getattr(r, "path", "")
        if not (path.startswith("/api") or path == "/ws"):
            continue
        for method in sorted(getattr(r, "methods", None) or {"WS"}):
            result.add(f"{method} {path}")
    return result


def test_route_table_parity():
    """The split must not add, drop or re-method any endpoint."""
    assert _route_set(api.main.app) == EXPECTED_ROUTES


def test_nine_domain_routers_registered():
    assert len(ALL_ROUTERS) == 9
    # every router contributes at least one route
    assert all(len(r.routes) > 0 for r in ALL_ROUTERS)


def test_conversations_literal_routes_precede_param_route():
    """/search, /current, /new must be registered before /{session_id}
    (FastAPI matches in order; otherwise 'search' becomes a session id)."""
    paths = [r.path for r in memory_routes.router.routes]
    param_idx = paths.index("/api/conversations/{session_id}")
    for literal in ("/api/conversations/search", "/api/conversations/current"):
        assert paths.index(literal) < param_idx


def test_configure_from_external_reexported():
    assert api.main.configure_from_external is api.state.configure_from_external


def test_module_getattr_shim_forwards_live_state():
    """scripts/web_exercise.py does `M.auth_manager` — must reflect state."""
    sentinel = object()
    old = api.state.auth_manager
    try:
        api.state.auth_manager = sentinel
        assert api.main.auth_manager is sentinel
    finally:
        api.state.auth_manager = old
    with pytest.raises(AttributeError):
        _ = api.main.no_such_attribute


def test_router_reads_state_at_request_time():
    """Handlers must not bind orch at import: swapping state.orch later works."""
    fake_orch = type(
        "FakeOrch",
        (),
        {"scheduler": type("FakeSched", (), {"list_tasks": staticmethod(list)})()},
    )()
    old = api.state.orch
    try:
        api.state.orch = fake_orch
        assert asyncio.run(tasks_routes.list_tasks(token="t")) == []
    finally:
        api.state.orch = old


def test_get_auth_token_rejects_missing_header():
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    with pytest.raises(HTTPException) as exc:
        api.state.get_auth_token(Request(scope))
    assert exc.value.status_code == 401

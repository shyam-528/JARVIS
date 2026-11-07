"""Agent pool / brains / provider chain / stats / ML / plugins / insights."""

from fastapi import APIRouter, Depends

from api import state
from api.state import get_auth_token

router = APIRouter()


@router.get("/api/agents")
async def agents_overview(token: str = Depends(get_auth_token)):
    """Specialist agent pool overview (names, icons, descriptions, busy state)."""
    if getattr(state.orch, "agent_pool", None) is None:
        return []
    return state.orch.agent_pool.agents_overview()


@router.get("/api/brains")
async def brains_list(token: str = Depends(get_auth_token)):
    """Brain manager assignments: dedicated provider brains per agent."""
    mgr = getattr(state.orch, "brain_manager", None)
    if mgr is None:
        return []
    return [
        {"agent": a.agent, "primary": a.primary, "fallbacks": a.fallbacks, "reason": a.reason}
        for a in mgr.all()
    ]


@router.get("/api/chain")
async def chain(token: str = Depends(get_auth_token)):
    return [
        {
            "priority": p,
            "name": n,
            "model": m,
            "state": s,
            "budget_left": b,
        }
        for p, n, m, s, b in state.orch.chain_status()
    ]


@router.get("/api/stats")
async def stats(token: str = Depends(get_auth_token)):
    s = state.orch.stats()
    return {
        "usage": [{"provider": n, "tokens": t} for n, t in s["usage"]],
        "total_hops": s["total_hops"],
        "last_brain": s["last_brain"],
        "context_messages": s["context_messages"],
        "context_budget": s["context_budget"],
        # T6: vector-search state for the HUD system panel.
        "embeddings": s.get("embeddings", {"state": "unknown"}),
        "scheduler_job_errors": s.get("scheduler_job_errors", []),
        # Defect #1: per-turn context accounting for the HUD system panel.
        "context_usage": s.get("context_usage", {}),
    }


@router.get("/api/ml/stats")
async def ml_stats(token: str = Depends(get_auth_token)):
    try:
        return state.orch.ml_proactive.get_stats()
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/ml/suggestions")
async def ml_suggestions(token: str = Depends(get_auth_token)):
    try:
        return [
            {
                "type": s.type.value,
                "title": s.title,
                "description": s.description,
                "confidence": s.confidence,
            }
            for s in state.orch.ml_proactive.get_suggestions({})
        ]
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/plugins")
async def plugins(token: str = Depends(get_auth_token)):
    try:
        return [
            {
                "id": s.manifest.name,
                "version": s.manifest.version,
                "description": s.manifest.description,
                "source": s.source,
            }
            for s in state.orch.registry.list_installed()
        ]
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/api/web/insights")
async def web_insights(token: str = Depends(get_auth_token)):
    """ML-proactive suggestions + self-improvement lessons as insight cards."""
    insights = []
    try:
        for s in state.orch.ml_proactive.get_suggestions():
            d = s.__dict__ if hasattr(s, "__dict__") else dict(s)
            insights.append(
                {
                    "id": f"ml-{len(insights)}",
                    "kind": "suggestion",
                    "title": d.get("title", "Suggestion"),
                    "detail": d.get("description", ""),
                    "confidence": float(d.get("confidence", 0.5)),
                }
            )
    except Exception:
        pass
    try:
        for i, lesson in enumerate(state.orch.self_improve.top_lessons(limit=3, min_occurrences=2)):
            insights.append(
                {
                    "id": f"lesson-{i}",
                    "kind": "pattern",
                    "title": "Learned quality rule",
                    "detail": lesson,
                    "confidence": 0.8,
                }
            )
    except Exception:
        pass
    return insights

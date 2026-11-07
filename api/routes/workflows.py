"""Workflow list/toggle/run/delete + run history."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import state
from api.state import get_auth_token

router = APIRouter()


class WorkflowToggle(BaseModel):
    enabled: bool


@router.get("/api/workflows")
async def workflows_list(token: str = Depends(get_auth_token)):
    """All workflows with trigger info and step summaries."""
    engine = getattr(state.orch, "workflow_engine", None)
    if engine is None:
        return []
    out = []
    for spec in engine.list():
        d = spec.to_dict()
        d["trigger"] = spec.trigger.get("type", "manual")
        out.append(d)
    return out


@router.post("/api/workflows/{wf_id}/toggle")
async def workflow_toggle(wf_id: str, body: WorkflowToggle, token: str = Depends(get_auth_token)):
    """Enable/disable a workflow."""
    engine = getattr(state.orch, "workflow_engine", None)
    if engine is None:
        raise HTTPException(status_code=404, detail="no workflow engine")
    ok = engine.set_enabled(wf_id, body.enabled)
    return {"ok": ok}


@router.post("/api/workflows/{wf_id}/run")
async def workflow_run(wf_id: str, token: str = Depends(get_auth_token)):
    """Run a workflow now (background)."""
    engine = getattr(state.orch, "workflow_engine", None)
    if engine is None:
        raise HTTPException(status_code=404, detail="no workflow engine")
    spec = engine.get(wf_id)
    if not spec:
        raise HTTPException(status_code=404, detail=f"workflow {wf_id} not found")
    import asyncio as _aio

    _task = _aio.create_task(engine.run(wf_id))
    return {"ok": True, "started": spec.name}


@router.delete("/api/workflows/{wf_id}")
async def workflow_delete(wf_id: str, token: str = Depends(get_auth_token)):
    """Delete a workflow."""
    engine = getattr(state.orch, "workflow_engine", None)
    if engine is None:
        raise HTTPException(status_code=404, detail="no workflow engine")
    ok = engine.delete(wf_id)
    return {"ok": ok}


@router.get("/api/workflows/runs")
async def workflow_runs(limit: int = 20, token: str = Depends(get_auth_token)):
    """Recent workflow runs for the dashboard/automations page."""
    engine = getattr(state.orch, "workflow_engine", None)
    if engine is None:
        return []
    return engine.run_history(limit=limit)

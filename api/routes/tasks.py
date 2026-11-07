"""Scheduled tasks, proactive suggestions, automation rules."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import state
from api.state import get_auth_token

router = APIRouter()


class TaskCreate(BaseModel):
    name: str
    trigger_type: str
    trigger_spec: dict
    action: str
    action_args: dict = {}
    max_runs: int | None = None


class TaskResponse(BaseModel):
    id: str
    name: str
    trigger_type: str
    trigger_spec: dict
    action: str
    action_args: dict
    next_run: str | None
    run_count: int
    enabled: bool


class TaskToggle(BaseModel):
    enabled: bool


@router.get("/api/tasks", response_model=list[TaskResponse])
async def list_tasks(token: str = Depends(get_auth_token)):
    tasks = state.orch.scheduler.list_tasks()
    return [
        TaskResponse(
            id=t.id,
            name=t.name,
            trigger_type=t.trigger_type,
            trigger_spec=t.trigger_spec,
            action=t.action,
            action_args=t.action_args,
            next_run=t.next_run,
            run_count=t.run_count,
            enabled=t.enabled,
        )
        for t in tasks
    ]


@router.post("/api/tasks", response_model=TaskResponse)
async def create_task(request: TaskCreate, token: str = Depends(get_auth_token)):
    task_id = state.orch.scheduler.add_task(
        request.name,
        request.trigger_type,
        request.trigger_spec,
        request.action,
        request.action_args,
        request.max_runs,
    )
    task = state.orch.scheduler.get_task(task_id)
    return TaskResponse(
        id=task.id,
        name=task.name,
        trigger_type=task.trigger_type,
        trigger_spec=task.trigger_spec,
        action=task.action,
        action_args=task.action_args,
        next_run=task.next_run,
        run_count=task.run_count,
        enabled=task.enabled,
    )


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, token: str = Depends(get_auth_token)):
    ok = state.orch.scheduler.remove_task(task_id)
    return {"ok": ok}


@router.get("/api/tasks/errors")
async def list_task_errors(token: str = Depends(get_auth_token)):
    """Boot-time job registration failures (T3, ROADMAP P0).

    A job that failed to register simply never runs — this surfaces those
    failures to the dashboard/HUD instead of the boot console only.
    """
    return {"errors": list(getattr(state.orch, "_job_errors", []))}


@router.post("/api/tasks/{task_id}/toggle")
async def toggle_task(task_id: str, body: TaskToggle, token: str = Depends(get_auth_token)):
    """Enable/disable a scheduled task (web tasks page toggle)."""
    ok = state.orch.scheduler.enable_task(task_id, body.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True}


@router.get("/api/proactive")
async def get_proactive(token: str = Depends(get_auth_token)):
    return [s.__dict__ for s in state.orch.proactive.get_active(10)]


@router.get("/api/automation")
async def list_automation(token: str = Depends(get_auth_token)):
    try:
        from core.automation import AutomationEngine

        ae = AutomationEngine(state.orch.cfg, state.orch)
        return ae.list_rules_formatted()
    except Exception as exc:
        return {"error": str(exc)}

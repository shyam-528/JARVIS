"""Workflow tools: create, run, list, inspect, delete, and toggle multi-step
workflows on the workflow engine.
"""

from __future__ import annotations

import json

from core.tools import Tool, ToolBox


def register_workflow(box: ToolBox, engine) -> None:
    async def run_workflow(workflow_id: str, variables_json: str = "{}") -> str:
        """Run a workflow in the background; returns run_id immediately."""
        try:
            variables = json.loads(variables_json) if variables_json else {}
        except json.JSONDecodeError as exc:
            return f"error: bad variables JSON ({exc})"
        try:
            run_id = await engine.run(workflow_id, variables, background=True)
        except Exception as exc:
            return f"error: {exc}"
        return (
            f"workflow '{workflow_id}' started (run_id: {run_id}). Check /runs or run_detail tool."
        )

    async def create_workflow(spec_json: str) -> str:
        """Create a workflow. spec_json: {"name","description","trigger","steps":[{"tool","args","save_as","condition","foreach","retry","on_error","timeout_s"}]}."""
        try:
            data = json.loads(spec_json)
        except json.JSONDecodeError as exc:
            return f"error: bad spec JSON ({exc})"
        try:
            spec = engine.create(data)
        except Exception as exc:
            return f"error: {exc}"
        return (
            f"workflow created: '{spec.name}' (id: {spec.id}) with {len(spec.steps)} steps, "
            f"trigger: {spec.trigger.get('type', 'manual')}"
        )

    async def list_workflows() -> str:
        specs = engine.list()
        if not specs:
            return "no workflows defined — create one with create_workflow"
        lines = [f"Workflows ({len(specs)}):"]
        for s in specs:
            trig = s.trigger.get("type", "manual")
            steps = " -> ".join(st.tool for st in s.steps[:6])
            if len(s.steps) > 6:
                steps += " -> ..."
            lines.append(
                f"  [{s.id}] {s.name} ({trig}, {'on' if s.enabled else 'off'}): {s.description[:60]}"
            )
            lines.append(f"      {steps}")
        return "\n".join(lines)

    async def workflow_runs(workflow_id: str = "", limit: int = 10) -> str:
        runs = engine.run_history(limit=int(limit), workflow_id=workflow_id)
        if not runs:
            return "no runs recorded"
        lines = [f"Workflow runs ({len(runs)}):"]
        for r in runs:
            dur = f"{r['duration_s']:.0f}s" if r["duration_s"] else "-"
            lines.append(
                f"  {r['id']}: {r['workflow_name']} [{r['status']}] {dur} — {(r['result'] or r['error'])[:70]}"
            )
        return "\n".join(lines)

    async def run_detail(run_id: str) -> str:
        d = engine.run_detail(run_id)
        if not d:
            return f"run not found: {run_id}"
        lines = [
            f"Run {d['id']}: {d['workflow_name']} [{d['status']}] ({d.get('duration_s') or 0:.0f}s)",
        ]
        if d.get("error"):
            lines.append(f"  error: {d['error']}")
        for s in d.get("steps", []):
            mark = {"ok": "+", "error": "x", "skipped": "-"}.get(s["status"], "?")
            lines.append(
                f"  [{mark}] {s['tool']} ({s['duration_ms']}ms) {s['output'][:60]}{(s['error'] or '')[:60]}"
            )
        return "\n".join(lines)

    async def delete_workflow(workflow_id: str) -> str:
        if engine.delete(workflow_id):
            return f"workflow {workflow_id} deleted (triggers removed)"
        return f"workflow not found: {workflow_id}"

    async def toggle_workflow(workflow_id: str, enabled: bool = True) -> str:
        if engine.set_enabled(workflow_id, enabled):
            return f"workflow {workflow_id} {'enabled' if enabled else 'disabled'}"
        return f"workflow not found: {workflow_id}"

    box.register(
        Tool(
            name="create_workflow",
            description='Create a reusable multi-step workflow: named steps calling tools with $variables, conditions, foreach loops, retries, and triggers (manual/schedule/cron/event). spec_json example: {"name":"inbox_check","description":"daily check","trigger":{"type":"manual"},"steps":[{"tool":"web_search","args":{"query":"$q"},"save_as":"results"}]}',
            parameters={
                "type": "object",
                "properties": {"spec_json": {"type": "string"}},
                "required": ["spec_json"],
            },
            handler=create_workflow,
        )
    )
    box.register(
        Tool(
            name="run_workflow",
            description="Run a saved workflow by id or name in the background; returns run_id",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "variables_json": {"type": "string", "default": "{}"},
                },
                "required": ["workflow_id"],
            },
            handler=run_workflow,
        )
    )
    box.register(
        Tool(
            name="list_workflows",
            description="List all saved workflows with triggers and step summaries",
            parameters={"type": "object", "properties": {}},
            handler=list_workflows,
        )
    )
    box.register(
        Tool(
            name="workflow_runs",
            description="Show recent workflow run history (optionally filtered by workflow)",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string", "default": ""},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": [],
            },
            handler=workflow_runs,
        )
    )
    box.register(
        Tool(
            name="run_detail",
            description="Show step-by-step detail of one workflow run",
            parameters={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            handler=run_detail,
        )
    )
    box.register(
        Tool(
            name="delete_workflow",
            description="Delete a workflow and its triggers",
            parameters={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
                "required": ["workflow_id"],
            },
            handler=delete_workflow,
        )
    )
    box.register(
        Tool(
            name="toggle_workflow",
            description="Enable or disable a workflow",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "enabled": {"type": "boolean", "default": True},
                },
                "required": ["workflow_id"],
            },
            handler=toggle_workflow,
        )
    )

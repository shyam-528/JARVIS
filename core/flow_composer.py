from __future__ import annotations

import json

from .router import AllProvidersDown
from .workflow import MAX_STEPS, WorkflowSpec

COMPOSER_SYSTEM = """You convert natural-language automation requests into JARVIS workflow JSON.

Available tools (name: description):
__TOOLS__

Workflow JSON schema:
{
  "name": "short_snake_name (max 60 chars)",
  "description": "one line",
  "trigger": {"type": "manual"}  OR  {"type": "schedule", "interval_s": 3600}  OR  {"type": "cron", "cron": "0 9 * * *"}  OR  {"type": "event", "event": "file_created|workflow_finished|tool_result|command"},
  "steps": [
    {
      "tool": "tool_name",
      "args": {"param": "value", "param2": "$variable"},
      "save_as": "optional_var_name",
      "condition": "optional python expr over $vars, e.g. search_out != ''",
      "foreach": "optional var holding a list; item available as $item",
      "retry": 0,
      "on_error": "stop|continue",
      "timeout_s": 120
    }
  ]
}

Rules:
- Output ONLY the JSON object, no markdown fences, no commentary.
- Use at most __MAX_STEPS__ steps.
- $variables: set with "save_as" in an earlier step, reference later as "$name" in STRING arguments only.
- NEVER embed $variables inside "code" arguments for code_exec_python / code_exec_shell — variables are
  not available inside executed code. For code steps use only self-contained Python that prints() its result,
  then capture with "save_as".
- Validate tool names against the list above; use exact parameter names.
- If the user's request maps to a repeating schedule, set the trigger accordingly; otherwise manual.
- For event triggers, the workflow receives variables: $event_type, $event_payload, plus any fields of the
  event payload spread as top-level $variables (e.g. a "workflow" event provides $workflow and $status).
- Prefer few, reliable steps. If a request is impossible with the available tools, still produce the closest reasonable workflow."""


class FlowComposer:
    """LLM-powered natural language -> workflow JSON composer."""

    def __init__(self, orchestrator):
        self.orch = orchestrator

    def _available_tools_text(self) -> str:
        specs = self.orch.toolbox.schemas()
        lines = []
        for s in specs:
            fn = s.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")[:110]
            params = list((fn.get("parameters") or {}).get("properties", {}).keys())
            lines.append(f"{name}: {desc} (params: {', '.join(params[:6])})")
        return "\n".join(lines)

    async def compose(self, description: str) -> dict | None:
        tools_text = self._available_tools_text()
        system = COMPOSER_SYSTEM.replace("__TOOLS__", tools_text).replace(
            "__MAX_STEPS__", str(MAX_STEPS)
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Automation request: {description}"},
        ]
        collected = []
        try:
            async for _name, chunk in self.orch.router.chat(
                messages, tools=None, task_type="coding"
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
        except AllProvidersDown:
            return None
        text = "".join(collected).strip()
        # strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"):
                text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return data

    async def compose_and_create(self, description: str) -> tuple[WorkflowSpec | None, str]:
        data = await self.compose(description)
        if data is None:
            return None, "composer could not produce valid workflow JSON"
        try:
            spec = self.orch.workflow_engine.create(data)
            return spec, "ok"
        except Exception as exc:
            return None, f"validation failed: {exc}"

"""Enhanced tool use: parallel execution, chains, auto-discovery.

Contract:
- execute_parallel() runs calls concurrently and never raises; per-call
  failures come back as ToolResult(success=False, error=...). A call counts
  as failed when the toolbox returns an "error: ..." string (ToolBox.execute
  never raises — see core.tools).
- Chains run steps sequentially, substituting {{var}} from earlier step
  outputs (string args only). A failed step aborts the chain; the failing
  ToolResult is included and chain.failure_count is bumped.
- tool_categories powers auto_discover(); every name listed must be a real
  registered tool (no dead references).
- import_chain validates required keys and raises ValueError on bad input.
- execution_history is capped at max_history entries (oldest dropped).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field

from core.tools import ToolBox


@dataclass
class ToolChain:
    id: str
    name: str
    steps: list[dict]  # [{"tool": "name", "args": {...}, "output_var": "var_name"}]
    description: str
    created: float = field(default_factory=time.time)
    success_count: int = 0
    failure_count: int = 0


@dataclass
class ToolResult:
    tool: str
    args: dict
    result: str
    success: bool
    duration: float
    error: str | None = None


class ToolUseV2:
    """Enhanced tool use: parallel execution, chains, auto-discovery."""

    def __init__(self, toolbox: ToolBox):
        self.toolbox = toolbox
        self.chains: dict[str, ToolChain] = {}
        self.execution_history: list[ToolResult] = []
        self.max_history = 1000

        # Tool categories for auto-discovery
        self.tool_categories = {
            "file": ["read_file", "write_file", "edit_file", "list_dir", "search_code"],
            "shell": ["run_command", "run_tests"],
            "git": ["git_status", "git_diff", "git_log", "git_commit"],
            "web": ["web_search", "browser_extract"],
            "memory": ["remember", "recall", "search_conversations"],
            "knowledge": ["search_knowledge", "index_path"],
            "system": ["system_info", "screenshot", "open_app", "set_volume"],
            "code": ["get_diagnostics", "run_tests", "search_code"],
            "vision": ["analyze_image", "ocr", "read_screen"],
        }

    # ===== Parallel Execution =====

    async def execute_parallel(
        self,
        calls: list[tuple[str, str, dict]],  # [(call_id, tool_name, args)]
    ) -> list[ToolResult]:
        """Execute multiple tool calls in parallel."""

        async def exec_one(call_id: str, tool_name: str, args: dict) -> ToolResult:
            start = time.time()
            try:
                result = await self.toolbox.execute(tool_name, json.dumps(args))
                # ToolBox.execute never raises — it reports failure as an
                # "error: ..." string, so detect that here.
                failed = result.startswith("error:")
                return ToolResult(
                    tool=tool_name,
                    args=args,
                    result="" if failed else result,
                    success=not failed,
                    duration=time.time() - start,
                    error=result if failed else None,
                )
            except Exception as e:
                return ToolResult(
                    tool=tool_name,
                    args=args,
                    result="",
                    success=False,
                    duration=time.time() - start,
                    error=str(e),
                )

        tasks = [exec_one(cid, name, args) for cid, name, args in calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                cid, name, args = calls[i]
                final_results.append(
                    ToolResult(
                        tool=name, args=args, result="", success=False, duration=0, error=str(r)
                    )
                )
            else:
                final_results.append(r)

        self._record_history(final_results)
        return final_results

    # ===== Tool Chains =====

    def create_chain(
        self,
        name: str,
        steps: list[dict],
        description: str = "",
    ) -> str:
        """Create a reusable tool chain.

        Steps format:
        [
            {"tool": "tool_name", "args": {"arg": "value"}, "output_var": "result1"},
            {"tool": "tool2", "args": {"input": "{{result1}}"}, "output_var": "result2"},
        ]
        """
        chain_id = f"chain_{len(self.chains) + 1}_{int(time.time())}"
        chain = ToolChain(
            id=chain_id,
            name=name,
            steps=steps,
            description=description,
        )
        self.chains[chain_id] = chain
        return chain_id

    async def execute_chain(
        self,
        chain_id: str,
        initial_vars: dict = None,
    ) -> list[ToolResult]:
        """Execute a tool chain with variable substitution."""
        chain = self.chains.get(chain_id)
        if not chain:
            raise ValueError(f"Chain {chain_id} not found")

        vars_dict = initial_vars or {}
        results = []

        for step in chain.steps:
            # Substitute variables in args
            args = self._substitute_vars(step.get("args", {}), vars_dict)
            result = await self.toolbox.execute(step["tool"], json.dumps(args))

            tool_result = ToolResult(
                tool=step["tool"],
                args=args,
                result=result,
                success="error" not in result.lower()[:10],
                duration=0,
            )
            results.append(tool_result)

            # Store output for next step
            if "output_var" in step:
                vars_dict[step["output_var"]] = result

            # Check if we should stop on failure
            if not tool_result.success and step.get("stop_on_failure", True):
                break

        chain.success_count += sum(1 for r in results if r.success)
        chain.failure_count += sum(1 for r in results if not r.success)

        self._record_history(results)
        return results

    def _substitute_vars(self, args: dict, vars_dict: dict) -> dict:
        """Substitute {{var}} templates in args."""
        result = {}
        for k, v in args.items():
            if isinstance(v, str):
                for var_name, var_value in vars_dict.items():
                    v = v.replace(f"{{{{{var_name}}}}}", str(var_value))
            result[k] = v
        return result

    # ===== Auto-Discovery =====

    def discover_tools_for_task(self, task_description: str) -> list[str]:
        """Auto-discover relevant tools for a task."""
        task_lower = task_description.lower()
        relevant = []

        # Score tools based on task keywords
        for category, tools in self.tool_categories.items():
            score = 0
            cat_keywords = {
                "file": ["file", "read", "write", "edit", "directory", "folder", "code"],
                "shell": ["run", "execute", "command", "terminal", "bash", "script"],
                "git": ["git", "commit", "diff", "status", "branch", "merge"],
                "web": ["search", "web", "internet", "fetch", "url", "api"],
                "memory": ["remember", "recall", "memory", "fact", "knowledge"],
                "knowledge": ["knowledge", "document", "index", "rag"],
                "system": ["system", "screenshot", "app", "volume", "process"],
                "code": ["test", "debug", "diagnostic", "lint", "error"],
                "vision": ["image", "screen", "vision", "ocr", "camera", "photo"],
            }

            for kw in cat_keywords.get(category, []):
                if kw in task_lower:
                    score += 1

            if score > 0:
                for tool in tools:
                    relevant.append((tool, score))

        # Sort by score, deduplicate
        relevant.sort(key=lambda x: x[1], reverse=True)
        seen = set()
        return [t for t, s in relevant if not (t in seen or seen.add(t))]

    def get_tool_info(self, tool_name: str) -> dict | None:
        """Get detailed info about a tool."""
        for tool in self.toolbox._tools.values():
            if tool.name == tool_name:
                return {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
        return None

    def suggest_tool_combinations(self, task: str) -> list[list[str]]:
        """Suggest tool combinations for a task."""
        relevant = self.discover_tools_for_task(task)

        # Common patterns
        patterns = {
            "read_then_write": ["read_file", "write_file"],
            "search_then_read": ["search_code", "read_file"],
            "git_workflow": ["git_status", "git_diff", "git_commit"],
            "test_fix": ["run_tests", "read_file", "edit_file", "run_tests"],
            "web_research": ["web_search", "fetch_url", "search_knowledge"],
            "debug": ["system_info", "run_command", "read_file"],
        }

        suggestions = []
        for _pattern_name, tools in patterns.items():
            if all(t in relevant for t in tools):
                suggestions.append(tools)

        return suggestions

    # ===== Execution History & Analytics =====

    def _record_history(self, results: list[ToolResult]):
        self.execution_history.extend(results)
        if len(self.execution_history) > self.max_history:
            self.execution_history = self.execution_history[-self.max_history :]

    def get_tool_stats(self) -> dict:
        """Get tool usage statistics."""
        stats = defaultdict(lambda: {"calls": 0, "successes": 0, "failures": 0, "total_time": 0})

        for r in self.execution_history:
            stats[r.tool]["calls"] += 1
            stats[r.tool]["total_time"] += r.duration
            if r.success:
                stats[r.tool]["successes"] += 1
            else:
                stats[r.tool]["failures"] += 1

        # Calculate rates
        for _tool, s in stats.items():
            if s["calls"] > 0:
                s["success_rate"] = s["successes"] / s["calls"]
                s["avg_time"] = s["total_time"] / s["calls"]

        return dict(stats)

    def get_recent_failures(self, limit: int = 10) -> list[ToolResult]:
        return [r for r in reversed(self.execution_history) if not r.success][:limit]

    # ===== Tool Validation =====

    def validate_tool_args(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Validate tool arguments against schema."""
        info = self.get_tool_info(tool_name)
        if not info:
            return False, f"Unknown tool: {tool_name}"

        schema = info.get("parameters", {})
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        # Check required
        for req in required:
            if req not in args:
                return False, f"Missing required argument: {req}"

        # Check types (basic)
        for key, value in args.items():
            if key in properties:
                expected_type = properties[key].get("type")
                if expected_type == "integer" and not isinstance(value, int):
                    try:
                        args[key] = int(value)
                    except (ValueError, TypeError):
                        return False, f"Argument {key} must be integer"
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    try:
                        args[key] = float(value)
                    except (ValueError, TypeError):
                        return False, f"Argument {key} must be number"
                elif expected_type == "boolean" and not isinstance(value, bool):
                    if isinstance(value, str):
                        args[key] = value.lower() in ("true", "1", "yes")
                    else:
                        return False, f"Argument {key} must be boolean"

        return True, "OK"

    # ===== Chain Management =====

    def list_chains(self) -> list[ToolChain]:
        return list(self.chains.values())

    def delete_chain(self, chain_id: str) -> bool:
        if chain_id in self.chains:
            del self.chains[chain_id]
            return True
        return False

    def export_chain(self, chain_id: str) -> dict | None:
        chain = self.chains.get(chain_id)
        if not chain:
            return None
        return {
            "id": chain.id,
            "name": chain.name,
            "steps": chain.steps,
            "description": chain.description,
            "created": chain.created,
            "success_count": chain.success_count,
            "failure_count": chain.failure_count,
        }

    def import_chain(self, data: dict) -> str:
        missing = [k for k in ("id", "name", "steps") if k not in data]
        if missing:
            raise ValueError(f"chain data missing required keys: {', '.join(missing)}")
        if not isinstance(data["steps"], list):
            raise ValueError("chain 'steps' must be a list")
        chain = ToolChain(
            id=data["id"],
            name=data["name"],
            steps=data["steps"],
            description=data.get("description", ""),
            created=data.get("created", time.time()),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
        )
        self.chains[chain.id] = chain
        return chain.id


# Predefined useful chains
DEFAULT_CHAINS = {
    "read_analyze_write": {
        "name": "Read, Analyze, Write",
        "description": "Read a file, analyze it, then write changes",
        "steps": [
            {"tool": "read_file", "args": {"path": "{{file_path}}"}, "output_var": "content"},
            {
                "tool": "search_code",
                "args": {"query": "{{search_query}}", "path": "{{file_path}}"},
                "output_var": "analysis",
            },
            {
                "tool": "write_file",
                "args": {"path": "{{file_path}}", "content": "{{new_content}}"},
                "output_var": "result",
            },
        ],
    },
    "git_commit_workflow": {
        "name": "Git Commit Workflow",
        "description": "Check status, show diff, commit",
        "steps": [
            {"tool": "git_status", "args": {}, "output_var": "status"},
            {"tool": "git_diff", "args": {}, "output_var": "diff"},
            {
                "tool": "git_commit",
                "args": {"message": "{{commit_message}}"},
                "output_var": "result",
            },
        ],
    },
    "test_debug_cycle": {
        "name": "Test Debug Cycle",
        "description": "Run tests, read failing file, fix, re-run",
        "steps": [
            {"tool": "run_tests", "args": {"path": "{{test_path}}"}, "output_var": "test_results"},
            {"tool": "read_file", "args": {"path": "{{failing_file}}"}, "output_var": "code"},
            {
                "tool": "edit_file",
                "args": {
                    "path": "{{failing_file}}",
                    "old_str": "{{old_code}}",
                    "new_str": "{{new_code}}",
                },
                "output_var": "fix",
            },
            {"tool": "run_tests", "args": {"path": "{{test_path}}"}, "output_var": "final_results"},
        ],
    },
}

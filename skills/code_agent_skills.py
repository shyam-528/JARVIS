"""Core code-agent tool surface: file ops, git, tests, diagnostics, tool
chains, multimodal (image/audio/TTS), distributed query, and prompt registry.
Tool handlers report failures as "error: ..." strings (see core.tools).
"""

from __future__ import annotations

import json

from core.code_agent import CodeAgent
from core.tools import Tool, ToolBox


def register_code_agent(box: ToolBox, agent: CodeAgent):
    async def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
        result = agent.read_file(path, start_line, end_line)
        if "error" in result:
            return f"Error: {result['error']}"
        return (
            f"File: {result['path']} ({result['total_lines']} total lines, showing {result['start_line']}-{result['end_line']})\n"
            f"{result['content']}"
        )

    async def write_file(path: str, content: str) -> str:
        result = agent.request_write(path, content)
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("pending"):
            return (
                f"PENDING APPROVAL (change_id: {result['change_id']})\n"
                f"File: {result['path']} ({result['lines']} lines)\n"
                f"Diff:\n{result['diff'][:2000]}\n\n"
                f"User must /approve {result['change_id']} to apply."
            )
        return f"File written: {result['path']} ({result['lines']} lines)"

    async def edit_file(path: str, edits_json: str) -> str:
        try:
            edits = json.loads(edits_json)
        except json.JSONDecodeError as e:
            return f"Error: invalid edits JSON ({e})"
        result = agent.request_edit(path, edits)
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("pending"):
            return (
                f"PENDING APPROVAL (change_id: {result['change_id']})\n"
                f"File: {result['path']} ({result['applied']} edits)\n"
                f"Diff:\n{result['diff'][:2000]}\n\n"
                f"User must /approve {result['change_id']} to apply."
            )
        return f"File edited: {result['path']} ({result['applied']} edits applied)"

    async def list_dir(path: str = ".") -> str:
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return f"Error: {path} not found"
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        dirs = [f"  [DIR] {e.name}/" for e in entries if e.is_dir()]
        files = [f"  {e.name} ({e.stat().st_size}b)" for e in entries if e.is_file()]
        return f"Directory: {p}\n" + "\n".join(dirs + files[:50])

    async def search_code(directory: str, pattern: str, extensions: str = "") -> str:
        ext_list = [e.strip() for e in extensions.split(",")] if extensions else None
        result = agent.editor.search_code(directory, pattern, file_extensions=ext_list)
        if "error" in result:
            return f"Error: {result['error']}"
        matches = result["matches"]
        if not matches:
            return f"No matches for '{pattern}' in {directory}"
        lines = [f"Found {result['total']} matches:"]
        for m in matches[:20]:
            lines.append(f"  {m['file']}:{m['line']}: {m['text']}")
        if result.get("truncated"):
            lines.append("  (truncated)")
        return "\n".join(lines)

    async def run_command(command: str, cwd: str = "") -> str:
        result = agent.request_command(command, cwd)
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("pending"):
            return (
                f"PENDING APPROVAL (change_id: {result['change_id']})\n"
                f"Command: {result['command']}\n"
                f"User must /approve {result['change_id']} to execute."
            )
        # request_command already executed (auto_approve or read-only);
        # reuse its output instead of running the command a second time.
        output = result.get("output") or agent.shell.execute(command, cwd)
        parts = [f"Command: {output['command']}"]
        if output.get("exit_code") is not None:
            parts.append(f"Exit code: {output['exit_code']}")
        if output.get("stdout"):
            parts.append(f"Output:\n{output['stdout'][:2000]}")
        if output.get("stderr"):
            parts.append(f"Errors:\n{output['stderr'][:1000]}")
        return "\n".join(parts)

    async def run_tests(directory: str = ".", test_pattern: str = "pytest") -> str:
        result = agent.shell.execute(f"{test_pattern} -q", cwd=directory)
        output = f"Exit code: {result.get('exit_code', '?')}\n"
        if result.get("stdout"):
            output += f"\n{result['stdout'][:3000]}"
        if result.get("stderr"):
            output += f"\nErrors:\n{result['stderr'][:1500]}"
        return output

    async def git_status(cwd: str = ".") -> str:
        result = agent.git.status(cwd)
        return f"Git Status:\n{result.get('stdout', '(clean)')[:2000]}"

    async def git_diff(cwd: str = ".", staged: bool = False) -> str:
        result = agent.git.diff(cwd, staged=staged)
        return f"Git Diff:\n{result.get('stdout', '(no changes)')[:3000]}"

    async def git_log(cwd: str = ".", count: int = 10) -> str:
        result = agent.git.log(cwd, count)
        return f"Recent Commits:\n{result.get('stdout', '(none)')[:2000]}"

    async def git_commit(cwd: str, message: str) -> str:
        change = agent.queue.submit(
            action="git_commit",
            file_path=cwd,
            description=f"Git commit: {message[:80]}",
            command=f"{cwd}|{message}",
        )
        if agent.queue.auto_approve:
            result = agent._execute_change(change)
            return f"Committed: {result.get('output', {}).get('stdout', '')[:500]}"
        return f"PENDING APPROVAL (change_id: {change.change_id})\nCommit: {message}\nUser must /approve {change.change_id}"

    async def delete_file(path: str) -> str:
        result = agent.request_delete(path)
        if "error" in result:
            return f"Error: {result['error']}"
        if result.get("pending"):
            return f"PENDING APPROVAL (change_id: {result['change_id']})\nDelete: {result['path']}\nUser must /approve {result['change_id']}"
        return f"File deleted: {result['path']}"

    async def get_diagnostics(path: str) -> str:
        result = agent.read_file(path)
        if "error" in result:
            return f"Error: {result['error']}"
        ext = result.get("extension", "")
        if ext == ".py":
            check = agent.shell.execute(
                f"python -c \"import ast; ast.parse(open(r'{path}', encoding='utf-8').read()); print('OK')\""
            )
            if "OK" in check.get("stdout", ""):
                return "Python syntax: OK"
            return f"Python syntax error:\n{check.get('stderr', '')[:1000]}"
        return f"Diagnostics not available for .{ext} files (only .py supported)"

    box.register(
        Tool(
            name="read_file",
            description="Read a file with line numbers",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "default": 1},
                    "end_line": {"type": "integer", "default": 0},
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
    box.register(
        Tool(
            name="write_file",
            description=(
                "Create or overwrite a file. Shows diff for approval. "
                "File placement: bare filenames are routed automatically (plans to docs/plans/, "
                "research to data/research/, logs to logs/, audio/images/video to D:/jarvis/media/, "
                "helper scripts to workspace/scripts/, else workspace/) — never the repo root. "
                "Repo source edits use their real repo-relative paths."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
            handler=write_file,
        )
    )
    box.register(
        Tool(
            name="edit_file",
            description='Edit specific lines in a file. edits_json: [{"line": 5, "old": "old text", "new": "new text"}]',
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "edits_json": {"type": "string"}},
                "required": ["path", "edits_json"],
            },
            handler=edit_file,
        )
    )
    box.register(
        Tool(
            name="list_dir",
            description="List directory contents",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
            handler=list_dir,
        )
    )
    box.register(
        Tool(
            name="search_code",
            description="Search for a regex pattern across files",
            parameters={
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "pattern": {"type": "string"},
                    "extensions": {"type": "string", "default": ""},
                },
                "required": ["directory", "pattern"],
            },
            handler=search_code,
        )
    )
    box.register(
        Tool(
            name="run_command",
            description="Execute a shell command. Writes/mutations need /approve; read-only queries (schtasks /query, git status/diff/log, gh view/list, tasklist) run immediately with no approval. For Jarvis reminders/scheduled tasks prefer the list_tasks tool.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "default": ""},
                },
                "required": ["command"],
            },
            handler=run_command,
        )
    )
    box.register(
        Tool(
            name="run_tests",
            description="Run test suite (pytest) in a directory",
            parameters={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "default": "."},
                    "test_pattern": {"type": "string", "default": "pytest"},
                },
                "required": [],
            },
            handler=run_tests,
        )
    )
    box.register(
        Tool(
            name="git_status",
            description="Show git working tree status",
            parameters={
                "type": "object",
                "properties": {"cwd": {"type": "string", "default": "."}},
                "required": [],
            },
            handler=git_status,
        )
    )
    box.register(
        Tool(
            name="git_diff",
            description="Show git diff of changes",
            parameters={
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "default": "."},
                    "staged": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            handler=git_diff,
        )
    )
    box.register(
        Tool(
            name="git_log",
            description="Show recent git commits",
            parameters={
                "type": "object",
                "properties": {
                    "cwd": {"type": "string", "default": "."},
                    "count": {"type": "integer", "default": 10},
                },
                "required": [],
            },
            handler=git_log,
        )
    )
    box.register(
        Tool(
            name="git_commit",
            description="Stage and commit changes",
            parameters={
                "type": "object",
                "properties": {"cwd": {"type": "string"}, "message": {"type": "string"}},
                "required": ["cwd", "message"],
            },
            handler=git_commit,
        )
    )
    box.register(
        Tool(
            name="delete_file",
            description="Delete a file (requires approval)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=delete_file,
        )
    )
    box.register(
        Tool(
            name="get_diagnostics",
            description="Check for syntax errors in a Python file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=get_diagnostics,
        )
    )

    # Tool Chain tools
    async def execute_tool_chain(chain_name: str, variables_json: str = "{}") -> str:
        """Execute a predefined tool chain by name with variable substitution."""
        try:
            variables = json.loads(variables_json) if variables_json else {}
        except json.JSONDecodeError as e:
            return f"Error: invalid variables JSON ({e})"

        orch = agent.orchestrator

        if orch and hasattr(orch, "tool_use"):
            chains = orch.tool_use.chains
            matching = [c for c in chains.values() if c.name == chain_name]
            if not matching:
                available = [c.name for c in chains.values()]
                return f"Chain '{chain_name}' not found. Available: {available}"

            chain = matching[0]
            try:
                results = await orch.tool_use.execute_chain(chain.id, variables)
                output = []
                for r in results:
                    status = "OK" if r.success else "FAIL"
                    output.append(f"  [{status}] {r.tool}: {r.result[:200]}")
                return f"Chain '{chain_name}' executed:\n" + "\n".join(output)
            except Exception as e:
                return f"Error executing chain: {e}"
        else:
            return "Tool chains not available"

    async def create_tool_chain(name: str, steps_json: str, description: str = "") -> str:
        """Create a new reusable tool chain.

        steps_json example:
        [
            {"tool": "read_file", "args": {"path": "{{file_path}}"}, "output_var": "content"},
            {"tool": "write_file", "args": {"path": "{{output_path}}", "content": "{{new_content}}"}, "output_var": "result"}
        ]
        """
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as e:
            return f"Error: invalid steps JSON ({e})"

        orch = agent.orchestrator

        if orch and hasattr(orch, "tool_use"):
            chain_id = orch.tool_use.create_chain(name, steps, description)
            return f"Tool chain '{name}' created with ID: {chain_id}"
        else:
            return "Tool chains not available"

    async def list_tool_chains() -> str:
        """List all available tool chains."""
        orch = agent.orchestrator

        if orch and hasattr(orch, "tool_use"):
            chains = orch.tool_use.list_chains()
            if not chains:
                return "No tool chains defined"
            lines = ["Available tool chains:"]
            for c in chains:
                lines.append(f"  {c.name} (ID: {c.id}): {c.description}")
                lines.append(
                    f"  Steps: {len(c.steps)}, Success: {c.success_count}, Failures: {c.failure_count}"
                )
            return "\n".join(lines)
        else:
            return "Tool chains not available"

    async def discover_tools(task_description: str) -> str:
        """Auto-discover relevant tools for a task."""
        orch = agent.orchestrator

        if orch and hasattr(orch, "tool_use"):
            tools = orch.tool_use.discover_tools_for_task(task_description)
            if not tools:
                return "No relevant tools found"

            suggestions = orch.tool_use.suggest_tool_combinations(task_description)
            lines = [f"Relevant tools for '{task_description}':", ""]
            for t in tools[:15]:
                info = orch.tool_use.get_tool_info(t)
                desc = info.get("description", "No description") if info else "No description"
                lines.append(f"  {t}: {desc[:80]}")

            if suggestions:
                lines.append("\nSuggested combinations:")
                for s in suggestions:
                    lines.append(f"  {s}")
            return "\n".join(lines)
        else:
            return "Tool discovery not available"

    async def get_tool_stats() -> str:
        """Get tool usage statistics."""
        orch = agent.orchestrator

        if orch and hasattr(orch, "tool_use"):
            stats = orch.tool_use.get_tool_stats()
            if not stats:
                return "No tool usage data yet"

            lines = ["Tool Usage Statistics:"]
            for tool, s in sorted(stats.items(), key=lambda x: x[1]["calls"], reverse=True):
                lines.append(
                    f"  {tool}: calls={s['calls']}, success_rate={s.get('success_rate', 0):.1%}, "
                    f"avg_time={s.get('avg_time', 0):.3f}s"
                )

            failures = orch.tool_use.get_recent_failures(5)
            if failures:
                lines.append("\nRecent failures:")
                for f in failures:
                    lines.append(f"  {f.tool}: {f.error[:100]}")
            return "\n".join(lines)
        else:
            return "Tool stats not available"

    box.register(
        Tool(
            name="execute_tool_chain",
            description="Execute a predefined tool chain by name with variables",
            parameters={
                "type": "object",
                "properties": {
                    "chain_name": {"type": "string"},
                    "variables_json": {"type": "string", "default": "{}"},
                },
                "required": ["chain_name"],
            },
            handler=execute_tool_chain,
        )
    )

    box.register(
        Tool(
            name="create_tool_chain",
            description="Create a reusable tool chain from steps JSON",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "steps_json": {"type": "string"},
                    "description": {"type": "string", "default": ""},
                },
                "required": ["name", "steps_json"],
            },
            handler=create_tool_chain,
        )
    )

    box.register(
        Tool(
            name="list_tool_chains",
            description="List all available tool chains",
            parameters={"type": "object", "properties": {}},
            handler=list_tool_chains,
        )
    )

    box.register(
        Tool(
            name="discover_tools",
            description="Auto-discover relevant tools for a task",
            parameters={
                "type": "object",
                "properties": {"task_description": {"type": "string"}},
                "required": ["task_description"],
            },
            handler=discover_tools,
        )
    )

    box.register(
        Tool(
            name="get_tool_stats",
            description="Get tool usage statistics",
            parameters={"type": "object", "properties": {}},
            handler=get_tool_stats,
        )
    )

    # Multi-Modal tools
    async def analyze_image(
        image_b64: str = "", image_path: str = "", prompt: str = "Describe this image in detail"
    ) -> str:
        """Analyze an image using vision models."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "multimodal"):
            from core.multimodal import MultiModalInput

            inp = MultiModalInput(image_b64=image_b64, image_path=image_path)
            result = await orch.multimodal.analyze_image(inp, prompt)
            return f"[{result.provider}] {result.analysis}"
        return "Multi-modal not available"

    async def transcribe_audio(
        audio_b64: str = "", audio_path: str = "", language: str = "en"
    ) -> str:
        """Transcribe audio to text using STT."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "multimodal"):
            from core.multimodal import MultiModalInput

            inp = MultiModalInput(audio_b64=audio_b64, audio_path=audio_path)
            result = await orch.multimodal.transcribe_audio(inp, language)
            return f"[{result.provider}] {result.transcript} (confidence: {result.confidence:.1%})"
        return "Multi-modal not available"

    async def reason_multimodal(
        text: str = "",
        image_b64: str = "",
        image_path: str = "",
        audio_b64: str = "",
        audio_path: str = "",
        task: str = "analyze",
    ) -> str:
        """Unified reasoning across text, vision, and audio."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "multimodal"):
            from core.multimodal import MultiModalInput

            inp = MultiModalInput(
                text=text,
                image_b64=image_b64,
                image_path=image_path,
                audio_b64=audio_b64,
                audio_path=audio_path,
            )
            result = await orch.multimodal.reason_multimodal(inp, task)
            return f"Response: {result['response']}\nModalities: {', '.join(result['modalities_used'])}"
        return "Multi-modal not available"

    async def text_to_speech(
        text: str, voice: str = "en-GB-RyanNeural", language: str = "en"
    ) -> str:
        """Convert text to speech."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "multimodal"):
            audio = await orch.multimodal.text_to_speech(text, voice, language)
            if audio:
                import base64

                return f"Audio generated ({len(audio)} bytes): {base64.b64encode(audio).decode()[:100]}..."
            return "TTS failed"
        return "Multi-modal not available"

    box.register(
        Tool(
            name="analyze_image",
            description="Analyze an image using vision models",
            parameters={
                "type": "object",
                "properties": {
                    "image_b64": {"type": "string", "default": ""},
                    "image_path": {"type": "string", "default": ""},
                    "prompt": {"type": "string", "default": "Describe this image in detail"},
                },
            },
            handler=analyze_image,
        )
    )

    box.register(
        Tool(
            name="transcribe_audio",
            description="Transcribe audio to text using STT",
            parameters={
                "type": "object",
                "properties": {
                    "audio_b64": {"type": "string", "default": ""},
                    "audio_path": {"type": "string", "default": ""},
                    "language": {"type": "string", "default": "en"},
                },
            },
            handler=transcribe_audio,
        )
    )

    box.register(
        Tool(
            name="reason_multimodal",
            description="Unified reasoning across text, vision, and audio",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "default": ""},
                    "image_b64": {"type": "string", "default": ""},
                    "image_path": {"type": "string", "default": ""},
                    "audio_b64": {"type": "string", "default": ""},
                    "audio_path": {"type": "string", "default": ""},
                    "task": {"type": "string", "default": "analyze"},
                },
            },
            handler=reason_multimodal,
        )
    )

    box.register(
        Tool(
            name="text_to_speech",
            description="Convert text to speech",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {"type": "string", "default": "en-GB-RyanNeural"},
                    "language": {"type": "string", "default": "en"},
                },
                "required": ["text"],
            },
            handler=text_to_speech,
        )
    )

    # Distributed Brain tools
    async def federated_query(
        query: str, task_type: str = "general", prefer_local: bool = True
    ) -> str:
        """Query the distributed brain (P2P model sharing via A2A mesh)."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "distributed"):
            return await orch.distributed.federated_query(query, task_type, prefer_local)
        return "Distributed brain not available"

    async def list_remote_models() -> str:
        """List all remote models available via A2A mesh."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "distributed"):
            models = orch.distributed.list_remote_models()
            if not models:
                return "No remote models discovered"
            lines = ["Remote models via A2A mesh:"]
            for m in models:
                status = "✓" if m["healthy"] else "✗"
                lines.append(f"  {status} {m['name']} (agent: {m['agent_id']})")
                lines.append(f"    Models: {', '.join(m['models'])}")
                lines.append(f"    Capabilities: {', '.join(m['capabilities'])}")
                lines.append(f"    Latency: {m['latency_ms']:.0f}ms, Budget: {m['budget_left']:,}")
            return "\n".join(lines)
        return "Distributed brain not available"

    async def get_distributed_stats() -> str:
        """Get distributed brain statistics."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "distributed"):
            stats = orch.distributed.get_stats()
            return (
                f"Distributed Brain Stats:\n"
                f"  Local models: {stats['local_models']}\n"
                f"  Remote providers: {stats['remote_providers']}\n"
                f"  Healthy remotes: {stats['healthy_remotes']}\n"
                f"  Pending requests: {stats['pending_requests']}\n"
                f"  Completed: {stats['completed_requests']}"
            )
        return "Distributed brain not available"

    box.register(
        Tool(
            name="federated_query",
            description="Query the distributed brain (P2P model sharing via A2A mesh)",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "task_type": {"type": "string", "default": "general"},
                    "prefer_local": {"type": "boolean", "default": True},
                },
                "required": ["query"],
            },
            handler=federated_query,
        )
    )

    box.register(
        Tool(
            name="list_remote_models",
            description="List all remote models available via A2A mesh",
            parameters={"type": "object", "properties": {}},
            handler=list_remote_models,
        )
    )

    box.register(
        Tool(
            name="get_distributed_stats",
            description="Get distributed brain statistics",
            parameters={"type": "object", "properties": {}},
            handler=get_distributed_stats,
        )
    )

    # Prompt Optimization tools
    async def register_prompt(name: str, template: str, variables_json: str = "[]") -> str:
        """Register a new prompt template for optimization."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "prompt_opt"):
            try:
                variables = json.loads(variables_json)
            except json.JSONDecodeError:
                variables = []
            pid = orch.prompt_opt.register_prompt(name, template, variables)
            return f"Prompt '{name}' registered with ID: {pid}"
        return "Prompt optimizer not available"

    async def list_prompts() -> str:
        """List all registered prompt templates."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "prompt_opt"):
            prompts = orch.prompt_opt.list_prompts()
            if not prompts:
                return "No prompts registered"
            lines = ["Registered Prompts:"]
            for p in prompts:
                lines.append(f"  {p.name} (ID: {p.id}, v{p.version})")
                lines.append(f"    Variables: {', '.join(p.variables)}")
                lines.append(f"    Template: {p.template[:80]}...")
            return "\n".join(lines)
        return "Prompt optimizer not available"

    async def optimize_prompt(
        prompt_id: str, test_cases_json: str, metric: str = "exact_match", iterations: int = 3
    ) -> str:
        """Optimize a prompt using evolutionary search.

        test_cases_json format:
        [{"input": "question", "expected": "expected answer"}]

        metric: exact_match, contains, length
        """
        orch = agent.orchestrator
        if not (orch and hasattr(orch, "prompt_opt")):
            return "Prompt optimizer not available"

        prompt = orch.prompt_opt.get_prompt(prompt_id)
        if not prompt:
            return f"Prompt {prompt_id} not found"

        try:
            test_cases = json.loads(test_cases_json)
        except json.JSONDecodeError as e:
            return f"Invalid test cases JSON: {e}"

        # Define metric function
        def metric_fn(output: str, expected: str) -> float:
            if metric == "exact_match":
                return 1.0 if output.strip() == expected.strip() else 0.0
            elif metric == "contains":
                return 1.0 if expected.lower() in output.lower() else 0.0
            elif metric == "length":
                return min(len(output) / max(len(expected), 1), 1.0)
            return 0.0

        try:
            result = await orch.prompt_opt.optimize_prompt(
                prompt.name, prompt.template, test_cases, metric_fn, iterations
            )
            return (
                f"Optimization complete:\n"
                f"  Baseline: {result.baseline_score:.2f}\n"
                f"  Optimized: {result.optimized_score:.2f}\n"
                f"  Improvement: {result.improvement:+.2f}\n"
                f"  Iterations: {result.iterations}\n"
                f"  Best prompt length: {len(result.best_prompt)} chars"
            )
        except Exception as e:
            return f"Optimization error: {e}"

    async def add_example(input_text: str, output_text: str, tags_json: str = "[]") -> str:
        """Add a few-shot example for prompt optimization."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "prompt_opt"):
            try:
                tags = json.loads(tags_json)
            except json.JSONDecodeError:
                tags = []
            ex_id = orch.prompt_opt.add_example(input_text, output_text, tags)
            return f"Example added with ID: {ex_id}"
        return "Prompt optimizer not available"

    async def get_examples(tags_json: str = "[]", limit: int = 10) -> str:
        """Get few-shot examples for a task."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "prompt_opt"):
            try:
                tags = json.loads(tags_json)
            except json.JSONDecodeError:
                tags = []
            examples = orch.prompt_opt.get_examples(tags, limit)
            if not examples:
                return "No examples found"
            lines = ["Examples:"]
            for e in examples:
                lines.append(
                    f"  [{e.id}] {e.input[:50]}... -> {e.output[:50]}... (score: {e.quality_score:.1f})"
                )
            return "\n".join(lines)
        return "Prompt optimizer not available"

    async def ab_test_prompts(
        prompt_a_id: str, prompt_b_id: str, test_cases_json: str, metric: str = "exact_match"
    ) -> str:
        """A/B test two prompts."""
        orch = agent.orchestrator
        if not (orch and hasattr(orch, "prompt_opt")):
            return "Prompt optimizer not available"

        prompt_a = orch.prompt_opt.get_prompt(prompt_a_id)
        prompt_b = orch.prompt_opt.get_prompt(prompt_b_id)
        if not prompt_a or not prompt_b:
            return "One or both prompts not found"

        try:
            test_cases = json.loads(test_cases_json)
        except json.JSONDecodeError as e:
            return f"Invalid test cases JSON: {e}"

        def metric_fn(output: str, expected: str) -> float:
            if metric == "exact_match":
                return 1.0 if output.strip() == expected.strip() else 0.0
            elif metric == "contains":
                return 1.0 if expected.lower() in output.lower() else 0.0
            return 0.0

        result = await orch.prompt_opt.ab_test(
            prompt_a.template, prompt_b.template, test_cases, metric_fn
        )

        winner = "A" if result["winner"] == "A" else "B"
        return (
            f"A/B Test Results:\n"
            f"  Prompt A ({prompt_a.name}): {result['prompt_a_score']:.2f}\n"
            f"  Prompt B ({prompt_b.name}): {result['prompt_b_score']:.2f}\n"
            f"  Winner: Prompt {winner}\n"
            f"  Difference: {result['difference']:.2f}"
        )

    async def bootstrap_examples(task_description: str, count: int = 10) -> str:
        """Generate bootstrapped examples for a task."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "prompt_opt"):
            examples = await orch.prompt_opt.bootstrap_examples(task_description, count)
            return f"Generated {len(examples)} examples for '{task_description}'"
        return "Prompt optimizer not available"

    async def get_prompt_stats() -> str:
        """Get prompt optimizer statistics."""
        orch = agent.orchestrator
        if orch and hasattr(orch, "prompt_opt"):
            stats = orch.prompt_opt.get_stats()
            return (
                f"Prompt Optimizer Stats:\n"
                f"  Prompts: {stats['total_prompts']}\n"
                f"  Examples: {stats['total_examples']}\n"
                f"  Optimizations run: {stats['optimizations_run']}\n"
                f"  Avg improvement: {stats['avg_improvement']:.2f}"
            )
        return "Prompt optimizer not available"

    box.register(
        Tool(
            name="register_prompt",
            description="Register a new prompt template for optimization",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "template": {"type": "string"},
                    "variables_json": {"type": "string", "default": "[]"},
                },
                "required": ["name", "template"],
            },
            handler=register_prompt,
        )
    )

    box.register(
        Tool(
            name="list_prompts",
            description="List all registered prompt templates",
            parameters={"type": "object", "properties": {}},
            handler=list_prompts,
        )
    )

    box.register(
        Tool(
            name="optimize_prompt",
            description="Optimize a prompt using evolutionary search",
            parameters={
                "type": "object",
                "properties": {
                    "prompt_id": {"type": "string"},
                    "test_cases_json": {"type": "string"},
                    "metric": {
                        "type": "string",
                        "default": "exact_match",
                        "enum": ["exact_match", "contains", "length"],
                    },
                    "iterations": {"type": "integer", "default": 3},
                },
                "required": ["prompt_id", "test_cases_json"],
            },
            handler=optimize_prompt,
        )
    )

    box.register(
        Tool(
            name="add_example",
            description="Add a few-shot example for prompt optimization",
            parameters={
                "type": "object",
                "properties": {
                    "input_text": {"type": "string"},
                    "output_text": {"type": "string"},
                    "tags_json": {"type": "string", "default": "[]"},
                },
                "required": ["input_text", "output_text"],
            },
            handler=add_example,
        )
    )

    box.register(
        Tool(
            name="get_examples",
            description="Get few-shot examples for a task",
            parameters={
                "type": "object",
                "properties": {
                    "tags_json": {"type": "string", "default": "[]"},
                    "limit": {"type": "integer", "default": 10},
                },
            },
            handler=get_examples,
        )
    )

    box.register(
        Tool(
            name="ab_test_prompts",
            description="A/B test two prompts",
            parameters={
                "type": "object",
                "properties": {
                    "prompt_a_id": {"type": "string"},
                    "prompt_b_id": {"type": "string"},
                    "test_cases_json": {"type": "string"},
                    "metric": {
                        "type": "string",
                        "default": "exact_match",
                        "enum": ["exact_match", "contains", "length"],
                    },
                },
                "required": ["prompt_a_id", "prompt_b_id", "test_cases_json"],
            },
            handler=ab_test_prompts,
        )
    )

    box.register(
        Tool(
            name="bootstrap_examples",
            description="Generate bootstrapped examples for a task",
            parameters={
                "type": "object",
                "properties": {
                    "task_description": {"type": "string"},
                    "count": {"type": "integer", "default": 10},
                },
                "required": ["task_description"],
            },
            handler=bootstrap_examples,
        )
    )

    box.register(
        Tool(
            name="get_prompt_stats",
            description="Get prompt optimizer statistics",
            parameters={"type": "object", "properties": {}},
            handler=get_prompt_stats,
        )
    )

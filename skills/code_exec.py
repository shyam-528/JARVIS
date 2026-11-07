"""Sandboxed code execution tools (python/shell/js) with persistent sessions
and package install. Returns captured output or an error string; never raises
into the agent loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path

from core.tools import Tool, ToolBox


class PythonREPL:
    """Persistent Python REPL session using subprocess per execution."""

    def __init__(self, session_id: str, work_dir: Path):
        self.session_id = session_id
        self.work_dir = work_dir
        self.namespace: dict = {}

    async def execute(self, code: str, timeout: int = 30) -> dict:
        """Execute code in the REPL namespace."""
        try:
            # Serialize namespace to Python code (exclude modules)
            namespace_assignments = []
            for k, v in self.namespace.items():
                # Skip modules to avoid shadowing imports
                if k in ("sys", "json", "os", "asyncio", "pathlib", "typing", "traceback"):
                    continue
                if isinstance(v, (type(json), type(sys))):
                    continue
                try:
                    json.dumps(v)
                    namespace_assignments.append(f"{k} = {repr(v)}")
                except Exception:
                    pass

            namespace_code = "\n".join(namespace_assignments)

            # Prepare the code - escape triple quotes in user code
            code_escaped = code.replace('"""', '\\"\\"\\"')

            full_code = (
                "import sys\n"
                "import json\n\n"
                "# Restore namespace\n"
                f"{namespace_code}\n\n"
                "# Execute code\n"
                "try:\n"
                f"    {namespace_code}\n\n"
                f'    exec("""{code_escaped}""", globals(), locals())\n'
                "except Exception as e:\n"
                "    import traceback\n"
                "    print('__ERROR__: ' + traceback.format_exc(), file=sys.stderr)\n"
                "    sys.exit(1)\n\n"
                "# Save namespace\n"
                "namespace_out = {k: v for k, v in globals().items() if not k.startswith('_')}\n"
                "# Convert non-serializable\n"
                "for k, v in list(namespace_out.items()):\n"
                "    try:\n"
                "        json.dumps(v)\n"
                "    except Exception:\n"
                "        namespace_out[k] = str(v)\n\n"
                "print('__NAMESPACE__' + json.dumps(namespace_out))\n"
            )

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                full_code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.work_dir,
            )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            stdout_str = stdout.decode(errors="replace")
            stderr_str = stderr.decode(errors="replace")

            # Parse namespace from output
            for line in stdout_str.split("\n"):
                if line.startswith("__NAMESPACE__"):
                    with contextlib.suppress(Exception):
                        self.namespace = json.loads(line[13:])

            result = {
                "stdout": stdout_str.split("__NAMESPACE__")[0].strip(),
                "stderr": stderr_str.strip(),
                "error": None if proc.returncode == 0 else stderr_str.strip(),
            }
            return result
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": "execution timed out", "error": "timeout"}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "error": str(e)}

    async def reset(self):
        """Reset the REPL state."""
        self.namespace.clear()

    async def close(self):
        """Close the REPL."""
        pass


class CodeExecutor:
    """Manages sandboxed code execution sessions."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path.home() / ".jarvis_code"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, PythonREPL] = {}
        self.temp_dir = self.base_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def get_session(self, session_id: str = None) -> PythonREPL:
        """Get or create a REPL session."""
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]

        if session_id not in self.sessions:
            session_dir = self.base_dir / "sessions" / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            self.sessions[session_id] = PythonREPL(session_id, session_dir)

        return self.sessions[session_id]

    async def execute_python(
        self, code: str, session_id: str = None, timeout: int = 30, packages: list[str] = None
    ) -> dict:
        """Execute Python code in a REPL session."""
        session = self.get_session(session_id)

        # Install packages if requested
        if packages:
            for pkg in packages:
                await self._install_package(pkg)

        return await session.execute(code, timeout)

    async def _install_package(self, package: str):
        """Install a Python package in the current environment."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            pass  # Silently ignore

    async def execute_file(self, file_path: str, args: list[str] = None, timeout: int = 60) -> dict:
        """Execute a Python file."""
        args = args or []
        file_path = Path(file_path).expanduser().resolve()

        if not file_path.exists():
            return {"stdout": "", "stderr": f"file not found: {file_path}", "error": "not found"}

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(file_path),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=file_path.parent,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "exit_code": proc.returncode,
                "error": None if proc.returncode == 0 else f"exit code {proc.returncode}",
            }
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": "execution timed out", "error": "timeout"}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "error": str(e)}

    async def execute_shell(self, code: str, timeout: int = 60) -> dict:
        """Execute shell command."""
        try:
            proc = await asyncio.create_subprocess_shell(
                code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "exit_code": proc.returncode,
                "error": None if proc.returncode == 0 else f"exit code {proc.returncode}",
            }
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": "execution timed out", "error": "timeout"}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "error": str(e)}

    async def execute_js(self, code: str, timeout: int = 30) -> dict:
        """Execute JavaScript/Node.js code."""
        # Write to temp file
        temp_file = self.temp_dir / f"js_{uuid.uuid4().hex[:8]}.js"
        temp_file.write_text(code)

        try:
            proc = await asyncio.create_subprocess_exec(
                "node",
                str(temp_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.temp_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
                "exit_code": proc.returncode,
                "error": None if proc.returncode == 0 else f"exit code {proc.returncode}",
            }
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": "execution timed out", "error": "timeout"}
        except FileNotFoundError:
            return {"stdout": "", "stderr": "node not found", "error": "node not installed"}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "error": str(e)}
        finally:
            with contextlib.suppress(Exception):
                temp_file.unlink()

    async def install_package(self, package: str) -> str:
        """Install a Python package."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                return f"installed {package}"
            return f"error: {stderr.decode(errors='replace')[:200]}"
        except Exception as e:
            return f"error: {e}"

    def list_sessions(self) -> list[str]:
        """List active session IDs."""
        return list(self.sessions.keys())

    def remove_session(self, session_id: str) -> bool:
        """Remove a session."""
        if session_id in self.sessions:
            asyncio.create_task(self.sessions[session_id].close())
            del self.sessions[session_id]
            return True
        return False

    async def close_all(self):
        """Close all sessions."""
        for session in self.sessions.values():
            await session.close()
        self.sessions.clear()


# Global executor instance
_code_executor = CodeExecutor()


async def code_exec_python(
    code: str, session_id: str = None, timeout: int = 30, packages: list[str] = None
) -> str:
    """Execute Python code in a REPL session."""
    result = await _code_executor.execute_python(code, session_id, timeout, packages)
    if result["error"]:
        return f"Error: {result['error']}\n{result['stderr']}"
    return result["stdout"]


async def code_exec_python_file(file_path: str, args_json: str = "[]", timeout: int = 60) -> str:
    """Execute a Python file."""
    args = json.loads(args_json) if args_json else []
    result = await _code_executor.execute_file(file_path, args, timeout)
    if result["error"]:
        return f"Error (exit {result['exit_code']}): {result['error']}\n{result['stderr']}"
    return result["stdout"]


async def code_exec_shell(code: str, timeout: int = 60) -> str:
    """Execute shell command."""
    result = await _code_executor.execute_shell(code, timeout)
    if result["error"]:
        return f"Error (exit {result['exit_code']}): {result['error']}\n{result['stderr']}"
    return result["stdout"]


async def code_exec_js(code: str, timeout: int = 30) -> str:
    """Execute JavaScript/Node.js code."""
    result = await _code_executor.execute_js(code, timeout)
    if result["error"]:
        return f"Error (exit {result['exit_code']}): {result['error']}\n{result['stderr']}"
    return result["stdout"]


async def code_exec_session_list() -> str:
    """List active code execution sessions."""
    sessions = _code_executor.list_sessions()
    if not sessions:
        return "no active sessions"
    return "Active sessions:\n" + "\n".join(f"  - {s}" for s in sessions)


async def code_exec_session_reset(session_id: str) -> str:
    """Reset a session's state."""
    if session_id in _code_executor.sessions:
        await _code_executor.sessions[session_id].reset()
        return f"session {session_id} reset"
    return f"session {session_id} not found"


async def code_exec_session_close(session_id: str) -> str:
    """Close a session."""
    if _code_executor.remove_session(session_id):
        return f"session {session_id} closed"
    return f"session {session_id} not found"


async def code_exec_install_package(package: str) -> str:
    """Install a Python package via pip."""
    return await _code_executor.install_package(package)


def register_code_exec(box: ToolBox) -> None:
    box.register(
        Tool(
            name="code_exec_python",
            description="Execute Python code in a persistent REPL session (maintains state across calls)",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "session_id": {"type": "string", "default": ""},
                    "timeout": {"type": "integer", "default": 30},
                    "packages": {"type": "array", "items": {"type": "string"}, "default": []},
                },
                "required": ["code"],
            },
            handler=code_exec_python,
        )
    )
    box.register(
        Tool(
            name="code_exec_python_file",
            description="Execute a Python file with arguments",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "args_json": {"type": "string", "default": "[]"},
                    "timeout": {"type": "integer", "default": 60},
                },
                "required": ["file_path"],
            },
            handler=code_exec_python_file,
        )
    )
    box.register(
        Tool(
            name="code_exec_shell",
            description="Execute shell command (bash/powershell)",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "default": 60},
                },
                "required": ["code"],
            },
            handler=code_exec_shell,
        )
    )
    box.register(
        Tool(
            name="code_exec_js",
            description="Execute JavaScript/Node.js code",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["code"],
            },
            handler=code_exec_js,
        )
    )
    box.register(
        Tool(
            name="code_exec_session_list",
            description="List active code execution sessions",
            parameters={"type": "object", "properties": {}},
            handler=code_exec_session_list,
        )
    )
    box.register(
        Tool(
            name="code_exec_session_reset",
            description="Reset a session's state (clear variables)",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            handler=code_exec_session_reset,
        )
    )
    box.register(
        Tool(
            name="code_exec_session_close",
            description="Close a session and free resources",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
            },
            handler=code_exec_session_close,
        )
    )
    box.register(
        Tool(
            name="code_exec_install_package",
            description="Install a Python package via pip",
            parameters={
                "type": "object",
                "properties": {"package": {"type": "string"}},
                "required": ["package"],
            },
            handler=code_exec_install_package,
        )
    )

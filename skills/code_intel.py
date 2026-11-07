"""Code intelligence tools: find symbol/references, lint, format, typecheck,
rename refactor. Fail-soft when language tooling is missing.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from core.tools import Tool, ToolBox

# Availability probe only: the LSP client below is config-driven and never
# touches pygls names directly, so a single package probe keeps the gate
# without 40 unused imports.
try:
    import pygls  # noqa: F401

    PYGLS_AVAILABLE = True
except ImportError:
    PYGLS_AVAILABLE = False


class LSPManager:
    """Manages Language Server Protocol servers for different languages."""

    def __init__(self):
        self.servers = {}
        self.documents = {}  # uri -> document content
        self.diagnostics = {}  # uri -> list of diagnostics

    def get_server_config(self, language: str) -> dict | None:
        """Get LSP server configuration for a language."""
        configs = {
            "python": {
                "command": ["pylsp"],
                "root_patterns": [
                    "pyproject.toml",
                    "setup.py",
                    "setup.cfg",
                    "requirements.txt",
                    ".git",
                ],
            },
            "typescript": {
                "command": ["typescript-language-server", "--stdio"],
                "root_patterns": ["package.json", "tsconfig.json", ".git"],
            },
            "javascript": {
                "command": ["typescript-language-server", "--stdio"],
                "root_patterns": ["package.json", ".git"],
            },
            "rust": {
                "command": ["rust-analyzer"],
                "root_patterns": ["Cargo.toml", ".git"],
            },
            "go": {
                "command": ["gopls"],
                "root_patterns": ["go.mod", ".git"],
            },
            "cpp": {
                "command": ["clangd"],
                "root_patterns": ["compile_commands.json", ".git"],
            },
            "c": {
                "command": ["clangd"],
                "root_patterns": ["compile_commands.json", ".git"],
            },
        }
        return configs.get(language.lower())

    def find_project_root(self, file_path: Path, patterns: list) -> Path:
        """Find project root by looking for marker files."""
        current = file_path.parent
        while current != current.parent:
            for pattern in patterns:
                if list(current.glob(pattern)):
                    return current
            current = current.parent
        return file_path.parent

    async def start_server(self, language: str) -> bool:
        """Start LSP server for a language."""
        if not PYGLS_AVAILABLE:
            return False

        config = self.get_server_config(language)
        # This is a simplified version - in reality we'd need a full LSP client
        # For now, we'll use command-line tools for basic operations
        return bool(config)


_lsp_manager = LSPManager()


async def find_symbol(symbol: str, directory: str = ".", extensions: str = "") -> str:
    """Find symbol definitions across files using regex search."""
    dir_path = Path(directory).expanduser().resolve()
    ext_list = [e.strip() for e in extensions.split(",")] if extensions else None

    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    matches = []

    try:
        for ext in ext_list or [".py", ".js", ".ts", ".go", ".rs", ".cpp", ".h", ".java", ".cs"]:
            for file_path in dir_path.rglob(f"*{ext}"):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    for i, text in enumerate(content.splitlines(), 1):
                        if pattern.search(text):
                            rel_path = file_path.relative_to(Path(directory).expanduser().resolve())
                            matches.append(f"{rel_path}:{i}: {text.strip()}")
                            if len(matches) >= 50:
                                break
                    if len(matches) >= 50:
                        break
                except Exception as e:
                    return f"error reading {file_path}: {e}"
    except Exception as e:
        return f"error: {e}"

    if not matches:
        return f"No matches found for '{symbol}'"

    return f"Found {len(matches)} matches for '{symbol}':\n" + "\n".join(matches)


async def find_references(symbol: str, file_path: str, line: int = 0) -> str:
    """Find references to a symbol."""
    file_path_obj = Path(file_path).expanduser().resolve()
    if not file_path_obj.exists():
        return f"error: file not found: {file_path}"

    try:
        content = file_path_obj.read_text(encoding="utf-8", errors="ignore")

        # Simple reference finding - look for the symbol in other files
        symbol_pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        matches = []

        root = file_path_obj.parent
        for ext in [".py", ".js", ".ts", ".go", ".rs", ".cpp", ".h", ".java", ".cs"]:
            for f in root.rglob(f"*{ext}"):
                if f == file_path_obj:
                    continue
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                    for i, text in enumerate(content.splitlines(), 1):
                        if symbol_pattern.search(text):
                            rel = f.relative_to(Path(file_path).parent)
                            matches.append(f"{rel}:{i}: {text.strip()}")
                            if len(matches) >= 50:
                                break
                    if len(matches) >= 50:
                        break
                except Exception as e:
                    return f"error reading {f}: {e}"
    except Exception as e:
        return f"error: {e}"

    if not matches:
        return f"No references found for '{symbol}'"

    return f"Found {len(matches)} references to '{symbol}':\n" + "\n".join(matches)


async def lint_file(path: str) -> str:
    """Lint a file using appropriate linter."""
    path_obj = Path(path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: file not found: {path}"

    ext = path_obj.suffix.lower()

    try:
        if ext == ".py":
            # Use ruff or pylint
            if shutil.which("ruff"):
                proc = await asyncio.create_subprocess_exec(
                    "ruff",
                    "check",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            elif shutil.which("pylint"):
                proc = await asyncio.create_subprocess_exec(
                    "pylint",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "error: no Python linter found (install ruff or pylint)"
        elif ext in (".js", ".ts", ".jsx", ".tsx"):
            if shutil.which("eslint"):
                proc = await asyncio.create_subprocess_exec(
                    "eslint",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "error: eslint not installed"
        elif ext == ".go":
            if shutil.which("golint"):
                proc = await asyncio.create_subprocess_exec(
                    "golint",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "error: golint not installed"
        elif ext == ".rs":
            if shutil.which("cargo"):
                proc = await asyncio.create_subprocess_exec(
                    "cargo",
                    "clippy",
                    "--",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "error: cargo not installed"
        else:
            return f"no linter configured for {ext}"

        stdout, stderr = await proc.communicate()
        output = stdout.decode(errors="replace")
        if stderr:
            output += "\n" + stderr.decode(errors="replace")
        return output if output else "No lint issues found"
    except Exception as e:
        return f"error: {e}"


async def format_code(path: str) -> str:
    """Format a file using appropriate formatter."""
    path_obj = Path(path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: file not found: {path}"

    ext = path_obj.suffix.lower()

    try:
        if ext == ".py":
            if shutil.which("black"):
                proc = await asyncio.create_subprocess_exec(
                    "black",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return stdout.decode(errors="replace") or "formatted with black"
            elif shutil.which("autopep8"):
                proc = await asyncio.create_subprocess_exec(
                    "autopep8",
                    "--in-place",
                    "--aggressive",
                    "--aggressive",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return stdout.decode(errors="replace") or "formatted with autopep8"
            return "no Python formatter found (install black or autopep8)"
        elif ext in (".js", ".ts", ".jsx", ".tsx", ".json"):
            if shutil.which("prettier"):
                proc = await asyncio.create_subprocess_exec(
                    "prettier",
                    "--write",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return stdout.decode(errors="replace") or "formatted with prettier"
            return "error: prettier not installed"
        elif ext == ".go":
            if shutil.which("gofmt"):
                proc = await asyncio.create_subprocess_exec(
                    "gofmt",
                    "-w",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return stdout.decode(errors="replace") or "formatted with gofmt"
            return "error: gofmt not found"
        elif ext == ".rs":
            if shutil.which("rustfmt"):
                proc = await asyncio.create_subprocess_exec(
                    "rustfmt",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return stdout.decode(errors="replace") or "formatted with rustfmt"
            return "error: rustfmt not installed"
        else:
            return f"no formatter configured for {ext}"
    except Exception as e:
        return f"error: {e}"


async def typecheck_file(path: str) -> str:
    """Type check a file."""
    path_obj = Path(path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: file not found: {path}"

    ext = path_obj.suffix.lower()

    try:
        if ext == ".py":
            if shutil.which("mypy"):
                proc = await asyncio.create_subprocess_exec(
                    "mypy",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            elif shutil.which("pyright"):
                proc = await asyncio.create_subprocess_exec(
                    "pyright",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "error: no Python type checker found (install mypy or pyright)"
        elif ext in (".ts", ".tsx"):
            if shutil.which("tsc"):
                proc = await asyncio.create_subprocess_exec(
                    "tsc",
                    "--noEmit",
                    str(path_obj),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                return "error: tsc not installed"
        else:
            return f"no type checker for {ext}"

        stdout, stderr = await proc.communicate()
        output = stdout.decode(errors="replace")
        if stderr:
            output += "\n" + stderr.decode(errors="replace")
        return output if output else "Type check passed"
    except Exception as e:
        return f"error: {e}"


async def refactor_rename(old_name: str, new_name: str, root: str = ".") -> str:
    """Rename a symbol across files (simple text replacement)."""
    if old_name == new_name:
        return "old and new names are the same"

    pattern = re.compile(rf"\b{re.escape(old_name)}\b")
    changes = []

    try:
        for ext in [".py", ".js", ".ts", ".go", ".rs", ".cpp", ".h", ".java", ".cs"]:
            for file_path in Path(root).rglob(f"*{ext}"):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if pattern.search(content):
                        new_content = pattern.sub(new_name, content)
                        file_path.write_text(new_content, encoding="utf-8")
                        rel = file_path.relative_to(Path.cwd())
                        changes.append(str(rel))
                except Exception:
                    pass
    except Exception as e:
        return f"error: {e}"

    if not changes:
        return f"no occurrences of '{old_name}' found"
    return f"Renamed '{old_name}' to '{new_name}' in {len(changes)} files:\n" + "\n".join(changes)


async def run_tests(directory: str = ".", test_pattern: str = "pytest") -> str:
    """Run test suite in a directory."""
    try:
        proc = await asyncio.create_subprocess_shell(
            f"{test_pattern} -q",
            cwd=directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = f"Exit code: {proc.returncode}\n"
        if stdout:
            output += f"\n{stdout.decode(errors='replace')[:3000]}"
        if stderr:
            output += f"\nErrors:\n{stderr.decode(errors='replace')[:1500]}"
        return output
    except Exception as e:
        return f"error: {e}"


async def get_diagnostics(path: str) -> str:
    """Get diagnostics for a file (syntax errors, warnings)."""
    return await lint_file(path)


def register_code_intel(box: ToolBox) -> None:
    box.register(
        Tool(
            name="find_symbol",
            description="Find symbol definitions across files in a project",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "directory": {"type": "string", "default": "."},
                    "extensions": {"type": "string", "default": ""},
                },
                "required": ["symbol"],
            },
            handler=find_symbol,
        )
    )
    box.register(
        Tool(
            name="find_references",
            description="Find references to a symbol in a project",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "file_path": {"type": "string"},
                    "line": {"type": "integer", "default": 0},
                },
                "required": ["symbol", "file_path"],
            },
            handler=find_references,
        )
    )
    box.register(
        Tool(
            name="lint_file",
            description="Lint a file using appropriate linter (ruff, pylint, eslint, golint, clippy)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lint_file,
        )
    )
    box.register(
        Tool(
            name="format_code",
            description="Format a file using appropriate formatter (black, prettier, gofmt, rustfmt)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=format_code,
        )
    )
    box.register(
        Tool(
            name="typecheck_file",
            description="Type check a file (mypy, pyright, tsc)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=typecheck_file,
        )
    )
    box.register(
        Tool(
            name="refactor_rename",
            description="Rename a symbol across files in a project",
            parameters={
                "type": "object",
                "properties": {
                    "old_name": {"type": "string"},
                    "new_name": {"type": "string"},
                    "root": {"type": "string", "default": "."},
                },
                "required": ["old_name", "new_name"],
            },
            handler=refactor_rename,
        )
    )
    box.register(
        Tool(
            name="run_tests",
            description="Run test suite in a directory",
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
            name="get_diagnostics",
            description="Get diagnostics for a file (syntax errors, warnings)",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=get_diagnostics,
        )
    )

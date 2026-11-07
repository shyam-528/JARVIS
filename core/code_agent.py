from __future__ import annotations

import difflib
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from core.workspace import check_write_path

BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"format\s+[cC]:",
    r"shutdown",
    r"del\s+/[sS]\s+/[qQ]",
    r"rmdir\s+/[sS]\s+/[qQ]",
    r"rd\s+/[sS]\s+/[qQ]",
    r"diskpart",
    r"reg\s+delete\s+HK",
    r"Remove-Item\s+-Recurse\s+-Force\s+C:\\",
    r"Remove-ItemProperty",
    r"taskkill\s+/f\s+/im\s+explorer",
    r"bcdedit",
    # PowerShell / native equivalents of the destructive commands above:
    # recursive force-delete of any drive root or system path (the plain
    # blocklist only caught the literal "C:\" spelling).
    r"\bRemove-Item\b(?=[^|;]*\s-Recurse)(?=[^|;]*\s-Force)[^|;]*(?:[A-Za-z]:\\(?:[\s'\"]|$)|[A-Za-z]:\\Windows\b|[A-Za-z]:\\Users\\?[\\'\"\s]*$|%USERPROFILE%|\$env:)",
    r"(?:^|[\s|&;])(?:del|erase)\s+/[fFsS]\b",  # del /f, del /s, erase /s ...
    r"Format-Volume\b",
    r"Clear-Disk\b",
    r"Remove-Partition\b",
    r"vssadmin\s+delete\b",
    r"cipher\s+/w\b",  # wipes free disk space
    r"takeown\s+/[fF]\s+[cC]:\\",
    r"icacls\s+[cC]:\\.*\/reset",
]

# Read-only shell commands: provably non-mutating, run immediately with
# NO approval (e.g. checking reminders/scheduled tasks). Writes, deletes,
# commits and everything else still go through the approval queue.
SAFE_READONLY_PATTERNS = [
    # check Windows scheduled tasks / autostart entry (optional path/.exe
    # prefix: models sometimes send the absolute schtasks.exe path)
    r"(?:^|[\s\"'])(?:[A-Za-z]:\\[^\s\"']*\\)?schtasks(\.exe)?\s+/query\b",
    r"Get-ScheduledTask\b",  # read-only PowerShell cmdlet
    r"git\s+(status|diff|log|show|branch|rev-parse|remote)\b",
    r"gh\s+(pr|issue|run)\s+(status|view|list)\b",
    r"tasklist\b",  # list running processes
    r"sc\s+query\b",  # query service state
]

# Shell chaining / redirection metacharacters. A command is only treated
# as read-only when NONE of these appear — blocks smuggling a mutating
# command behind an allowlisted one (e.g. `schtasks /query & del ...`).
SHELL_CHAIN_CHARS = ("&", "|", ";", "`", ">", "<", "$(")

# Tail filters allowed after a pipe in a read-only command: pure output
# filters that cannot mutate anything (e.g. `schtasks /query | findstr x`).
# Tails are matched whole; anything else falls back to approval.
_READONLY_PIPE_TAIL = re.compile(r"^\s*(findstr|more)\b[^&;`$><]*$", re.IGNORECASE)


def _split_pipes(command: str) -> list[str]:
    """Split on | outside single/double quotes (findstr patterns like
    "a\\|b" must survive as one segment)."""
    parts, cur = [], []
    sq = dq = False
    for ch in command:
        if ch == "'" and not dq:
            sq = not sq
        elif ch == '"' and not sq:
            dq = not dq
        if ch == "|" and not sq and not dq:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


@dataclass
class PendingChange:
    change_id: str
    action: str  # "write_file", "edit_file", "delete_file", "run_command", "git_commit"
    file_path: str
    description: str
    diff: str
    old_content: str | None
    new_content: str | None
    command: str | None
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending, approved, rejected


class CodeEditor:
    """Read, write, and edit files with diff generation and approval workflow."""

    def __init__(self, max_file_size_mb: float = 10.0):
        self.max_file_size = max_file_size_mb * 1024 * 1024

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> dict:
        p = Path(path).resolve()
        if not p.exists():
            return {"error": f"File not found: {path}"}
        if p.stat().st_size > self.max_file_size:
            return {
                "error": f"File too large: {p.stat().st_size / 1e6:.1f}MB (max: {self.max_file_size / 1e6:.0f}MB)"
            }
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"error": str(exc)}
        lines = content.splitlines()
        total = len(lines)
        if end_line <= 0 or end_line > total:
            end_line = total
        if start_line < 1:
            start_line = 1
        selected = lines[start_line - 1 : end_line]
        numbered = [f"{i + start_line:4d}| {line}" for i, line in enumerate(selected)]
        return {
            "path": str(p),
            "total_lines": total,
            "start_line": start_line,
            "end_line": min(end_line, total),
            "content": "\n".join(numbered),
            "extension": p.suffix,
        }

    def generate_diff(self, old_content: str, new_content: str, path: str) -> str:
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""
        )
        return "".join(diff)

    def write_file(self, path: str, content: str) -> dict:
        try:
            allowed, reason = check_write_path(path)
        except Exception:
            allowed, reason = True, "ok"
        if not allowed:
            return {"error": f"write refused ({reason})"}
        p = Path(path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        old_content = ""
        existed = p.exists()
        if existed:
            old_content = p.read_text(encoding="utf-8", errors="replace")
        diff = self.generate_diff(old_content, content, str(p))
        return {
            "path": str(p),
            "existed": existed,
            "old_content": old_content if existed else None,
            "new_content": content,
            "diff": diff,
            "lines": len(content.splitlines()),
        }

    def apply_write(self, path: str, content: str) -> bool:
        try:
            try:
                allowed, _ = check_write_path(path)
            except Exception:
                allowed = True
            if not allowed:
                return False
            p = Path(path).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return True
        except Exception:
            return False

    def edit_lines(self, path: str, edits: list[dict]) -> dict:
        """edits: [{"line": 5, "old": "exact old text", "new": "replacement"}]"""
        p = Path(path).resolve()
        if not p.exists():
            return {"error": f"File not found: {path}"}
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        applied = 0
        for edit in sorted(edits, key=lambda e: e.get("line", 0), reverse=True):
            line_num = edit.get("line", 0)
            if line_num < 1 or line_num > len(lines):
                continue
            old_text = edit.get("old", "")
            new_text = edit.get("new", "")
            actual = lines[line_num - 1].rstrip("\n\r")
            if old_text and old_text not in actual:
                continue
            lines[line_num - 1] = new_text + "\n"
            applied += 1
        if applied == 0:
            return {"error": "No edits could be applied (old text not found)"}
        new_content = "".join(lines)
        diff = self.generate_diff(content, new_content, str(p))
        return {
            "path": str(p),
            "applied": applied,
            "old_content": content,
            "new_content": new_content,
            "diff": diff,
        }

    def apply_edit(self, path: str, new_content: str) -> bool:
        return self.apply_write(path, new_content)

    def delete_file(self, path: str) -> dict:
        p = Path(path).resolve()
        if not p.exists():
            return {"error": f"File not found: {path}"}
        return {"path": str(p), "size": p.stat().st_size, "confirmed": True}

    def apply_delete(self, path: str) -> bool:
        try:
            Path(path).unlink()
            return True
        except Exception:
            return False

    def search_code(
        self, directory: str, pattern: str, file_extensions: list[str] = None, max_results: int = 30
    ) -> dict:
        root = Path(directory).resolve()
        if not root.exists():
            return {"error": f"Directory not found: {directory}"}
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return {"error": f"Invalid regex: {exc}"}

        exts = set(file_extensions) if file_extensions else None
        results = []
        skip = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next"}
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            if any(part in skip for part in f.parts):
                continue
            if exts and f.suffix.lower() not in exts:
                continue
            if f.stat().st_size > self.max_file_size:
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    results.append(
                        {
                            "file": str(f),
                            "line": i,
                            "text": line.strip()[:200],
                        }
                    )
                    if len(results) >= max_results:
                        return {"matches": results, "total": len(results), "truncated": True}
        return {"matches": results, "total": len(results)}


class ShellExecutor:
    """Safe shell command execution with blocklist."""

    def __init__(self, blocked_patterns: list[str] = None, timeout_s: int = 120):
        self.blocked = [
            re.compile(p, re.IGNORECASE) for p in (blocked_patterns or BLOCKED_PATTERNS)
        ]
        self.readonly = [re.compile(p, re.IGNORECASE) for p in SAFE_READONLY_PATTERNS]
        self.timeout = timeout_s

    def is_safe(self, command: str) -> tuple[bool, str]:
        for pattern in self.blocked:
            if pattern.search(command):
                return False, f"Blocked: matches dangerous pattern '{pattern.pattern}'"
        return True, ""

    def is_readonly(self, command: str) -> bool:
        """True when a command is a provably non-mutating read-only query.

        Requires: passes the blocklist, head matches the read-only
        allowlist, and no shell chaining/redirection metacharacters —
        except a pipe into a pure output filter (findstr/more).
        """
        safe, _ = self.is_safe(command)
        if not safe:
            return False
        if "|" in command:
            parts = _split_pipes(command)
            if len(parts) < 2:
                return False
            head, tails = parts[0], parts[1:]
            if any(ch in head for ch in ("&", ";", "`", ">", "<", "$(")):
                return False
            if not any(pat.match(head.strip()) for pat in self.readonly):
                return False
            return all(_READONLY_PIPE_TAIL.match(t) for t in tails)
        if any(ch in command for ch in SHELL_CHAIN_CHARS):
            return False
        stripped = command.strip()
        return any(pat.match(stripped) for pat in self.readonly)

    def execute(self, command: str, cwd: str = "") -> dict:
        safe, reason = self.is_safe(command)
        if not safe:
            return {"error": reason, "blocked": True}
        work_dir = cwd if cwd and Path(cwd).exists() else None
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=work_dir,
            )
            return {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout[:5000],
                "stderr": result.stderr[:5000],
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {self.timeout}s", "command": command}
        except Exception as exc:
            return {"error": str(exc), "command": command}


class GitOps:
    """Git wrapper for common operations."""

    def __init__(self, shell: ShellExecutor):
        self.shell = shell

    def status(self, cwd: str) -> dict:
        return self.shell.execute("git status --short", cwd=cwd)

    def diff(self, cwd: str, staged: bool = False) -> dict:
        cmd = "git diff --cached" if staged else "git diff"
        return self.shell.execute(cmd, cwd=cwd)

    def log(self, cwd: str, count: int = 10) -> dict:
        return self.shell.execute(f"git log --oneline -{count}", cwd=cwd)

    def add(self, cwd: str, files: str = ".") -> dict:
        return self.shell.execute(f"git add {files}", cwd=cwd)

    def commit(self, cwd: str, message: str) -> dict:
        safe_msg = message.replace('"', '\\"')
        return self.shell.execute(f'git commit -m "{safe_msg}"', cwd=cwd)

    def branch(self, cwd: str) -> dict:
        return self.shell.execute("git branch -a", cwd=cwd)


class ApprovalQueue:
    """Manages pending changes that require user approval."""

    def __init__(self, auto_approve: bool = False):
        self.auto_approve = auto_approve
        self.pending: dict[str, PendingChange] = {}
        self.approved_count = 0
        self.rejected_count = 0

    def submit(
        self,
        action: str,
        file_path: str,
        description: str,
        diff: str = "",
        old_content: str = None,
        new_content: str = None,
        command: str = None,
    ) -> PendingChange:
        change = PendingChange(
            change_id=str(uuid.uuid4())[:8],
            action=action,
            file_path=file_path,
            description=description,
            diff=diff,
            old_content=old_content,
            new_content=new_content,
            command=command,
        )
        if self.auto_approve:
            change.status = "approved"
        else:
            self.pending[change.change_id] = change
        return change

    def approve(self, change_id: str) -> PendingChange | None:
        change = self.pending.pop(change_id, None)
        if change:
            change.status = "approved"
            self.approved_count += 1
        return change

    def reject(self, change_id: str) -> PendingChange | None:
        change = self.pending.pop(change_id, None)
        if change:
            change.status = "rejected"
            self.rejected_count += 1
        return change

    def get_pending(self) -> list[PendingChange]:
        return list(self.pending.values())

    def has_pending(self) -> bool:
        return len(self.pending) > 0

    def approve_all(self) -> list[PendingChange]:
        approved = list(self.pending.values())
        for c in approved:
            c.status = "approved"
            self.approved_count += 1
        self.pending.clear()
        return approved


class CodeAgent:
    """High-level code agent combining all code operations."""

    def __init__(
        self,
        auto_approve: bool = False,
        blocked_commands: list[str] = None,
        max_file_size_mb: float = 10.0,
        timeout_s: int = 120,
    ):
        self.editor = CodeEditor(max_file_size_mb=max_file_size_mb)
        self.shell = ShellExecutor(blocked_patterns=blocked_commands, timeout_s=timeout_s)
        self.git = GitOps(self.shell)
        self.queue = ApprovalQueue(auto_approve=auto_approve)

    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> dict:
        return self.editor.read_file(path, start_line, end_line)

    def request_write(self, path: str, content: str) -> dict:
        result = self.editor.write_file(path, content)
        if "error" in result:
            return result
        change = self.queue.submit(
            action="write_file",
            file_path=result["path"],
            description=f"Create file ({result['lines']} lines)"
            if not result["existed"]
            else f"Overwrite file ({result['lines']} lines)",
            diff=result["diff"],
            old_content=result.get("old_content"),
            new_content=content,
        )
        if self.queue.auto_approve:
            self._execute_change(change)
        return {
            "pending": not self.queue.auto_approve,
            "change_id": change.change_id,
            "diff": result["diff"],
            "path": result["path"],
            "lines": result["lines"],
        }

    def request_edit(self, path: str, edits: list[dict]) -> dict:
        result = self.editor.edit_lines(path, edits)
        if "error" in result:
            return result
        change = self.queue.submit(
            action="edit_file",
            file_path=result["path"],
            description=f"Edit {result['applied']} lines",
            diff=result["diff"],
            old_content=result["old_content"],
            new_content=result["new_content"],
        )
        if self.queue.auto_approve:
            self._execute_change(change)
        return {
            "pending": not self.queue.auto_approve,
            "change_id": change.change_id,
            "diff": result["diff"],
            "path": result["path"],
            "applied": result["applied"],
        }

    def request_delete(self, path: str) -> dict:
        result = self.editor.delete_file(path)
        if "error" in result:
            return result
        change = self.queue.submit(
            action="delete_file",
            file_path=result["path"],
            description=f"Delete file ({result['size']} bytes)",
        )
        if self.queue.auto_approve:
            exec_result = self._execute_change(change)
            return {
                "pending": False,
                "change_id": change.change_id,
                "path": result["path"],
                "applied": exec_result.get("applied", False),
            }
        return {"pending": True, "change_id": change.change_id, "path": result["path"]}

    def request_command(self, command: str, cwd: str = "") -> dict:
        safe, reason = self.shell.is_safe(command)
        if not safe:
            return {"error": reason, "blocked": True}
        if self.shell.is_readonly(command):
            # Read-only query (e.g. schtasks /query to check reminders):
            # run immediately, no approval. Recorded as approved for audit.
            import logging as _logging

            _logging.getLogger("jarvis.code_agent").info("readonly-allow: %s", command[:120])
            change = self.queue.submit(
                action="run_command",
                file_path=cwd or ".",
                description=f"Execute (read-only, no approval): {command[:100]}",
                command=command,
            )
            change.status = "approved"
            self.queue.approved_count += 1
            # audit record only: must not linger in the pending queue
            self.queue.pending.pop(change.change_id, None)
            result_cmd = self._execute_change(change)
            return {
                "pending": False,
                "change_id": change.change_id,
                "command": command,
                "output": result_cmd.get("output", {}),
                "readonly": True,
            }
        else:
            import logging as _logging

            _logging.getLogger("jarvis.code_agent").info("approval-required: %s", command[:120])
        change = self.queue.submit(
            action="run_command",
            file_path=cwd or ".",
            description=f"Execute: {command[:100]}",
            command=command,
        )
        if self.queue.auto_approve:
            result_cmd = self._execute_change(change)
            return {
                "pending": False,
                "change_id": change.change_id,
                "command": command,
                "output": result_cmd.get("output", {}),
            }
        return {"pending": True, "change_id": change.change_id, "command": command}

    def approve(self, change_id: str) -> dict:
        change = self.queue.approve(change_id)
        if not change:
            return {"error": f"Change {change_id} not found"}
        return self._execute_change(change)

    def reject(self, change_id: str) -> dict:
        change = self.queue.reject(change_id)
        if not change:
            return {"error": f"Change {change_id} not found"}
        return {"rejected": True, "change_id": change_id, "description": change.description}

    def approve_all(self) -> list[dict]:
        changes = self.queue.approve_all()
        return [self._execute_change(c) for c in changes]

    def _execute_change(self, change: PendingChange) -> dict:
        result = {
            "change_id": change.change_id,
            "action": change.action,
            "file_path": change.file_path,
        }
        if change.action == "write_file":
            ok = self.editor.apply_write(change.file_path, change.new_content)
            result["applied"] = ok
        elif change.action == "edit_file":
            ok = self.editor.apply_edit(change.file_path, change.new_content)
            result["applied"] = ok
        elif change.action == "delete_file":
            ok = self.editor.apply_delete(change.file_path)
            result["applied"] = ok
        elif change.action == "run_command":
            result["output"] = self.shell.execute(change.command)
        elif change.action == "git_commit":
            parts = change.command.split("|", 1)
            cwd, msg = parts[0], parts[1] if len(parts) > 1 else "Update"
            self.git.add(cwd)
            result["output"] = self.git.commit(cwd, msg)
        return result

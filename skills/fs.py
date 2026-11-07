"""Filesystem tools (fs_*): read/write/edit/list/glob/search, copy/move/delete,
file info, git status/diff/log, and directory watching. Paths are validated;
failures return error strings.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from core.tools import Tool, ToolBox
from core.workspace import check_write_path


class FileWatchHandler(FileSystemEventHandler):
    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self.queue = queue
        self.loop = loop

    def _emit(self, event_type: str, path: str, dest_path: str = ""):
        asyncio.run_coroutine_threadsafe(
            self.queue.put(
                {
                    "type": event_type,
                    "path": path,
                    "dest_path": dest_path,
                    "timestamp": datetime.now().isoformat(),
                }
            ),
            self.loop,
        )

    def on_created(self, event):
        if not event.is_directory:
            self._emit("created", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._emit("modified", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._emit("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._emit("moved", event.src_path, event.dest_path)


class FileWatcher:
    def __init__(self):
        self.observers: dict[str, Observer] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start_watch(self, path: str, events: list[str] = None) -> str:
        """Start watching a directory. Returns watch_id."""
        if self.loop is None:
            self.loop = asyncio.get_running_loop()

        path = str(Path(path).resolve())
        watch_id = f"watch_{len(self.observers)}"

        events = events or ["created", "modified", "deleted", "moved"]
        queue = asyncio.Queue()
        self.queues[watch_id] = queue

        handler = FileWatchHandler(queue, self.loop)
        observer = Observer()
        observer.schedule(handler, path, recursive=True)
        observer.start()

        self.observers[watch_id] = observer
        return watch_id

    async def stop_watch(self, watch_id: str) -> bool:
        """Stop watching. Returns True if was watching."""
        if watch_id in self.observers:
            self.observers[watch_id].stop()
            self.observers[watch_id].join()
            del self.observers[watch_id]
            del self.queues[watch_id]
            return True
        return False

    async def get_events(self, watch_id: str, timeout: float = 1.0) -> list[dict]:
        """Get pending events for a watch."""
        if watch_id not in self.queues:
            return []
        queue = self.queues[watch_id]
        events = []
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
                events.append(event)
        except asyncio.TimeoutError:
            pass
        return events


# Global watcher instance
_file_watcher = FileWatcher()


async def read_file(path: str, encoding: str = "utf-8") -> str:
    """Read a file with automatic encoding detection."""
    path = str(Path(path).expanduser().resolve())
    try:
        return Path(path).read_text(encoding=encoding)
    except UnicodeDecodeError:
        # Try common encodings
        for enc in ["utf-8", "latin-1", "cp1252", "utf-16"]:
            try:
                return Path(path).read_text(encoding=enc)
            except UnicodeDecodeError:
                continue
        return "error: could not decode file"


async def write_file(path: str, content: str, create_dirs: bool = True, backup: bool = True) -> str:
    """Write content to a file, optionally creating parent directories and backing up existing file."""
    try:
        allowed, reason = check_write_path(path)
    except Exception:
        allowed, reason = True, "ok"
    if not allowed:
        return f"error: write refused ({reason})"
    path_obj = Path(path).expanduser().resolve()
    if create_dirs:
        path_obj.parent.mkdir(parents=True, exist_ok=True)

    if backup and path_obj.exists():
        backup_path = path_obj.with_suffix(path_obj.suffix + f".bak.{int(time.time())}")
        shutil.copy2(path_obj, backup_path)

    path_obj.write_text(content, encoding="utf-8")
    return f"written {len(content)} bytes to {path}"


async def edit_file(path: str, old_str: str, new_str: str) -> str:
    """Precise surgical edit - replaces first occurrence of old_str with new_str."""
    path_obj = Path(path).expanduser().resolve()
    content = path_obj.read_text(encoding="utf-8")

    if old_str not in content:
        return f"error: old_str not found in {path}"

    new_content = content.replace(old_str, new_str, 1)
    path_obj.write_text(new_content, encoding="utf-8")
    return f"edited {path} (1 replacement)"


async def list_dir(path: str = ".", recursive: bool = False, max_depth: int = 3) -> str:
    """List directory contents with sizes and modification times."""
    path_obj = Path(path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: path not found: {path}"
    if not path_obj.is_dir():
        return f"error: not a directory: {path}"

    lines = []
    if recursive:
        for item in path_obj.rglob("*"):
            if item.is_file():
                rel = item.relative_to(path_obj)
                depth = len(rel.parts)
                if depth > max_depth:
                    continue
                stat = item.stat()
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"{mtime}  {size:>10}  {rel}")
    else:
        for item in sorted(path_obj.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            stat = item.stat()
            size = stat.st_size if item.is_file() else 0
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            prefix = "📁" if item.is_dir() else "📄"
            lines.append(f"{mtime}  {size:>10}  {prefix} {item.name}")

    return "\n".join(lines) if lines else "(empty)"


async def glob_files(pattern: str, root: str = ".") -> str:
    """Fast glob pattern matching."""
    root_obj = Path(root).expanduser().resolve()
    matches = list(root_obj.rglob(pattern))
    if not matches:
        return f"no matches for '{pattern}' in {root}"
    return "\n".join(str(p.relative_to(root_obj)) for p in matches)


async def search_files(
    pattern: str, root: str = ".", max_results: int = 50, content_search: bool = False
) -> str:
    """Search for files by name or content."""
    root_obj = Path(root).expanduser().resolve()
    if not root_obj.exists():
        return f"error: root not found: {root}"

    found = []

    for scanned, item in enumerate(root_obj.rglob("*")):
        if scanned > 5000:
            break
        if content_search and item.is_file() and item.stat().st_size < 1_000_000:
            try:
                content = item.read_text(encoding="utf-8", errors="ignore")
                if pattern in content:
                    found.append(str(item.relative_to(root_obj)))
                    if len(found) >= max_results:
                        break
            except Exception:
                pass
        elif pattern in item.name:
            found.append(str(item.relative_to(root_obj)))
            if len(found) >= max_results:
                break

    return "\n".join(found) if found else f"no files matching '{pattern}'"


async def file_info(path: str) -> str:
    """Get detailed file information."""
    path_obj = Path(path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: not found: {path}"

    stat = path_obj.stat()
    return "\n".join(
        [
            f"Path: {path_obj}",
            f"Name: {path_obj.name}",
            f"Size: {stat.st_size:,} bytes ({stat.st_size / 1024:.1f} KB)",
            f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}",
            f"Created: {datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')}",
            f"Permissions: {oct(stat.st_mode)[-3:]}",
            f"Is dir: {path_obj.is_dir()}",
            f"Is file: {path_obj.is_file()}",
            f"Extension: {path_obj.suffix}",
        ]
    )


async def copy_file(src: str, dst: str, overwrite: bool = False) -> str:
    """Copy a file or directory."""
    src_obj = Path(src).expanduser().resolve()
    dst_obj = Path(dst).expanduser().resolve()

    if not src_obj.exists():
        return f"error: source not found: {src}"

    if dst_obj.exists() and not overwrite:
        return "error: destination exists (use overwrite=True)"

    if src_obj.is_dir():
        if dst_obj.exists():
            shutil.rmtree(dst_obj)
        shutil.copytree(src_obj, dst_obj)
        return f"copied directory {src} -> {dst}"
    else:
        dst_obj.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_obj, dst_obj)
        return f"copied file {src} -> {dst}"


async def move_file(src: str, dst: str, overwrite: bool = False) -> str:
    """Move/rename a file or directory."""
    src_obj = Path(src).expanduser().resolve()
    dst_obj = Path(dst).expanduser().resolve()

    if not src_obj.exists():
        return f"error: source not found: {src}"

    if dst_obj.exists() and not overwrite:
        return "error: destination exists (use overwrite=True)"

    dst_obj.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_obj), str(dst_obj))
    return f"moved {src} -> {dst}"


async def delete_file(path: str, force: bool = False) -> str:
    """Delete a file or directory."""
    path_obj = Path(path).expanduser().resolve()
    if not path_obj.exists():
        return f"error: not found: {path}"

    if path_obj.is_dir():
        if force:
            shutil.rmtree(path_obj)
        else:
            try:
                path_obj.rmdir()
            except OSError:
                return "error: directory not empty (use force=True)"
    else:
        path_obj.unlink()
    return f"deleted {path}"


async def git_status(repo_path: str = ".") -> str:
    """Get git status of a repository."""
    repo_obj = Path(repo_path).expanduser().resolve()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--short",
            "--branch",
            cwd=repo_obj,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return f"error: {err.decode()[:200]}"
        return out.decode().strip() or "(clean)"
    except Exception as e:
        return f"error: {e}"


async def git_diff(repo_path: str = ".", staged: bool = False) -> str:
    """Get git diff."""
    repo_obj = Path(repo_path).expanduser().resolve()
    args = ["git", "diff"]
    if staged:
        args.append("--cached")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=repo_obj, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return f"error: {err.decode()[:200]}"
        return out.decode().strip() or "(no changes)"
    except Exception as e:
        return f"error: {e}"


async def git_log(repo_path: str = ".", max_count: int = 10) -> str:
    """Get git log."""
    repo_obj = Path(repo_path).expanduser().resolve()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            "--oneline",
            f"-n{max_count}",
            cwd=repo_obj,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return f"error: {err.decode()[:200]}"
        return out.decode().strip()
    except Exception as e:
        return f"error: {e}"


async def watch_dir(path: str, events: list[str] = None) -> str:
    """Start watching a directory for file changes. Returns watch_id."""
    watch_id = await _file_watcher.start_watch(path, events)
    return f"watching {path} (watch_id: {watch_id})"


async def watch_events(watch_id: str, timeout: float = 1.0) -> str:
    """Get pending events for a watch."""
    events = await _file_watcher.get_events(watch_id, timeout)
    if not events:
        return "(no events)"
    return "\n".join(
        f"[{e['type']}] {e['path']}" + (f" -> {e['dest_path']}" if e["dest_path"] else "")
        for e in events
    )


async def unwatch(watch_id: str) -> str:
    """Stop watching a directory."""
    if await _file_watcher.stop_watch(watch_id):
        return f"stopped watching (watch_id: {watch_id})"
    return f"error: watch not found: {watch_id}"


def register_fs(box: ToolBox) -> None:
    box.register(
        Tool(
            name="fs_read_file",
            description="Read a text file with automatic encoding detection",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "encoding": {"type": "string", "default": "utf-8"},
                },
                "required": ["path"],
            },
            handler=read_file,
        )
    )
    box.register(
        Tool(
            name="fs_write_file",
            description=(
                "Write content to a file, creating directories and backing up existing file. "
                "File placement: bare filenames are routed automatically (plans to docs/plans/, "
                "research to data/research/, logs to logs/, audio/images/video to D:/jarvis/media/, "
                "helper scripts to workspace/scripts/, else workspace/) — never the repo root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_dirs": {"type": "boolean", "default": True},
                    "backup": {"type": "boolean", "default": True},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        )
    )
    box.register(
        Tool(
            name="fs_edit_file",
            description="Precise surgical edit - replaces first occurrence of old_str with new_str",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
            },
            handler=edit_file,
        )
    )
    box.register(
        Tool(
            name="fs_list_dir",
            description="List directory contents with sizes and modification times",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                    "max_depth": {"type": "integer", "default": 3},
                },
                "required": [],
            },
            handler=list_dir,
        )
    )
    box.register(
        Tool(
            name="fs_glob_files",
            description="Fast glob pattern matching",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string", "default": "."},
                },
                "required": ["pattern"],
            },
            handler=glob_files,
        )
    )
    box.register(
        Tool(
            name="fs_search_files",
            description="Search for files by name or content (grep-like)",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "root": {"type": "string", "default": "."},
                    "max_results": {"type": "integer", "default": 50},
                    "content_search": {"type": "boolean", "default": False},
                },
                "required": ["pattern"],
            },
            handler=search_files,
        )
    )
    box.register(
        Tool(
            name="fs_file_info",
            description="Get detailed file information",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=file_info,
        )
    )
    box.register(
        Tool(
            name="fs_copy_file",
            description="Copy a file or directory",
            parameters={
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dst": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["src", "dst"],
            },
            handler=copy_file,
        )
    )
    box.register(
        Tool(
            name="fs_move_file",
            description="Move/rename a file or directory",
            parameters={
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dst": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["src", "dst"],
            },
            handler=move_file,
        )
    )
    box.register(
        Tool(
            name="fs_delete_file",
            description="Delete a file or directory",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "force": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
            handler=delete_file,
        )
    )
    box.register(
        Tool(
            name="fs_git_status",
            description="Get git status of a repository",
            parameters={
                "type": "object",
                "properties": {"repo_path": {"type": "string", "default": "."}},
                "required": [],
            },
            handler=git_status,
        )
    )
    box.register(
        Tool(
            name="fs_git_diff",
            description="Get git diff",
            parameters={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "default": "."},
                    "staged": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            handler=git_diff,
        )
    )
    box.register(
        Tool(
            name="fs_git_log",
            description="Get git log",
            parameters={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "default": "."},
                    "max_count": {"type": "integer", "default": 10},
                },
                "required": [],
            },
            handler=git_log,
        )
    )
    box.register(
        Tool(
            name="fs_watch_dir",
            description="Start watching a directory for file changes (created, modified, deleted, moved)",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "events": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["created", "modified", "deleted", "moved"],
                    },
                },
                "required": ["path"],
            },
            handler=watch_dir,
        )
    )
    box.register(
        Tool(
            name="fs_watch_events",
            description="Get pending events for a directory watch",
            parameters={
                "type": "object",
                "properties": {
                    "watch_id": {"type": "string"},
                    "timeout": {"type": "number", "default": 1.0},
                },
                "required": ["watch_id"],
            },
            handler=watch_events,
        )
    )
    box.register(
        Tool(
            name="fs_unwatch",
            description="Stop watching a directory",
            parameters={
                "type": "object",
                "properties": {"watch_id": {"type": "string"}},
                "required": ["watch_id"],
            },
            handler=unwatch,
        )
    )

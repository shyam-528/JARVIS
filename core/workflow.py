"""Multi-step tool workflow engine: define, persist, trigger, execute, audit.

Contract: validation errors raise WorkflowValidationError (caller-visible).
Run/step audit writes are state — a failed audit write is printed loudly and
never changes the run's outcome (a disk hiccup must not turn a successful
run into a "failed" one or wedge it as "running" forever). Trigger teardown
failures are loud too: a silently orphaned scheduler task or event
subscription keeps firing a deleted workflow.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .db import connect as _db_connect
from .events import EventBus
from .safe_expr import eval_gate

MAX_STEPS = 25
SENSITIVE_TOOLS = {
    "delete_file",
    "fs_delete_file",
    "run_command",
    "sys_run_command",
    "sys_kill_process",
    "sys_shutdown",
    "git_commit",
    "notify",
    "send_keys",
    "keyboard_type",
    "sys_send_keys",
    "write_file",
    "fs_write_file",
    "edit_file",
    "fs_edit_file",
}


@dataclass
class WorkflowStep:
    tool: str
    args: dict = field(default_factory=dict)
    save_as: str = ""  # store output in $var
    condition: str = ""  # python expr over $vars; skip step if falsy
    foreach: str = ""  # var name holding a list -> run per item (as $item)
    retry: int = 0
    on_error: str = "stop"  # stop | continue
    timeout_s: int = 120

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "args": self.args,
            "save_as": self.save_as,
            "condition": self.condition,
            "foreach": self.foreach,
            "retry": self.retry,
            "on_error": self.on_error,
            "timeout_s": self.timeout_s,
        }

    @staticmethod
    def from_dict(d: dict) -> WorkflowStep:
        return WorkflowStep(
            tool=str(d["tool"]),
            args=dict(d.get("args") or {}),
            save_as=str(d.get("save_as") or ""),
            condition=str(d.get("condition") or ""),
            foreach=str(d.get("foreach") or ""),
            retry=int(d.get("retry") or 0),
            on_error=str(d.get("on_error") or "stop"),
            timeout_s=int(d.get("timeout_s") or 120),
        )


@dataclass
class WorkflowSpec:
    id: str
    name: str
    description: str = ""
    trigger: dict = field(
        default_factory=dict
    )  # {"type":"manual"} | {"type":"schedule","interval_s":N} | {"type":"event","event":"file_created"}
    steps: list[WorkflowStep] = field(default_factory=list)
    enabled: bool = True
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "steps": [s.to_dict() for s in self.steps],
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> WorkflowSpec:
        return WorkflowSpec(
            id=str(d.get("id") or uuid.uuid4().hex[:8]),
            name=str(d["name"]),
            description=str(d.get("description") or ""),
            trigger=dict(d.get("trigger") or {"type": "manual"}),
            steps=[WorkflowStep.from_dict(s) for s in (d.get("steps") or [])],
            enabled=bool(d.get("enabled", True)),
            created_at=str(d.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
        )


@dataclass
class StepRecord:
    idx: int
    tool: str
    args_json: str
    status: str  # ok | skipped | error
    output: str = ""
    error: str = ""
    duration_ms: int = 0


class WorkflowValidationError(Exception):
    pass


class WorkflowEngine:
    """Defines, persists, and executes multi-step tool workflows.

    Features: $variables between steps, conditions (safe eval), foreach loops,
    retries, continue-on-error, step timeouts, full run audit trail.
    """

    def __init__(self, orchestrator, events: EventBus, db_path: Path):
        self.orch = orchestrator
        self.events = events
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._load_cache()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._event_subs: dict[str, tuple] = {}
        self._event_run_log: dict[str, list] = {}
        # Attach triggers for pre-existing workflows (restart recovery)
        for spec in self._workflows.values():
            try:
                self._setup_triggers(spec)
            except Exception as exc:
                print(f"[Workflow] trigger setup failed for {spec.name}: {exc}")

    # ---------- storage ----------

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                trigger_json TEXT DEFAULT '{}',
                steps_json TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TEXT,
                run_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                duration_s REAL,
                error TEXT DEFAULT '',
                result TEXT DEFAULT '',
                vars_json TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS workflow_run_steps (
                run_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                tool TEXT NOT NULL,
                args_json TEXT DEFAULT '',
                status TEXT NOT NULL,
                output TEXT DEFAULT '',
                error TEXT DEFAULT '',
                duration_ms INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_runs_wf ON workflow_runs (workflow_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_steps_run ON workflow_run_steps (run_id, idx);
        """)
        self.conn.commit()

    def _load_cache(self):
        self._workflows: dict[str, WorkflowSpec] = {}
        for row in self.conn.execute("SELECT * FROM workflows").fetchall():
            try:
                spec = WorkflowSpec.from_dict(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "description": row["description"],
                        "trigger": json.loads(row["trigger_json"]),
                        "steps": json.loads(row["steps_json"]),
                        "enabled": bool(row["enabled"]),
                        "created_at": row["created_at"],
                    }
                )
                self._workflows[spec.id] = spec
            except Exception as exc:
                print(f"[Workflow] skipping bad workflow row {row['id']}: {exc}")

    # ---------- CRUD ----------

    def validate(self, spec: WorkflowSpec) -> None:
        if not spec.name or not re.match(r"^[\w\- ]{2,60}$", spec.name):
            raise WorkflowValidationError("name must be 2-60 chars (letters, digits, space, _ -)")
        if not spec.steps:
            raise WorkflowValidationError("workflow needs at least 1 step")
        if len(spec.steps) > MAX_STEPS:
            raise WorkflowValidationError(f"too many steps (max {MAX_STEPS})")
        known = set(self.orch.toolbox.names())
        for i, step in enumerate(spec.steps):
            if step.tool not in known:
                raise WorkflowValidationError(f"step {i + 1}: unknown tool '{step.tool}'")
            if step.on_error not in ("stop", "continue"):
                raise WorkflowValidationError(f"step {i + 1}: on_error must be stop|continue")

    def create(self, data: dict) -> WorkflowSpec:
        spec = WorkflowSpec.from_dict(data)
        # unique human id from name if not provided
        if not data.get("id"):
            base = re.sub(r"[^a-z0-9]+", "_", spec.name.lower()).strip("_")[:24]
            spec.id = base or uuid.uuid4().hex[:8]
            n = 2
            while spec.id in self._workflows:
                spec.id = f"{base}_{n}"
                n += 1
        self.validate(spec)
        self.conn.execute(
            "INSERT OR REPLACE INTO workflows VALUES (?,?,?,?,?,?,?,?)",
            (
                spec.id,
                spec.name,
                spec.description,
                json.dumps(spec.trigger),
                json.dumps([s.to_dict() for s in spec.steps]),
                1 if spec.enabled else 0,
                spec.created_at,
                0,
            ),
        )
        self.conn.commit()
        self._workflows[spec.id] = spec
        try:
            self._setup_triggers(spec)
        except Exception as exc:
            print(f"[Workflow] trigger setup failed for {spec.name}: {exc}")
        self.events.publish("workflow_created", {"workflow_id": spec.id, "name": spec.name})
        return spec

    # ---------- triggers ----------

    def _persist_trigger(self, spec: WorkflowSpec):
        self.conn.execute(
            "UPDATE workflows SET trigger_json = ? WHERE id = ?",
            (json.dumps(spec.trigger), spec.id),
        )
        self.conn.commit()

    def _setup_triggers(self, spec: WorkflowSpec):
        t = spec.trigger or {}
        ttype = str(t.get("type", "manual")).lower()
        if ttype == "schedule" and getattr(self.orch, "scheduler", None) is not None:
            interval = int(t.get("interval_s") or 3600)
            task_id = self.orch.scheduler.add_task(
                name=f"wf:{spec.name}",
                trigger_type="interval",
                trigger_spec={"seconds": interval},
                action="run_workflow",
                action_args={"workflow_id": spec.id},
            )
            spec.trigger["scheduler_task_id"] = task_id
            self._persist_trigger(spec)
        elif (
            ttype == "cron" and getattr(self.orch, "scheduler", None) is not None and t.get("cron")
        ):
            try:
                task_id = self.orch.scheduler.add_task(
                    name=f"wf:{spec.name}",
                    trigger_type="cron",
                    trigger_spec={"expression": t["cron"]},
                    action="run_workflow",
                    action_args={"workflow_id": spec.id},
                )
                spec.trigger["scheduler_task_id"] = task_id
                self._persist_trigger(spec)
            except Exception as exc:
                print(f"[Workflow] cron trigger failed for {spec.name}: {exc}")
        elif ttype == "event" and t.get("event"):
            etype = str(t["event"])

            async def handler(event, _spec=spec):
                try:
                    # LOOP GUARD: an event-triggered run's own completion must not
                    # re-fire event workflows (prevents infinite self-trigger chains).
                    if event.payload.get("_from_event_trigger"):
                        return
                    # RATE GUARD: max 10 event-triggered runs per workflow per minute
                    now = time.time()
                    recent = [ts for ts in self._event_run_log.get(_spec.id, []) if now - ts < 60]
                    if len(recent) >= 10:
                        print(f"[Workflow] rate guard: {_spec.name} skipped (event storm)")
                        return
                    recent.append(now)
                    self._event_run_log[_spec.id] = recent
                    variables = {
                        "event_type": event.type,
                        "event_payload": json.dumps(event.payload, default=str)[:500],
                    }
                    # Spread event payload fields as top-level workflow variables
                    for k, v in event.payload.items():
                        if isinstance(k, str) and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", k):
                            variables[k] = (
                                v
                                if isinstance(v, (int, float, bool, str))
                                else json.dumps(v, default=str)[:300]
                            )
                    await self.run(_spec.id, variables)
                except Exception as exc:
                    print(f"[Workflow] event trigger failed for {_spec.name}: {exc}")

            self.events.subscribe(etype, handler)
            self._event_subs[spec.id] = (etype, handler)

    def _teardown_triggers(self, spec: WorkflowSpec):
        t = spec.trigger or {}
        sid = t.get("scheduler_task_id")
        if sid and getattr(self.orch, "scheduler", None) is not None:
            try:
                self.orch.scheduler.remove_task(sid)
            except Exception as exc:
                # Loud: an orphaned scheduler task keeps firing a deleted workflow.
                print(f"[Workflow] failed to remove scheduler task {sid} for {spec.name}: {exc}")
        sub = self._event_subs.pop(spec.id, None)
        if sub:
            try:
                self.events.unsubscribe(sub[0], sub[1])
            except Exception as exc:
                print(f"[Workflow] failed to unsubscribe event trigger for {spec.name}: {exc}")

    def get(self, id_or_name: str) -> WorkflowSpec | None:
        if id_or_name in self._workflows:
            return self._workflows[id_or_name]
        for spec in self._workflows.values():
            if spec.name.lower() == id_or_name.lower():
                return spec
        return None

    def list(self) -> list[WorkflowSpec]:
        return sorted(self._workflows.values(), key=lambda s: s.name.lower())

    def delete(self, wf_id: str) -> bool:
        if wf_id not in self._workflows:
            return False
        spec = self._workflows[wf_id]
        self._teardown_triggers(spec)
        self.conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
        self.conn.commit()
        del self._workflows[wf_id]
        return True

    def set_enabled(self, wf_id: str, enabled: bool) -> bool:
        spec = self._workflows.get(wf_id)
        if not spec:
            return False
        spec.enabled = enabled
        self.conn.execute(
            "UPDATE workflows SET enabled = ? WHERE id = ?", (1 if enabled else 0, wf_id)
        )
        self.conn.commit()
        return True

    # ---------- variable machinery ----------

    VAR_RE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")

    def _interpolate(self, args: dict, variables: dict) -> dict:
        out = {}
        for k, v in args.items():
            if isinstance(v, str):

                def repl(m):
                    return str(variables.get(m.group(1), m.group(0)))

                out[k] = self.VAR_RE.sub(repl, v)
            else:
                out[k] = v
        return out

    @staticmethod
    def _eval_condition(condition: str, variables: dict) -> bool:
        """Safely evaluate a condition expression over workflow variables."""
        safe_vars = {
            k: (v if isinstance(v, (int, float, bool, str, list, dict, type(None))) else str(v))
            for k, v in variables.items()
        }
        return eval_gate(condition, safe_vars)

    # ---------- execution ----------

    async def run(
        self, id_or_name: str, initial_vars: dict | None = None, background: bool = True
    ) -> str:
        spec = self.get(id_or_name)
        if not spec:
            raise WorkflowValidationError(f"workflow not found: {id_or_name}")
        if not spec.enabled:
            raise WorkflowValidationError(f"workflow '{spec.name}' is disabled")

        run_id = uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO workflow_runs (id, workflow_id, workflow_name, status, started_at, vars_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                run_id,
                spec.id,
                spec.name,
                "running",
                time.time(),
                json.dumps(initial_vars or {}, default=str),
            ),
        )
        self.conn.execute("UPDATE workflows SET run_count = run_count + 1 WHERE id = ?", (spec.id,))
        self.conn.commit()

        coro = self._execute(spec, run_id, initial_vars or {})
        if background:
            task = asyncio.create_task(coro)
            self._running_tasks[run_id] = task
            task.add_done_callback(lambda t: self._running_tasks.pop(run_id, None))
        else:
            await coro
        return run_id

    async def _execute(self, spec: WorkflowSpec, run_id: str, variables: dict):
        self.events.publish("workflow_started", {"run_id": run_id, "workflow": spec.name})
        status = "done"
        error = ""
        summary_parts = []
        try:
            for i, step in enumerate(spec.steps):
                # condition gate
                if not self._eval_condition(step.condition, variables):
                    self._record_step(run_id, i, step.tool, "{}", "skipped", "condition false")
                    continue

                # foreach expansion
                if step.foreach:
                    items = variables.get(step.foreach)
                    if not isinstance(items, list):
                        items = [items] if items is not None else []
                    for n, item in enumerate(items):
                        loop_vars = dict(variables)
                        loop_vars["item"] = item
                        loop_vars["item_index"] = n
                        rec = await self._run_step(step, loop_vars)
                        self._record_step(
                            run_id,
                            i,
                            step.tool,
                            rec.args_json,
                            rec.status,
                            rec.output,
                            rec.error,
                            rec.duration_ms,
                            loop_index=n,
                        )
                        if rec.status == "error" and step.on_error == "stop":
                            raise RuntimeError(f"step {i + 1} ({step.tool}) item {n}: {rec.error}")
                        if step.save_as:
                            variables[step.save_as] = rec.output
                else:
                    rec = await self._run_step(step, variables)
                    self._record_step(
                        run_id,
                        i,
                        step.tool,
                        rec.args_json,
                        rec.status,
                        rec.output,
                        rec.error,
                        rec.duration_ms,
                    )
                    if rec.status == "error" and step.on_error == "stop":
                        raise RuntimeError(f"step {i + 1} ({step.tool}): {rec.error}")
                    if step.save_as:
                        variables[step.save_as] = rec.output
                    if rec.status == "ok":
                        summary_parts.append(f"{step.tool}: ok")

        except Exception as exc:
            status = "failed"
            error = str(exc)

        finished = time.time()
        result_summary = (
            error
            if error
            else f"{len(summary_parts)} steps ok; vars: "
            + ", ".join(f"{k}={str(v)[:40]}" for k, v in list(variables.items())[:6])
        )
        try:
            self.conn.execute(
                "UPDATE workflow_runs SET status=?, finished_at=?, duration_s=?, error=?, result=?, vars_json=? WHERE id=?",
                (
                    status,
                    finished,
                    finished - _start_of(self.conn, run_id),
                    error,
                    result_summary[:2000],
                    json.dumps(variables, default=str)[:4000],
                    run_id,
                ),
            )
            self.conn.commit()
        except Exception as exc:
            # Without this guard a failed final UPDATE killed background tasks
            # silently and left the run wedged as "running" forever.
            print(f"[Workflow] final run update failed for run {run_id} ({spec.name}): {exc}")
        # Mark event-triggered runs so their own finish can't re-fire event workflows
        finished_payload = {
            "run_id": run_id,
            "workflow": spec.name,
            "status": status,
            "error": error,
        }
        if variables.get("event_type"):
            finished_payload["_from_event_trigger"] = True
        self.events.publish("workflow_finished", finished_payload)
        if status == "failed":
            print(f"[Workflow] '{spec.name}' run {run_id} FAILED: {error}")
        else:
            print(f"[Workflow] '{spec.name}' run {run_id} done ({result_summary[:80]})")

    async def _run_step(self, step: WorkflowStep, variables: dict) -> StepRecord:
        args = self._interpolate(step.args, variables)
        args_json = json.dumps(args, default=str)
        started = time.time()
        attempts = step.retry + 1
        last_err = ""
        for attempt in range(attempts):
            try:
                output = await asyncio.wait_for(
                    self.orch.toolbox.execute(step.tool, args_json),
                    timeout=step.timeout_s,
                )
                # ToolBox reports failures as "error: ..." strings, not
                # exceptions - flag them as failures (same contract as
                # tool_use_v2.execute_parallel) instead of recording "ok".
                if isinstance(output, str) and output.startswith("error:"):
                    last_err = output
                else:
                    dur = int((time.time() - started) * 1000)
                    return StepRecord(0, step.tool, args_json, "ok", output=output, duration_ms=dur)
            except asyncio.TimeoutError:
                last_err = f"timeout after {step.timeout_s}s"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
        dur = int((time.time() - started) * 1000)
        return StepRecord(0, step.tool, args_json, "error", error=last_err, duration_ms=dur)

    def _record_step(
        self,
        run_id: str,
        idx: int,
        tool: str,
        args_json: str,
        status: str,
        output: str = "",
        error: str = "",
        duration_ms: int = 0,
        loop_index: int = -1,
    ):
        # Audit write must not change the run's outcome: log loudly, keep going.
        try:
            self.conn.execute(
                "INSERT INTO workflow_run_steps VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    idx,
                    tool,
                    args_json[:1000],
                    status,
                    (output or "")[:1500],
                    (error or "")[:500],
                    duration_ms,
                ),
            )
            self.conn.commit()
        except Exception as exc:
            print(f"[Workflow] step audit write failed for run {run_id} step {idx} ({tool}): {exc}")

    # ---------- introspection ----------

    def run_history(self, limit: int = 15, workflow_id: str = "") -> list[dict]:
        if workflow_id:
            rows = self.conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? ORDER BY started_at DESC LIMIT ?",
                (workflow_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def run_detail(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        steps = [
            dict(s)
            for s in self.conn.execute(
                "SELECT * FROM workflow_run_steps WHERE run_id = ? ORDER BY idx, duration_ms",
                (run_id,),
            ).fetchall()
        ]
        d = dict(row)
        d["steps"] = steps
        return d

    def stats(self) -> dict:
        wf = self.conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        runs = self.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        failed = self.conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE status='failed'"
        ).fetchone()[0]
        return {
            "workflows": wf,
            "runs": runs,
            "failed_runs": failed,
            "active_executions": len(self._running_tasks),
        }

    async def stop_all(self):
        for task in list(self._running_tasks.values()):
            task.cancel()


def _start_of(conn: sqlite3.Connection, run_id: str) -> float:
    row = conn.execute("SELECT started_at FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    return row[0] if row else time.time()

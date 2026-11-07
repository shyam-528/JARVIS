"""Multi-agent pipelines: each agent's output flows into the next, with an
auto-review hook that gives every substantial 'coder' output a 'code_reviewer'
pass.

Failure policy: pipeline step failures are recorded on the PipelineRun
(status="failed", errors list) and surfaced via the "pipeline_finished" event —
that is the contract and is unchanged. Best-effort side effects (auto-review
passes, the on_complete callback, bg-note filing) must degrade gracefully but
NEVER silently: every swallowed failure prints a loud [AgentTeam] diagnostic
with run/task context.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field

from .agent_pool import AgentPool, BackgroundTask
from .safe_expr import eval_gate


@dataclass
class PipelineStep:
    agent: str
    instruction: str  # how this agent treats $input; "" = pass through
    save_as: str = ""  # not needed; output auto-flows to next agent
    gate: str = ""  # optional condition expression over $vars


@dataclass
class PipelineSpec:
    name: str
    description: str
    steps: list[PipelineStep] = field(default_factory=list)
    enabled: bool = True


@dataclass
class PipelineRun:
    run_id: str
    pipeline: str
    input_text: str
    status: str = "queued"  # queued|running|done|failed
    outputs: dict[str, str] = field(default_factory=dict)  # agent -> output
    errors: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    _task: asyncio.Task | None = None

    @property
    def duration_s(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.created_at)


# ---- built-in pipelines: the collaboration recipes ----

SEED_PIPELINES: dict[str, PipelineSpec] = {
    "code_pipeline": PipelineSpec(
        name="code_pipeline",
        description="Write code, then auto-review it, then get a one-line verdict",
        steps=[
            PipelineStep(agent="coder", instruction="Implement this fully and cleanly:\n\n$input"),
            PipelineStep(
                agent="code_reviewer",
                instruction="Review the following code for bugs, security issues, and style. List findings, then verdict APPROVE or REVISE:\n\n$input",
            ),
            PipelineStep(
                agent="fast", instruction="One line: ship or fix? Based on this review: $input"
            ),
        ],
    ),
    "code_pipeline_debug": PipelineSpec(
        name="code_pipeline_debug",
        description="Write code, review it, then auto-fix what the reviewer flagged",
        steps=[
            PipelineStep(agent="coder", instruction="Implement this fully and cleanly:\n\n$input"),
            PipelineStep(
                agent="code_reviewer",
                instruction="Review this code. List concrete bugs/issues only, no praise:\n\n$input",
            ),
            PipelineStep(
                agent="coder",
                instruction="Fix every issue the reviewer found and return the final corrected code. Review notes:\n$input_prev_review\n\nOriginal code:\n$input",
                gate="$review_flag == 'issues'",
            ),
        ],
    ),
    "research_report": PipelineSpec(
        name="research_report",
        description="Research a topic, then have the analyst turn findings into an executive brief",
        steps=[
            PipelineStep(
                agent="researcher", instruction="Research this thoroughly with sources:\n\n$input"
            ),
            PipelineStep(
                agent="analyst",
                instruction="Turn these research findings into a 5-bullet executive brief with the single most actionable takeaway:\n\n$input",
            ),
        ],
    ),
    "design_review": PipelineSpec(
        name="design_review",
        description="Build a UI concept, then critique it like a design lead",
        steps=[
            PipelineStep(agent="writer", instruction="Design a clean UI concept for:\n\n$input"),
            PipelineStep(
                agent="designer",
                instruction="Critique this UI concept like a senior design lead: 3 strengths, 3 fixes:\n\n$input",
            ),
        ],
    ),
    "doc_pipeline": PipelineSpec(
        name="doc_pipeline",
        description="Have code analyzed then documented",
        steps=[
            PipelineStep(
                agent="coder",
                instruction="Summarize what this code/project does, its structure, and public interface:\n\n$input",
            ),
            PipelineStep(
                agent="docwriter",
                instruction="Write a polished README section from this analysis. Use markdown, install/usage examples:\n\n$input",
            ),
        ],
    ),
}


class AgentTeam:
    """Agent collaboration: multi-agent pipelines where each agent's output
    flows into the next. Includes an auto-review hook so any 'coder' output
    gets a 'code_reviewer' pass automatically (the user's core example).

    Agents share information through:
    - chained $input (previous agent's output)
    - named $variables (e.g. $input_prev_review) for multi-reference steps
    - the orchestrator's shared memory + event bus
    """

    AUTO_REVIEW_ON_CODE = True
    AUTO_REVIEW_MIN_LEN = 80  # only review substantial code

    def __init__(self, pool: AgentPool, orchestrator):
        self.pool = pool
        self.orch = orchestrator
        self.pipelines: dict[str, PipelineSpec] = dict(SEED_PIPELINES)
        self.runs: dict[str, PipelineRun] = {}
        self.on_complete = None
        self.auto_review_enabled = True

    # ---------- pipeline management ----------

    def create_pipeline(self, name: str, description: str, steps: list[dict]) -> PipelineSpec:
        if not name or not isinstance(name, str):
            raise ValueError("name required")
        parsed = []
        for i, s in enumerate(steps or []):
            agent = s.get("agent", "")
            if agent not in self.pool.agents:
                raise ValueError(
                    f"step {i + 1}: unknown agent '{agent}' (create it first with /agent new)"
                )
            parsed.append(
                PipelineStep(
                    agent=agent,
                    instruction=str(s.get("instruction", "")),
                    gate=str(s.get("gate", "")),
                )
            )
        if not parsed:
            raise ValueError("pipeline needs at least 1 step")
        spec = PipelineSpec(name=name, description=description or name, steps=parsed)
        self.pipelines[name] = spec
        return spec

    def delete_pipeline(self, name: str) -> bool:
        return self.pipelines.pop(name, None) is not None

    # ---------- execution ----------

    def start(self, pipeline_name: str, input_text: str) -> PipelineRun:
        spec = self.pipelines.get(pipeline_name)
        if not spec:
            raise ValueError(f"unknown pipeline '{pipeline_name}'")
        run = PipelineRun(
            run_id=f"pipe-{uuid.uuid4().hex[:8]}",
            pipeline=pipeline_name,
            input_text=input_text,
        )
        self.runs[run.run_id] = run
        run._task = asyncio.create_task(self._execute(spec, run))
        return run

    async def _execute(self, spec: PipelineSpec, run: PipelineRun):
        run.status = "running"
        current_input = run.input_text
        variables: dict[str, str] = {"input": run.input_text}
        prev_agent = ""
        try:
            for i, step in enumerate(spec.steps):
                # gate check
                if step.gate and not self._eval_gate(step.gate, variables):
                    run.outputs[step.agent + f"_{i}"] = "(skipped by gate)"
                    continue

                # Build the prompt: $input = PREVIOUS agent's output (flow),
                # named $vars = registered references. Interpolate named refs
                # FIRST (excluding 'input'), then substitute $input manually so
                # it always carries the previous agent's output — not the
                # original task text.
                if prev_agent:
                    variables[f"input_prev_{prev_agent}"] = current_input
                step_vars = {k: v for k, v in variables.items() if k != "input"}
                prompt = self._interpolate(step.instruction or "$input", step_vars)
                # Substitute $input last, with a word boundary so we don't
                # mangle $input_prev_* tokens (naive str.replace would).
                prompt = re.sub(
                    r"\$input(?![A-Za-z0-9_])", lambda _m, _ci=current_input: _ci, prompt
                )

                output_parts: list[str] = []
                async for provider, chunk in self.pool.run(step.agent, prompt):
                    if provider == "error":
                        run.errors.append(f"step {i + 1} ({step.agent}): {chunk}")
                        break
                    if provider != "tool":
                        output_parts.append(str(chunk))
                output = "".join(output_parts).strip()

                if run.errors and not output:
                    run.status = "failed"
                    run.finished_at = time.time()
                    await self._notify(run)
                    return

                run.outputs[f"{i + 1}:{step.agent}"] = output
                variables[f"agent_{step.agent}"] = output
                current_input = output
                prev_agent = step.agent

                # AUTO-REVIEW HOOK: coder output inside a pipeline gets an
                # automatic code_reviewer pass (unless the next step IS a review)
                if (
                    self.auto_review_enabled
                    and step.agent == "coder"
                    and len(output) >= self.AUTO_REVIEW_MIN_LEN
                    and self.AUTO_REVIEW_ON_CODE
                    and "review" not in (spec.steps[i + 1].agent if i + 1 < len(spec.steps) else "")
                ):
                    review = await self._auto_review(output)
                    if review:
                        run.outputs[f"{i + 1}:code_reviewer(auto)"] = review
                        variables["review_flag"] = (
                            "issues" if "REVISE" in review.upper() else "clean"
                        )
                        variables["auto_review"] = review

            run.status = "done"
        except Exception as exc:
            # fail-soft: unexpected step errors are recorded on the run and
            # surfaced via the pipeline_finished event (status="failed")
            run.errors.append(f"{type(exc).__name__}: {exc}")
            run.status = "failed"
        run.finished_at = time.time()
        await self._notify(run)

    async def _auto_review(self, code_output: str) -> str:
        """Auto-review agent output that looks like code."""
        reviewer = "code_reviewer" if "code_reviewer" in self.pool.agents else None
        if reviewer is None:
            return ""
        parts: list[str] = []
        try:
            async for provider, chunk in self.pool.run(
                reviewer,
                "Review this code for bugs, security, and style. Findings then verdict APPROVE or REVISE:\n\n"
                + code_output[:6000],
            ):
                if provider == "error":
                    print(f"[AgentTeam] auto-review by '{reviewer}' failed: {chunk}")
                    return ""
                if provider != "tool":
                    parts.append(str(chunk))
        except Exception as exc:
            print(f"[AgentTeam] auto-review by '{reviewer}' raised {type(exc).__name__}: {exc}")
            return ""
        return "".join(parts).strip()

    # ---------- helpers ----------

    @staticmethod
    def _interpolate(text: str, variables: dict) -> str:
        def repl(m):
            return variables.get(m.group(1), m.group(0))

        return re.sub(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", repl, text)

    @staticmethod
    def _eval_gate(gate: str, variables: dict) -> bool:
        safe = {k: v for k, v in variables.items() if isinstance(v, (str, int, float, bool))}
        return eval_gate(gate, safe)

    async def _notify(self, run: PipelineRun):
        self.orch.event_bus.publish(
            "pipeline_finished",
            {
                "run_id": run.run_id,
                "pipeline": run.pipeline,
                "status": run.status,
                "steps": len(run.outputs),
            },
        )
        if self.on_complete:
            try:
                await self.on_complete(run)
            except Exception as exc:
                print(
                    f"[AgentTeam] on_complete callback for run {run.run_id} "
                    f"({run.pipeline}) raised {type(exc).__name__}: {exc}"
                )

    # ---------- standalone auto-review for ANY coder background job ----------

    async def maybe_auto_review(self, task: BackgroundTask):
        """Called when a background agent job finishes: if a coder wrote
        substantial code, dispatch a code_reviewer pass on it automatically.
        This is the user's core ask: coder codes -> debugger/reviewer reviews."""
        if not self.auto_review_enabled:
            return
        if task.agent != "coder" or task.status != "done":
            return
        if "code_reviewer" not in self.pool.agents:
            return
        result = task.result or ""
        # looks like code? (has a def/class/function block)
        if len(result) < self.AUTO_REVIEW_MIN_LEN:
            return
        if not any(marker in result for marker in ("def ", "class ", "function ", "=>", "```")):
            return
        review = await self._auto_review(result)
        if not review:
            return
        task.result = result + "\n\n---\n**AUTO-REVIEW (code_reviewer agent):**\n" + review[:2000]
        # Single-delivery: the review travels INSIDE task.result (shown once
        # via the normal job channel). No separate note — filing one more
        # would surface the same review twice next turn.

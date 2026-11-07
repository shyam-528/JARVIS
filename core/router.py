"""Failover router — the single point every conversation flows through.

Owns: provider selection and ordering (task tags, stickiness, rate penalties,
budget pressure, learned optimizer scores), circuit breakers per provider,
per-provider concurrency gates, the token usage ledger (SQLite), mid-stream
failover with continuation nudges, and restart-safe router state
(router_state.json: breakers + learned TPM limits).

Rules:
- Observability must never break routing. Persist hooks, optimizer rewards,
  and on_event callbacks are wrapped in fail-soft try/except BY DESIGN — a
  telemetry bug must not kill a reply. Do not "fix" those swallows.
- A provider is tried at most once per turn (sticky same-brain 429 retry
  excepted) and the hop count is capped, so a degraded evening fails fast
  with AllProvidersDown instead of churning through the whole chain.
- This module never touches conversation content beyond token estimates and
  the ContextTooLong slimming fallback; memory/prompt assembly is the
  orchestrator's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time

from .db import connect as _db_connect
from .providers import (
    HEARTBEAT,
    AuthFailed,
    ContextTooLong,
    PermanentError,
    ProviderError,
    RateLimited,
    StreamDied,
    ToolCallDelta,
    ToolsRejected,
)

TASK_TAGS = {
    "coding": ["coding", "primary"],
    "reasoning": ["reasoning", "primary", "thinking"],
    "vision": ["vision", "multimodal", "primary"],
    "fast": ["fast", "fastest", "lightweight"],
    "general": ["general", "primary"],
    "multilingual": ["multilingual"],
    "fallback": ["fallback", "lightweight"],
}

DEFAULT_TASK = "general"


class AllProvidersDown(Exception):
    pass


def estimate_tokens(text: str | ToolCallDelta) -> int:
    if isinstance(text, ToolCallDelta):
        return max(1, (len(text.args) + 3) // 4)
    return max(1, (len(text) + 3) // 4)


class UsageLedger:
    def __init__(self, db_path):
        from pathlib import Path as _P

        self.db_path = _P(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (day TEXT NOT NULL, provider TEXT NOT NULL, tokens INTEGER NOT NULL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_day_provider ON usage (day, provider)"
        )
        self.conn.commit()
        # in-memory rolling latency stats: provider -> list of (first_token_ms, total_ms)
        self.latency: dict[str, list] = {}
        self._MAX_LAT = 50
        # sliding 60s token window per provider (TPM guard vs 429 churn)
        self._tpm: dict[str, list] = {}  # provider -> [(ts, tokens), ...]
        self.TPM_LIMIT: dict[str, int] = {}  # provider -> learned limit (default heuristics)
        self._TPM_WINDOW_S = 60.0

    def tpm_estimate(self, provider: str) -> int:
        """Tokens this provider consumed in the last 60s."""
        now = time.time()
        bucket = self._tpm.get(provider)
        if not bucket:
            return 0
        recent = [(ts, tok) for ts, tok in bucket if now - ts < self._TPM_WINDOW_S]
        self._tpm[provider] = recent
        return sum(tok for _, tok in recent)

    def record_latency(self, provider: str, first_token_ms: float, total_ms: float) -> None:
        bucket = self.latency.setdefault(provider, [])
        bucket.append((first_token_ms, total_ms))
        if len(bucket) > self._MAX_LAT:
            del bucket[: -self._MAX_LAT]

    def learn_tpm_limit(self, provider: str, limit: int) -> None:
        """Remember a provider's real TPM from its 429 messages (e.g. Groq 8000)."""
        self.TPM_LIMIT[provider] = limit

    def latency_stats(self) -> dict[str, dict]:
        """Per-provider rolling latency: avg/p95 first-token + avg total (last N calls)."""
        out = {}
        for provider, samples in self.latency.items():
            if not samples:
                continue
            firsts = sorted(s[0] for s in samples)
            totals = [s[1] for s in samples]
            n = len(firsts)
            p95 = firsts[max(0, int(n * 0.95) - 1)]
            out[provider] = {
                "calls": n,
                "first_token_avg_ms": round(sum(firsts) / n),
                "first_token_p95_ms": round(p95),
                "total_avg_ms": round(sum(totals) / n),
            }
        return out

    def add(self, provider: str, tokens: int) -> None:
        day = time.strftime("%Y-%m-%d")
        self.conn.execute("INSERT INTO usage VALUES (?, ?, ?)", (day, provider, int(tokens)))
        self.conn.commit()
        # sliding TPM window
        self._tpm.setdefault(provider, []).append((time.time(), int(tokens)))

    def add_tpm(self, provider: str, tokens: int) -> None:
        """Record token consumption toward the TPM window without a DB write."""
        self._tpm.setdefault(provider, []).append((time.time(), int(tokens)))

    def today(self, provider: str) -> int:
        day = time.strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE day = ? AND provider = ?",
            (day, provider),
        ).fetchone()
        return int(row[0])

    def summary(self) -> list[tuple[str, int]]:
        day = time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT provider, SUM(tokens) FROM usage WHERE day = ? GROUP BY provider ORDER BY 2 DESC",
            (day,),
        ).fetchall()
        return [(str(r[0]), int(r[1])) for r in rows]


class BreakerState:
    __slots__ = ("failures", "open_until", "kind")

    def __init__(self) -> None:
        self.failures: int = 0
        self.open_until: float = 0.0
        self.kind: str = ""

    def allows(self) -> bool:
        return time.monotonic() >= self.open_until

    def seconds_until_open_ends(self) -> float:
        return max(0.0, self.open_until - time.monotonic())


class CircuitBreakers:
    AUTH_COOLDOWN_S = 1800.0
    CTX_COOLDOWN_S = 120.0
    NET_BASE_COOLDOWN_S = 5.0
    SLOW_BASE_COOLDOWN_S = 45.0
    MAX_COOLDOWN_S = 600.0
    PERM_COOLDOWN_S = 86400.0  # dead model / wrong endpoint: stay out 24h

    def __init__(self) -> None:
        self._states = {}
        self.on_change = None  # FailoverRouter sets persist hook

    def _changed(self) -> None:
        try:
            if callable(self.on_change):
                self.on_change()
        except Exception:
            pass

    def get(self, name: str):
        if name not in self._states:
            self._states[name] = BreakerState()
        return self._states[name]

    def record_failure(self, provider, err):
        st = self.get(provider.name)
        st.failures += 1
        if isinstance(err, PermanentError):
            st.kind = "dead"
            st.open_until = time.monotonic() + self.PERM_COOLDOWN_S
        elif isinstance(err, AuthFailed):
            st.kind = "auth"
            st.open_until = time.monotonic() + self.AUTH_COOLDOWN_S
        elif isinstance(err, RateLimited):
            st.kind = "rate"
            retry_after = getattr(err, "retry_after", None)
            cooldown = (
                retry_after
                if retry_after
                else min(60.0 * (2 ** max(0, st.failures - 1)), self.MAX_COOLDOWN_S)
            )
            st.open_until = time.monotonic() + cooldown
        elif isinstance(err, ContextTooLong):
            st.kind = "ctx"
            st.open_until = time.monotonic() + self.CTX_COOLDOWN_S
        elif isinstance(err, asyncio.TimeoutError):
            st.kind = "slow"
            st.open_until = time.monotonic() + min(
                self.SLOW_BASE_COOLDOWN_S * st.failures, self.MAX_COOLDOWN_S
            )
        else:
            st.kind = "net"
            st.open_until = time.monotonic() + min(
                self.NET_BASE_COOLDOWN_S * st.failures, self.MAX_COOLDOWN_S
            )
        self._changed()

    def record_success(self, name):
        st = self.get(name)
        st.failures = 0
        st.open_until = 0.0
        st.kind = ""
        self._changed()

    def describe(self, name):
        st = self.get(name)
        if st.allows():
            return ("ok" if st.kind == "" or st.failures == 0 else f"ok({st.kind})", 0.0)
        return (st.kind, st.seconds_until_open_ends())

    def export(self) -> dict:
        """Breaker states as remaining-seconds (restart-safe)."""
        now = time.monotonic()
        return {
            name: {
                "failures": st.failures,
                "kind": st.kind,
                "remaining": max(0.0, st.open_until - now),
            }
            for name, st in self._states.items()
            if st.failures
        }

    def import_state(self, data: dict) -> None:
        now = time.monotonic()
        for name, entry in (data or {}).items():
            try:
                st = self.get(str(name))
                st.failures = int(entry.get("failures", 1))
                st.kind = str(entry.get("kind", ""))
                st.open_until = now + max(0.0, float(entry.get("remaining", 0)))
            except Exception:
                continue


CONTINUE_NUDGE = (
    "Your previous reply was cut off mid-sentence due to a technical handoff. "
    "Continue it seamlessly from exactly where it stopped. Do not repeat any earlier text."
)


class FailoverRouter:
    def __init__(
        self, providers, client, ledger, watchdog_s=1.5, context_overflow_hook=None, on_event=None
    ):
        self.providers = sorted(providers, key=lambda p: p.priority)
        self.client = client
        self.ledger = ledger
        self.watchdog_s = watchdog_s
        self.context_overflow_hook = context_overflow_hook
        self.on_event = on_event
        self.breakers = CircuitBreakers()
        self._state_path = None
        try:
            from pathlib import Path as _P

            self._state_path = _P(self.ledger.db_path).parent / "router_state.json"
        except Exception:
            pass
        self._load_router_state()
        self.breakers.on_change = self._persist_router_state
        self.max_hops = 10  # was len*2 (~46): fail fast with a clear message instead
        self.last_provider: str | None = None
        self.hop_count = 0
        self.total_hops = 0
        # Sticky same-brain retry: a short "try again in Ns" 429 is a
        # transient blip, not exhaustion — waiting keeps one voice instead
        # of hopping to a differently-thinking model. Upper bound so a
        # "retry in 5 min" still fails over immediately.
        self.sticky_retry_max_s = 25.0
        self._sticky_retried = False
        self.preferred_names: list[str] = []
        self._gates: dict[str, asyncio.Semaphore] = {}  # per-provider concurrency limits
        # Anti-hop: remember who answered last and recent rate-failures
        self._sticky_until: dict[str, float] = {}  # provider -> monotonic time it stays preferred
        self._rate_penalty: dict[str, float] = {}  # provider -> penalty-until (monotonic)
        # P6: routing optimizer (learning); attached by orchestrator after init
        self.optimizer = None
        self._active_task_type: str = DEFAULT_TASK
        self._attempt_failed_providers: list[str] = []

    def set_preferred_names(self, names: list[str] | None) -> None:
        """Set provider names to boost for the next chat call (domain-aware routing)."""
        self.preferred_names = list(names or [])

    def available(self):
        out = []
        for p in self.providers:
            if not getattr(p, "supports_chat", True):
                continue
            if not self.breakers.get(p.name).allows():
                continue
            if self.ledger.today(p.name) >= p.daily_token_budget:
                continue
            # TPM guard: skip providers over their learned per-minute limit
            limit = self.ledger.TPM_LIMIT.get(p.name)
            if limit and self.ledger.tpm_estimate(p.name) >= int(limit * 0.9):
                continue
            out.append(p)
        return out

    def _task_score(self, provider, task_type):
        tags = getattr(provider, "tags", [])
        if not tags:
            return 10
        task_prefs = TASK_TAGS.get(task_type, TASK_TAGS[DEFAULT_TASK])
        for i, pref in enumerate(task_prefs):
            if pref in tags:
                return i
        return 10

    @staticmethod
    def _canonical_name(name: str) -> str:
        """Strip multi-key variant suffix: 'groq-gpt-oss-120b-k2' -> 'groq-gpt-oss-120b'."""
        return re.sub(r"-k\d+$", "", name)

    def _matches_preferred(self, name: str) -> bool:
        """True if provider name (or its base name without -kN suffix) is in preferred_names."""
        if not self.preferred_names:
            return False
        return name in self.preferred_names or self._canonical_name(name) in self.preferred_names

    async def chat(self, messages, tools=None, task_type=DEFAULT_TASK, only=None):
        self._active_task_type = task_type
        self._attempt_failed_providers = []
        self._tried_this_turn: set[str] = set()  # never retry same provider twice in one turn
        self._sticky_retried = False  # one sticky same-brain wait per turn, max
        self._tools_stripped = False
        self.hop_count = 0  # hops within THIS chat call
        candidates = self.available()
        if only:
            allowed = set(only)
            pinned = [
                p
                for p in candidates
                if p.name in allowed or self._canonical_name(p.name) in allowed
            ]
            if pinned:
                candidates = pinned
            # else: do NOT die — fall back to the full chain so agent tasks
            # still complete when all their pinned providers are cooling down.
        if not candidates:
            raise AllProvidersDown(
                "no brain available right now: all providers cooling down or over budget"
            )
        if tools:
            tool_candidates = [p for p in candidates if p.supports_tools]
            if tool_candidates:
                candidates = tool_candidates

        def provider_score(p):
            now = time.monotonic()
            # Sticky: the provider that JUST answered successfully stays first for
            # 90s — prevents key1->key2->key1 flapping between turns.
            sticky = (
                0
                if (
                    self.last_provider
                    and p.name == self.last_provider
                    and self._sticky_until.get(p.name, 0) > now
                )
                else 1
            )
            name_boost = 0 if self._matches_preferred(p.name) else 1
            task_score = self._task_score(p, task_type)
            preferred_score = 0 if p.preferred else 1
            tool_score = 0 if (not tools or p.supports_tools) else 1
            total = p._success_count + p._failure_count
            health_score = p._success_count / total if total > 0 else 0.5
            health_score = -health_score
            # Rate penalty: a provider that 429'd recently goes behind healthy
            # peers even after its breaker reopens (stops churn).
            penalty = 1 if self._rate_penalty.get(p.name, 0) > now else 0
            # Budget pressure: shed load off nearly-exhausted tiers (<25%
            # left) so one tier doesn't burn out mid-evening.
            try:
                from .budget_guard import tier_of, tier_pressure

                pressure = tier_pressure(self.providers, self.ledger, tier_of(p.name))
            except Exception:
                pressure = 0.0
            # P6: learned score from real outcomes (lower sort value = tried
            # earlier). Optimizer scores sit in [0,1]; invert so better=first.
            opt = self.optimizer.score(task_type, p.name) if self.optimizer else 0.5
            opt_sort = round(1.0 - opt, 3)
            priority_score = p.priority
            return (
                name_boost,
                sticky,
                penalty,
                pressure,
                opt_sort,
                task_score,
                preferred_score,
                tool_score,
                health_score,
                priority_score,
            )

        candidates = sorted(candidates, key=provider_score)

        # Per-provider concurrency gate inside _attempt (waits at each hop)
        async for item in self._attempt(candidates, [dict(m) for m in messages], "", tools, 0):
            yield item

    def _persist_router_state(self) -> None:
        """Save breakers + learned TPM limits (restart-proof memory)."""
        if not self._state_path:
            return
        try:
            import json as _json

            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                _json.dumps(
                    {
                        "breakers": self.breakers.export(),
                        "tpm_limits": dict(getattr(self.ledger, "TPM_LIMIT", {})),
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_router_state(self) -> None:
        if not self._state_path:
            return
        # P0: shared corrupt-file contract — a corrupt router snapshot
        # quarantines aside (loud, once) and breakers start fresh instead
        # of silently forgetting which providers were dead.
        from utils.json_utils import load_json_store as _load_store

        try:
            if not self._state_path.exists():
                return
            data = _load_store(self._state_path, default={}, owner="router", expect=dict)
            self.breakers.import_state(data.get("breakers", {}))
            for name, limit in (data.get("tpm_limits", {}) or {}).items():
                with contextlib.suppress(Exception):
                    self.ledger.TPM_LIMIT[str(name)] = int(limit)
        except Exception:
            pass

    def _pick_gate(self, provider_name: str):
        gate = self._gates.get(provider_name)
        if gate is None:
            gate = asyncio.Semaphore(2)
            self._gates[provider_name] = gate
        return gate

    @staticmethod
    def _messages_for(messages, partial):
        if not partial:
            return messages
        out = [dict(m) for m in messages]
        out.append({"role": "assistant", "content": partial})
        out.append({"role": "user", "content": CONTINUE_NUDGE})
        return out

    def _wrap_react_prompt(self, messages, tools):
        tool_descriptions = []
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "unknown")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            param_desc = []
            for param_name, param_info in params.get("properties", {}).items():
                param_type = param_info.get("type", "string")
                param_desc.append(
                    "  "
                    + param_name
                    + " ("
                    + param_type
                    + "): "
                    + param_info.get("description", "")
                )
            params_str = "\n".join(param_desc) if param_desc else "  (no parameters)"
            tool_descriptions.append(name + ": " + desc + "\nParameters:\n" + params_str)

        tools_text = "\n\n".join(tool_descriptions) if tool_descriptions else "No tools available."

        system_prompt = (
            "You are a helpful assistant with access to the following tools:\n\n"
            "{tools_text}\n\n"
            "When you need to use a tool, you MUST use the following XML format:\n\n"
            "<function=tool_name>\n"
            "<parameter=arg_name>value\n"
            ""
        )
        return [
            {"role": "system", "content": system_prompt.format(tools_text=tools_text)}
        ] + messages

    async def _attempt(self, candidates, messages, partial, tools, hop=0):
        if not candidates:
            raise AllProvidersDown("every brain failed; chain exhausted")
        provider = candidates[0]
        rest = candidates[1:]
        msgs = self._messages_for(messages, partial)

        stream = self.client.stream_chat(
            base_url=provider.base_url,
            api_key=provider.api_key,
            model=provider.model,
            messages=msgs,
            tools=tools if tools and provider.supports_tools else None,
        )
        buffer = []
        _t_start = time.monotonic()
        _t_first = None
        gate = self._pick_gate(provider.name)
        try:
            timeout = provider.first_token_timeout_s or self.watchdog_s
            first = None
            # Gate around connection + first token: parallel agents queue here
            # instead of hammering the same provider into 429 storms.
            async with gate:
                while True:
                    try:
                        first = await asyncio.wait_for(anext(stream), timeout=timeout)
                    except StopAsyncIteration as exc:
                        raise ProviderError("empty stream (no events)") from exc
                    if first is not HEARTBEAT:
                        break
                _t_first = (time.monotonic() - _t_start) * 1000
            self.breakers.record_success(provider.name)
            self._persist_router_state()
            provider._success_count += 1
            provider._last_success = time.time()
            self.last_provider = provider.name
            # Sticky winner for 90s: next turn reuses this exact provider/key
            self._sticky_until[provider.name] = time.monotonic() + 90.0
            self._rate_penalty.pop(provider.name, None)
            buffer.append(first)
            prompt_est = sum(estimate_tokens(str(m.get("content") or "")) for m in msgs)
            self.ledger.add(provider.name, estimate_tokens(first) + prompt_est)
            yield provider.name, first

            while True:
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                if chunk is HEARTBEAT:
                    continue
                if isinstance(chunk, ToolCallDelta):
                    yield provider.name, chunk
                    continue
                buffer.append(chunk)
                self.ledger.add(provider.name, estimate_tokens(chunk))
                yield provider.name, chunk
            self.ledger.record_latency(
                provider.name, _t_first, (time.monotonic() - _t_start) * 1000
            )
            # P6: reward the winner — success + speed bonus, penalize earlier failures
            if self.optimizer is not None:
                try:
                    self.optimizer.reward(
                        self._active_task_type, provider.name, _t_first, success=True, hopped=False
                    )
                    for failed in self._attempt_failed_providers:
                        self.optimizer.reward(
                            self._active_task_type, failed, _t_first, success=False, hopped=True
                        )
                    self._attempt_failed_providers = []
                except Exception:
                    pass
        except (
            RateLimited,
            AuthFailed,
            ContextTooLong,
            PermanentError,
            ToolsRejected,
            ProviderError,
            StreamDied,
            asyncio.TimeoutError,
        ) as err:
            with contextlib.suppress(Exception):
                await stream.aclose()
            if isinstance(err, ContextTooLong) and self.context_overflow_hook is not None:
                with contextlib.suppress(Exception):
                    self.context_overflow_hook()
            self.breakers.record_failure(provider, err)
            self._persist_router_state()
            provider._failure_count += 1
            provider._last_failure = time.time()
            self.total_hops += 1
            self.hop_count += 1
            hop += 1
            self._attempt_failed_providers.append(provider.name)
            with contextlib.suppress(Exception):
                self._tried_this_turn.add(provider.name)
            # Tools rejected -> retry same turn WITHOUT tools (one shot)
            next_tools = tools
            if (
                isinstance(err, ToolsRejected)
                and tools
                and not getattr(self, "_tools_stripped", False)
            ):
                self._tools_stripped = True
                next_tools = None
            # Context too long -> shrink messages for remaining hops
            next_messages = messages
            if isinstance(err, ContextTooLong):
                try:
                    kept = [messages[0]] if messages and messages[0].get("role") == "system" else []
                    tail = [m for m in messages if m.get("role") != "system"][-4:]
                    # halve long contents
                    slim = []
                    for m in kept + tail:
                        c = dict(m)
                        content = c.get("content") or ""
                        if isinstance(content, str) and len(content) > 4000:
                            c["content"] = content[-4000:]
                        slim.append(c)
                    next_messages = slim
                except Exception:
                    pass
            elif "400" in str(err):
                # Validator rejected the shape (usually poisoned tool_calls
                # from an older round): resend text-only, drop tool pairing.
                try:
                    slim = []
                    for m in messages:
                        if m.get("role") == "tool":
                            continue
                        c = dict(m)
                        c.pop("tool_calls", None)
                        slim.append(c)
                    if slim:
                        next_messages = slim
                except Exception:
                    pass
            # Rate failures earn a 3-min scoring penalty even after the breaker
            # reopens, so the chain stops retrying TPM-exhausted providers.
            if isinstance(err, RateLimited):
                retry_after = getattr(err, "retry_after", None)
                self._rate_penalty[provider.name] = time.monotonic() + max(
                    180.0, float(retry_after or 0)
                )
                # Sticky same-brain retry (once per turn): a SHORT "try again
                # in Ns" is a transient blip. Wait it out and retry THIS
                # provider so the reply keeps one voice instead of hopping
                # to a different model. Long/no waits fail over as before;
                # the breaker above already recorded the failure either way.
                _wait = retry_after or 0
                try:
                    _wait = float(_wait)
                except (TypeError, ValueError):
                    _wait = 0
                if 0 < _wait <= self.sticky_retry_max_s and not self._sticky_retried:
                    self._sticky_retried = True
                    with contextlib.suppress(Exception):
                        self._tried_this_turn.discard(provider.name)
                    if self.on_event is not None:
                        with contextlib.suppress(Exception):
                            self.on_event(
                                provider.name,
                                f"rate limited — holding {_wait:.0f}s for the same brain instead of hopping models",
                            )
                    try:
                        await asyncio.sleep(_wait)
                    except asyncio.CancelledError:
                        raise
                    async for item in self._attempt(
                        [provider] + rest, messages, partial, tools, hop
                    ):
                        yield item
                    return
                # Learn the provider's real TPM limit from its own 429 message
                # (Groq: "TPM): Limit 8000")
                m = re.search(r"Limit (\d+)", str(err))
                if m:
                    try:
                        self.ledger.learn_tpm_limit(provider.name, int(m.group(1)))
                        self._persist_router_state()
                    except Exception:
                        pass
            if self.on_event is not None:
                try:
                    reason = str(err).strip() or err.__class__.__name__
                    self.on_event(provider.name, reason)
                except Exception:
                    pass
            if hop >= self.max_hops:
                tried = sorted(getattr(self, "_tried_this_turn", set()))
                raise AllProvidersDown(
                    f"hop limit ({self.max_hops}) reached after trying: {', '.join(tried) or provider.name}. "
                    f"Last: {provider.name}: {str(err)[:160]}"
                ) from err
            # Never retry a provider already tried this turn (was: mimo 4x)
            rest = [p for p in rest if p.name not in getattr(self, "_tried_this_turn", set())]
            new_partial = "".join(buffer)
            async for item in self._attempt(rest, next_messages, new_partial, next_tools, hop):
                yield item

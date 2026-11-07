from __future__ import annotations

import asyncio

import pytest

from core.config import ProviderConfig
from core.providers import HEARTBEAT, RateLimited, StreamDied, ToolCallDelta
from core.router import AllProvidersDown, FailoverRouter, UsageLedger


def prov(
    name: str, priority: int, model: str | None = None, budget: int = 1_000_000
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.test/v1",
        model=model or name,
        api_key="k",
        priority=priority,
        daily_token_budget=budget,
    )


def sse(*pieces: str):
    async def gen():
        for p in pieces:
            yield p

    return gen()


async def rate_limited_gen():
    raise RateLimited("429")
    yield


async def hanging_gen():
    await asyncio.sleep(60)
    yield ""


async def dying_midway_gen(*prefix: str):
    for p in prefix:
        yield p
    raise StreamDied("connection reset")


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.calls: list[tuple[str, list[dict], list[dict] | None]] = []

    def stream_chat(self, base_url, api_key, model, messages, tools=None):
        self.calls.append((model, [dict(m) for m in messages], tools))
        return self.script(model, messages, tools)


def make_router(tmp_path, providers, client, watchdog_s=0.2):
    ledger = UsageLedger(tmp_path / "usage.db")
    router = FailoverRouter(providers, client, ledger, watchdog_s=watchdog_s)
    return router, ledger


async def collect(router, messages, tools=None):
    out = []
    async for name, chunk in router.chat(messages, tools):
        out.append((name, chunk))
    return out


async def test_hops_on_rate_limit(tmp_path):
    a, b = prov("a", 0), prov("b", 1)
    client = FakeClient(
        lambda model, msgs, tools=None: (
            rate_limited_gen() if model == a.model else sse("Hi", " Sir")
        )
    )
    router, _ = make_router(tmp_path, [a, b], client)

    out = await collect(router, [{"role": "user", "content": "hi"}])

    assert "".join(c for _, c in out) == "Hi Sir"
    assert {n for n, _ in out} == {"b"}
    assert [m for m, _, _ in client.calls] == [a.model, b.model]
    assert router.last_provider == "b"
    assert router.hop_count == 1


async def test_watchdog_skips_hanging_brain(tmp_path):
    slow, fast = prov("slow", 0), prov("fast", 1)

    def script(model, msgs, tools=None):
        if model == slow.model:
            return hanging_gen()
        return sse("quick")

    client = FakeClient(script)
    router, _ = make_router(tmp_path, [slow, fast], client, watchdog_s=0.05)

    out = await collect(router, [{"role": "user", "content": "hello"}])

    assert "".join(c for _, c in out) == "quick"
    kind, wait = router.breakers.describe(slow.name)
    assert kind == "slow" and wait > 0
    assert router.last_provider == "fast"


async def test_budget_exhausted_brain_is_skipped_without_call(tmp_path):
    rich_p, poor_p = prov("rich", 0), prov("poor", 1, budget=10)
    client = FakeClient(lambda model, msgs, tools=None: sse("ok"))
    router, ledger = make_router(tmp_path, [rich_p, poor_p], client)
    ledger.add(poor_p.name, 999)

    out = await collect(router, [{"role": "user", "content": "hi"}])

    assert {n for n, _ in out} == {"rich"}
    assert [m for m, _, _ in client.calls] == [rich_p.model]


async def test_midstream_death_replays_partial_and_continues(tmp_path):
    flaky, steady = prov("flaky", 0), prov("steady", 1)

    def script(model, msgs, tools=None):
        if model == flaky.model:
            return dying_midway_gen("Hel", "lo ")
        return sse("world")

    client = FakeClient(script)
    router, _ = make_router(tmp_path, [flaky, steady], client)

    out = await collect(router, [{"role": "user", "content": "greet"}])

    assert "".join(c for _, c in out) == "Hello world"
    second_model, second_msgs, _ = client.calls[1]
    assert second_model == steady.model
    assert second_msgs[-2]["role"] == "assistant" and second_msgs[-2]["content"] == "Hello "
    assert "Continue it seamlessly" in second_msgs[-1]["content"]


async def test_empty_stream_hops_to_next_brain(tmp_path):
    mute, talker = prov("mute", 0), prov("talker", 1)

    def script(model, msgs, tools=None):
        if model == mute.model:
            return sse()
        return sse("here")

    client = FakeClient(script)
    router, _ = make_router(tmp_path, [mute, talker], client)
    events: list[tuple[str, str]] = []
    router.on_event = lambda name, err: events.append((name, str(err)))

    out = await collect(router, [{"role": "user", "content": "hi"}])

    assert "".join(c for _, c in out) == "here"
    assert router.last_provider == "talker"
    assert events and events[0][0] == "mute" and "empty stream" in events[0][1]


async def test_heartbeats_do_not_block_or_leak(tmp_path):
    thinker, plain = prov("thinker", 0), prov("plain", 1)

    async def thinking_gen():
        for _ in range(4):
            await asyncio.sleep(0.08)
            yield HEARTBEAT
        yield "answer"

    def script(model, msgs, tools=None):
        if model == thinker.model:
            return thinking_gen()
        return sse("fallback")

    client = FakeClient(script)
    router, _ = make_router(tmp_path, [thinker, plain], client, watchdog_s=0.15)
    events: list[tuple[str, str]] = []
    router.on_event = lambda name, err: events.append((name, str(err)))

    out = await collect(router, [{"role": "user", "content": "think"}])

    assert "".join(c for _, c in out) == "answer"
    assert router.last_provider == "thinker"
    assert events == []


async def test_all_down_raises(tmp_path):
    a = prov("a", 0)
    b = prov("b", 1)
    client = FakeClient(lambda model, msgs, tools=None: rate_limited_gen())
    router, _ = make_router(tmp_path, [a, b], client)

    with pytest.raises(AllProvidersDown):
        await collect(router, [{"role": "user", "content": "anyone?"}])


async def sticky_429_then_ok_gen():
    raise RateLimited("429, try again in 0.05s", retry_after=0.05)
    yield


async def test_sticky_retry_same_brain_on_short_429(tmp_path):
    # short "try again in Ns": wait and retry the SAME provider once,
    # no hop to a different (differently-thinking) model.
    a, b = prov("a", 0), prov("b", 1)
    calls = {"n": 0}

    def script(model, msgs, tools=None):
        if model == a.model:
            calls["n"] += 1
            if calls["n"] == 1:
                return sticky_429_then_ok_gen()
            return sse("Hi", " Sir")
        return sse("WRONG", " BRAIN")

    client = FakeClient(script)
    router, _ = make_router(tmp_path, [a, b], client)

    out = await collect(router, [{"role": "user", "content": "hi"}])

    assert "".join(c for _, c in out) == "Hi Sir"
    assert {n for n, _ in out} == {"a"}
    assert [m for m, _, _ in client.calls] == [a.model, a.model]


async def test_no_sticky_wait_on_long_429(tmp_path):
    # long "retry in an hour": fail over immediately, no waiting.
    async def long_429_gen():
        raise RateLimited("429, try again in 3600s", retry_after=3600.0)
        yield  # pragma: no cover

    a, b = prov("a", 0), prov("b", 1)
    client = FakeClient(
        lambda model, msgs, tools=None: long_429_gen() if model == a.model else sse("ok")
    )
    router, _ = make_router(tmp_path, [a, b], client)

    import time as _t

    start = _t.monotonic()
    out = await collect(router, [{"role": "user", "content": "hi"}])
    elapsed = _t.monotonic() - start

    assert "".join(c for _, c in out) == "ok"
    assert [m for m, _, _ in client.calls] == [a.model, b.model]
    assert elapsed < 5.0


async def test_ledger_counts_tokens_per_day(tmp_path):
    p = prov("solo", 0)
    client = FakeClient(lambda model, msgs, tools=None: sse("abcd" * 100))
    router, ledger = make_router(tmp_path, [p], client)

    await collect(router, [{"role": "user", "content": "hi"}])

    assert ledger.today(p.name) > 0
    assert dict(ledger.summary())[p.name] == ledger.today(p.name)


async def test_tool_capable_providers_preferred_when_tools_requested(tmp_path):
    no_tools = prov("no-tools", 0)
    has_tools = prov("has-tools", 1, budget=10)
    has_tools.supports_tools = True

    client = FakeClient(lambda model, msgs, tools=None: sse("ok"))
    router, _ = make_router(tmp_path, [no_tools, has_tools], client)

    await collect(
        router,
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "ping"}}],
    )

    # has-tools should be called first even though it has lower priority
    assert [m for m, _, _ in client.calls] == [has_tools.model]


async def test_router_passes_tools_only_to_capable_providers(tmp_path):
    no_tools = prov("no-tools", 0)
    has_tools = prov("has-tools", 1)
    has_tools.supports_tools = True

    tools_seen = []

    def script(model, msgs, tools=None):
        tools_seen.append((model, tools is not None))
        return sse("ok")

    client = FakeClient(script)
    router, _ = make_router(tmp_path, [no_tools, has_tools], client)

    await collect(
        router,
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "ping"}}],
    )

    assert tools_seen == [(has_tools.model, True)]  # only has-tools gets tools


async def test_router_streams_tool_call_delta(tmp_path):
    tc = prov("tool-capable", 0)
    tc.supports_tools = True

    async def gen_with_tool_call():
        yield ToolCallDelta(index=0, id="call_1", name="ping", args='{"x"')
        yield ToolCallDelta(index=0, id=None, name=None, args=":1}")

    client = FakeClient(lambda model, msgs, tools=None: gen_with_tool_call())
    router, _ = make_router(tmp_path, [tc], client)

    out = await collect(
        router,
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "ping"}}],
    )

    # Should receive ToolCallDelta objects
    deltas = [c for _, c in out if isinstance(c, ToolCallDelta)]
    assert len(deltas) == 2
    assert deltas[0].name == "ping"
    assert deltas[1].args == ":1}"


def test_ledger_accepts_str_db_path(tmp_path):
    # Regression: __init__ used to call .parent on the raw argument, crashing
    # with AttributeError when a plain string path was passed instead of Path.
    ledger = UsageLedger(str(tmp_path / "usage.db"))
    ledger.add("p", 10)
    assert ledger.today("p") == 10


def test_router_module_has_single_definitions():
    # Regression: AllProvidersDown and estimate_tokens were defined twice in
    # router.py (top of file AND repeated at the bottom).
    import inspect

    import core.router as router_mod

    src = inspect.getsource(router_mod)
    assert src.count("class AllProvidersDown") == 1
    assert src.count("def estimate_tokens") == 1

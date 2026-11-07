"""Chain hardening: no-chat exclusion, 400 tool strip, no-retry-same, tried list."""

import asyncio

import pytest

from core.config import ProviderConfig
from core.providers import ProviderError
from core.router import AllProvidersDown, FailoverRouter, UsageLedger


def prov(name, priority, model=None, budget=1_000_000):
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.test/v1",
        model=model or name,
        api_key="k",
        priority=priority,
        daily_token_budget=budget,
    )


def sse(*pieces):
    async def gen():
        for p in pieces:
            yield p

    return gen()


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def stream_chat(self, base_url, api_key, model, messages, tools=None):
        self.calls.append((model, [dict(m) for m in messages], tools))
        return self.script(model, messages, tools)


def make_router(tmp_path, providers, client, watchdog_s=0.2):
    ledger = UsageLedger(tmp_path / "usage.db")
    return FailoverRouter(providers, client, ledger, watchdog_s=watchdog_s), ledger


async def collect(router, messages, tools=None):
    out = []
    async for name, chunk in router.chat(messages, tools):
        out.append((name, chunk))
    return out


def run(coro):
    return asyncio.run(coro)


def test_no_chat_excluded(tmp_path):
    a = prov("a", 0)
    a.supports_chat = False
    b = prov("b", 1)
    client = FakeClient(lambda model, msgs, tools=None: sse("ok"))
    router, _ = make_router(tmp_path, [a, b], client)
    _out = run(collect(router, [{"role": "user", "content": "hi"}]))
    assert [m for m, _, _ in client.calls] == [b.model]


def test_strips_tool_calls_on_400(tmp_path):
    a, b = prov("a", 0), prov("b", 1)

    async def fail_400():
        raise ProviderError(
            "https://a.test/v1/chat/completions: 400 Validation messages[11].tool_calls bad"
        )
        yield

    client = FakeClient(
        lambda model, msgs, tools=None: fail_400() if model == a.model else sse("recovered")
    )
    router, _ = make_router(tmp_path, [a, b], client)
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "x", "arguments": "{bad"}}
            ],
        },
        {"role": "tool", "tool_call_id": "1", "content": "err"},
    ]
    out = run(collect(router, msgs))
    assert "".join(c for _, c in out) == "recovered"
    second = client.calls[1][1]
    assert all("tool_calls" not in m for m in second)
    assert all(m.get("role") != "tool" for m in second)


def test_tried_list_in_final_error(tmp_path):
    a = prov("a", 0)

    async def fail():
        raise ProviderError("boom")
        yield

    client = FakeClient(lambda model, msgs, tools=None: fail())
    router, _ = make_router(tmp_path, [a], client)
    with pytest.raises(AllProvidersDown, match="a"):
        run(collect(router, [{"role": "user", "content": "hi"}]))
    # tried once — never retried same provider in one turn
    assert len(client.calls) == 1

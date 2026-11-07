from __future__ import annotations

from core.config import Config, ProviderConfig, WatchdogConfig
from core.memory import MemoryStore
from core.orchestrator import Orchestrator
from core.tools import default_toolbox


def make_store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.db")


async def test_add_and_recall_fts(tmp_path):
    store = make_store(tmp_path)
    assert store.add("The user's favorite project is Jarvis Mark 3") == "stored"
    assert store.add("The user drinks coffee every morning") == "stored"
    assert store.add("The user's favorite project is Jarvis Mark 3") == "already known"

    rows = store.search("favorite project", k=3)
    assert rows and "Jarvis Mark 3" in rows[0][2]


async def test_search_no_match_returns_empty(tmp_path):
    store = make_store(tmp_path)
    store.add("likes tea")
    assert store.search("quantum physics homework", k=3) == []


async def test_forget(tmp_path):
    store = make_store(tmp_path)
    store.add("birthday is May 5")
    assert store.forget("birthday") == 1
    assert store.count() == 0
    assert store.forget("birthday") == 0


async def test_recall_tool_roundtrip(tmp_path):
    store = make_store(tmp_path)
    box = default_toolbox(store)
    assert "available" not in box.names() or "remember" in box.names()
    assert (await box.execute("remember", '{"text": "user speaks Tamil and English"}')) == "stored"
    result = await box.execute("recall", '{"query": "Tamil languages"}')
    assert "Tamil" in result


async def test_memory_injection_into_turn(tmp_path):
    p = ProviderConfig(
        name="brain", base_url="https://b.test/v1", model="m", api_key="k", priority=0
    )
    cfg = Config(
        jarvis_name="JARVIS TEST",
        system_prompt="test",
        watchdog=WatchdogConfig(
            first_token_timeout_s=0.5, connect_timeout_s=2.0, read_timeout_s=5.0
        ),
        providers=[p],
        db_path=tmp_path / "usage.db",
    )
    orch = Orchestrator(cfg)
    orch.memory.add("The user's favorite color is blue")

    captured: list[list[dict]] = []

    class FakeClient:
        def stream_chat(self, base_url, api_key, model, messages, tools=None):
            captured.append([dict(m) for m in messages])

            async def gen():
                yield "noted"

            return gen()

    orch.router.client = FakeClient()
    out = [c async for _, c in orch.process("what is my favorite color?")]
    assert "".join(out) == "noted"

    sent = captured[0]
    assert sent[0]["role"] == "system"
    mem_msgs = [
        m
        for m in sent
        if m["role"] == "system" and "favorite color is blue" in m.get("content", "")
    ]
    assert len(mem_msgs) == 1
    assert all(
        "favorite color" not in str(m.get("content")) for m in orch.history if m["role"] == "system"
    )


async def test_trim_never_orphans_tool_messages(tmp_path):
    p = ProviderConfig(
        name="brain", base_url="https://b.test/v1", model="m", api_key="k", priority=0
    )
    cfg = Config(
        jarvis_name="JARVIS TEST",
        system_prompt="test",
        watchdog=WatchdogConfig(
            first_token_timeout_s=0.5, connect_timeout_s=2.0, read_timeout_s=5.0
        ),
        providers=[p],
        db_path=tmp_path / "usage.db",
    )
    orch = Orchestrator(cfg)
    orch.history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "assistant", "content": "done"},
    ]
    orch.context_budget_tokens = 5
    orch._trim()
    roles = [m["role"] for m in orch.history]
    assert roles[0] == "system"
    for i, r in enumerate(roles):
        if r == "tool":
            assert roles[i - 1] == "assistant" and orch.history[i - 1].get("tool_calls")

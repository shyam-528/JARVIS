from __future__ import annotations

from core.config import Config, ProviderConfig, WatchdogConfig
from core.orchestrator import Orchestrator
from core.providers import ToolCallDelta
from core.tools import Tool, ToolBox


async def add(a: int, b: int) -> str:
    return str(a + b)


async def boom() -> str:
    raise ValueError("kaboom")


def make_box() -> ToolBox:
    box = ToolBox()
    box.register(
        Tool(
            name="add",
            description="add two numbers",
            parameters={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
            handler=add,
        )
    )
    box.register(
        Tool(
            name="boom",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            handler=boom,
        )
    )
    return box


async def test_toolbox_executes_and_formats():
    box = make_box()
    assert await box.execute("add", '{"a": 2, "b": 3}') == "5"
    assert (await box.execute("add", "")).startswith("error: wrong arguments")


async def test_toolbox_unknown_tool():
    box = make_box()
    assert "unknown tool" in await box.execute("nope", "{}")


async def test_toolbox_bad_json():
    box = make_box()
    assert "bad arguments JSON" in await box.execute("add", "{not json")


async def test_toolbox_handler_exception_caught():
    box = make_box()
    result = await box.execute("boom", "{}")
    assert result.startswith("error:") and "kaboom" in result


async def test_toolbox_schemas_shape():
    box = make_box()
    schemas = box.schemas()
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] in ("add", "boom")
    assert box.names() == ["add", "boom"]


def make_cfg(tmp_path) -> Config:
    p = ProviderConfig(
        name="brain",
        base_url="https://brain.test/v1",
        model="brain-model",
        api_key="k",
        priority=0,
        supports_tools=True,
    )
    return Config(
        jarvis_name="JARVIS TEST",
        system_prompt="test",
        watchdog=WatchdogConfig(
            first_token_timeout_s=0.5, connect_timeout_s=2.0, read_timeout_s=5.0
        ),
        providers=[p],
        db_path=tmp_path / "usage.db",
    )


class ScriptedClient:
    def __init__(self, script):
        self.script = script
        self.calls: list[list[dict]] = []

    def stream_chat(self, base_url, api_key, model, messages, tools=None):
        self.calls.append([dict(m) for m in messages])
        return self.script(messages, tools)


async def test_orchestrator_tool_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    orch = Orchestrator(cfg)

    async def gen_tool_call(messages, tools):
        if not any(m.get("role") == "tool" for m in messages):
            yield ToolCallDelta(index=0, id="c1", name="add", args='{"a": 2, ')
            yield ToolCallDelta(index=0, id=None, name=None, args='"b": 3}')
        else:
            yield "The sum is 5, Sir."

    orch.router.client = ScriptedClient(gen_tool_call)
    orch.toolbox.register(
        Tool(
            name="add",
            description="add",
            parameters={"type": "object", "properties": {}},
            handler=add,
        )
    )

    out = []
    async for provider, chunk in orch.process("what is 2+3"):
        out.append((provider, chunk))

    text = "".join(c for p, c in out if p != "tool")
    assert "The sum is 5, Sir." in text
    tool_lines = [c for p, c in out if p == "tool"]
    assert any("add" in line for line in tool_lines)

    tool_msgs = [m for m in orch.history if m.get("role") == "tool"]
    assert (
        len(tool_msgs) == 1
        and tool_msgs[0]["content"] == "5"
        and tool_msgs[0]["tool_call_id"] == "c1"
    )
    assistant_final = orch.history[-1]
    assert assistant_final["role"] == "assistant" and "5" in assistant_final["content"]


async def test_orchestrator_plain_chat_no_tools(tmp_path):
    cfg = make_cfg(tmp_path)
    orch = Orchestrator(cfg)

    async def gen_plain(messages, tools):
        assert tools is None or tools
        yield "hello"

    orch.router.client = ScriptedClient(gen_plain)
    out = [c async for _, c in orch.process("hi")]
    assert "".join(out) == "hello"
    assert orch.history[-1] == {"role": "assistant", "content": "hello"}

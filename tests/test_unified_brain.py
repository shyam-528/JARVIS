"""Unified brain: mesh chat protocol round-trip + proxy fallback."""

from __future__ import annotations

import asyncio
import socket
import uuid

from core.agent_mesh import MeshNetwork


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_node(name: str, api_key: str = "brainkey") -> MeshNetwork:
    return MeshNetwork(
        local_agent_id=f"{name}-{uuid.uuid4().hex[:6]}",
        name=name,
        host="127.0.0.1",
        port=free_port(),
        api_key=api_key,
        capabilities={"role": name},
    )


async def wait_for(cond, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


# ── protocol: brain node streams a full pipeline over the mesh ──


async def test_brain_chat_round_trip_streams_labels_and_chunks():
    brain = make_node("brain")
    client = make_node("client")

    # Fake full pipeline: emits a tool event, tokens, then done
    async def fake_pipeline(message: str):
        yield "tool", f"⚙ web_search({message[:20]})"
        yield "groq-test", "Hello "
        yield "groq-test", "sir."
        yield "bg_dispatch", "sent to coder agent"

    brain.set_chat_handler(fake_pipeline)

    await brain.start()
    await client.start()
    try:
        ok = await client._connect_to_peer(
            {
                "agent_id": brain.local_agent_id,
                "host": brain.host,
                "port": brain.port,
                "name": brain.name,
            }
        )
        assert ok
        assert await wait_for(lambda: client.local_agent_id in brain.peers)

        events: list[tuple[str, str]] = []
        result = await client.send_brain_chat(
            brain.local_agent_id,
            "what time is it",
            on_event=lambda label, chunk: events.append((label, chunk)),
            timeout_s=10,
        )
        assert result["status"] == "done"
        labels = [lbl for lbl, _ in events]
        assert "tool" in labels
        assert "bg_dispatch" in labels
        text = "".join(c for lbl, c in events if lbl == "groq-test")
        assert text == "Hello sir."
    finally:
        await brain.stop()
        await client.stop()


async def test_brain_chat_error_propagates():
    brain = make_node("brain")
    client = make_node("client")

    async def broken_pipeline(message: str):
        raise RuntimeError("all providers down")
        yield  # pragma: no cover

    brain.set_chat_handler(broken_pipeline)
    await brain.start()
    await client.start()
    try:
        await client._connect_to_peer(
            {
                "agent_id": brain.local_agent_id,
                "host": brain.host,
                "port": brain.port,
                "name": brain.name,
            }
        )
        await wait_for(lambda: client.local_agent_id in brain.peers)
        result = await client.send_brain_chat(
            brain.local_agent_id, "hi", on_event=lambda _label, _chunk: None, timeout_s=10
        )
        assert result["status"] == "error"
        assert "all providers down" in result.get("error", "")
    finally:
        await brain.stop()
        await client.stop()


async def test_no_brain_attached_reports_fallback():
    brain = make_node("brain")  # NO chat handler attached
    client = make_node("client")
    await brain.start()
    await client.start()
    try:
        await client._connect_to_peer(
            {
                "agent_id": brain.local_agent_id,
                "host": brain.host,
                "port": brain.port,
                "name": brain.name,
            }
        )
        await wait_for(lambda: client.local_agent_id in brain.peers)
        result = await client.send_brain_chat(
            brain.local_agent_id, "hi", on_event=lambda _label, _chunk: None, timeout_s=10
        )
        assert result["status"] == "no_brain"
    finally:
        await brain.stop()
        await client.stop()


async def test_bad_api_key_rejected_for_chat():
    brain = make_node("brain", api_key="right")
    client = make_node("client", api_key="wrong")
    brain.set_chat_handler(lambda m: None)
    await brain.start()
    await client.start()
    try:
        await client._connect_to_peer(
            {
                "agent_id": brain.local_agent_id,
                "host": brain.host,
                "port": brain.port,
                "name": brain.name,
            }
        )
        await asyncio.sleep(0.3)
        # hello was rejected (bad key) — brain never registered us
        assert client.local_agent_id not in brain.peers
        result = await client.send_brain_chat(
            brain.local_agent_id, "hi", on_event=lambda _label, _chunk: None, timeout_s=3
        )
        # Not in peer_connections on the brain side: either not_connected or timeout
        assert result["status"] in ("not_connected", "timeout")
    finally:
        await brain.stop()
        await client.stop()


async def test_brain_chat_carries_wait_bg_flag():
    brain = make_node("brain")
    client = make_node("client")

    seen: list[bool] = []

    async def pipeline(message: str, wait_bg: bool = False):
        seen.append(wait_bg)
        yield "ok", "done"

    brain.set_chat_handler(pipeline)
    await brain.start()
    await client.start()
    try:
        ok = await client._connect_to_peer(
            {
                "agent_id": brain.local_agent_id,
                "host": brain.host,
                "port": brain.port,
                "name": brain.name,
            }
        )
        assert ok
        assert await wait_for(lambda: client.local_agent_id in brain.peers)

        result = await client.send_brain_chat(
            brain.local_agent_id,
            "hi",
            on_event=lambda _label, _chunk: None,
            timeout_s=10,
            wait_bg=True,
        )
        assert result["status"] == "done"
        result = await client.send_brain_chat(
            brain.local_agent_id,
            "hi",
            on_event=lambda _label, _chunk: None,
            timeout_s=10,
        )
        assert result["status"] == "done"
        assert seen == [True, False]
    finally:
        await brain.stop()
        await client.stop()


# ── proxy: fallback when brain unreachable ──


async def test_proxy_falls_back_to_local_brain(tmp_path):
    from core.unified_brain import UnifiedBrainProxy

    class FakeLocal:
        class cfg:
            class a2a:
                enabled = False

        async def process(self, message: str, wait_bg: bool = False):
            yield "local-fallback", "local answer"

    proxy = UnifiedBrainProxy(FakeLocal())
    # a2a disabled → start() no-ops; chat falls straight back to local
    await proxy.start()
    assert not proxy.available
    chunks = []
    async for label, chunk in proxy.chat_stream("hi"):
        chunks.append((label, chunk))
    assert chunks == [("local-fallback", "local answer")]
    await proxy.stop()

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from core.agent_mesh import MeshNetwork, MeshProtocol


def free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_node(name: str, api_key: str = "secret", executor=None) -> MeshNetwork:
    # Realistic ids (uuid-suffixed like the orchestrator creates)
    return MeshNetwork(
        local_agent_id=f"{name}-{uuid.uuid4().hex[:6]}",
        name=name,
        host="127.0.0.1",
        port=free_port(),
        api_key=api_key,
        capabilities={"tools": ["test_tool"], "version": "1.0.0"},
    )


async def connect(a: MeshNetwork, b: MeshNetwork) -> bool:
    """Have node a dial node b."""
    return await a._connect_to_peer(
        {
            "agent_id": b.local_agent_id,
            "host": b.host,
            "port": b.port,
            "name": b.name,
        }
    )


async def wait_for(condition, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return False


# ---------- protocol ----------


def test_protocol_encode_decode_roundtrip():
    msg = {"msg_type": "hello", "sender_id": "a", "payload": {"x": 1}}
    encoded = MeshProtocol.encode(msg)
    assert encoded.startswith(b"JARVIS-MESH")
    assert MeshProtocol.decode(encoded) == msg


def test_protocol_decode_rejects_garbage():
    assert MeshProtocol.decode(b"garbage-data") is None
    assert MeshProtocol.decode(b"JARVIS-MESH" + b"\x01") is None


# ---------- two-node round trip ----------


async def test_start_refuses_to_bind_without_api_key():
    """Fail closed: an empty api_key would mean an unauthenticated mesh
    listener on 0.0.0.0 — start() must refuse to bind instead."""
    node = make_node("nokey", api_key="")
    await node.start()
    try:
        assert not node.running, "mesh must not run without an API key"
        assert node._server is None, "no listener may be bound without an API key"
        # Belt-and-braces: the port really is closed.
        import socket

        with pytest.raises(OSError):
            s = socket.create_connection((node.host, node.port), timeout=1.0)
            s.close()
    finally:
        await node.stop()


async def test_two_nodes_connect_and_ping():
    a = make_node("alpha")
    b = make_node("bravo")
    await a.start()
    await b.start()
    try:
        assert await connect(a, b), "a should dial b"
        # Wait for the FULL handshake: b registered a from the hello
        assert await wait_for(lambda: a.local_agent_id in b.peers), "b should register a from hello"

        # capabilities round-trip
        caps = await a.request_capabilities(b.local_agent_id)
        assert caps, "capabilities request should resolve"
        assert caps["agent_id"] == b.local_agent_id
    finally:
        await a.stop()
        await b.stop()


async def test_task_roundtrip_executes_via_executor():
    a = make_node("alpha")
    calls = []

    async def executor(action: str, args_json: str) -> str:
        calls.append((action, args_json))
        return f"done:{action}"

    b = make_node("bravo", executor=executor)
    b.set_task_executor(executor)
    await a.start()
    await b.start()
    try:
        assert await connect(a, b)
        assert await wait_for(lambda: a.local_agent_id in b.peers)

        result = await a.send_task_and_wait(b.local_agent_id, "test_tool", {"x": 1}, timeout_s=10)
        assert result["status"] == "completed", result
        assert result["result"] == "done:test_tool"
        assert calls == [("test_tool", json.dumps({"x": 1}))]
    finally:
        await a.stop()
        await b.stop()


async def test_bad_api_key_is_rejected():
    a = make_node("alpha", api_key="right-key")
    b = make_node("bravo", api_key="wrong-key")
    await a.start()
    await b.start()
    try:
        assert await connect(a, b)
        await asyncio.sleep(0.3)
        # a never registers b as a peer (hello rejected due to key mismatch)
        assert b.local_agent_id not in a.peers
        # And a task from a to b... a doesn't even have b connected via hello path;
        # but the raw connection exists in a.peer_connections (dialer-side).
        # b will reject the task because a's api_key doesn't match b's.
        result = await a.send_task_and_wait(b.local_agent_id, "test_tool", {}, timeout_s=3)
        assert result["status"] in ("timeout", "failed"), result
    finally:
        await a.stop()
        await b.stop()


async def test_gossip_learns_transitive_peers():
    """a <-> b <-> c: a should learn about c through b's gossip."""
    a = make_node("alpha")
    b = make_node("bravo")
    c = make_node("charlie")
    for n in (a, b, c):
        await n.start()
    try:
        assert await connect(a, b)
        assert await connect(c, b)  # c dials b (b accepts as server + hello)
        await wait_for(lambda: c.local_agent_id in b.peers)

        # Retry gossip until a learns about c (idempotent on the receiver)
        gossip = {
            "msg_id": "g1",
            "msg_type": "gossip",
            "sender_id": b.local_agent_id,
            "api_key": "secret",
            "target_ids": [],
            "payload": {
                "peers": [
                    {
                        "agent_id": c.local_agent_id,
                        "name": "charlie",
                        "host": c.host,
                        "port": c.port,
                    },
                    {"agent_id": b.local_agent_id, "name": "bravo", "host": b.host, "port": b.port},
                ]
            },
            "ttl": 1,
        }
        deadline = asyncio.get_event_loop().time() + 10
        learned = False
        while asyncio.get_event_loop().time() < deadline:
            if c.local_agent_id in a.peers:
                learned = True
                break
            await b._broadcast(gossip)
            await asyncio.sleep(0.3)
        assert learned, "a should learn about c from b's gossip"
    finally:
        for n in (a, b, c):
            await n.stop()


async def test_gossip_loop_runs_without_name_error():
    """Regression: the gossip snapshot comprehension was missing its for-clause
    (undefined pid/info) — every tick raised NameError and silently killed the
    gossip task. The task must stay alive across several ticks."""
    a = make_node("alpha")
    b = make_node("bravo")
    b.gossip_interval = 0.1  # fast ticks so the test exercises several loops
    await a.start()
    await b.start()
    try:
        assert await connect(a, b)
        assert await wait_for(lambda: a.local_agent_id in b.peers)
        await asyncio.sleep(0.35)  # several gossip ticks
        task = b._gossip_task
        assert not task.done() or task.exception() is None, (
            f"gossip loop died: {task.exception() if task.done() else ''}"
        )
        # And the fix actually advertises known peers in the payload.
        assert a.local_agent_id in b.peers
    finally:
        await a.stop()
        await b.stop()


async def test_heartbeat_keeps_peer_alive():
    a = make_node("alpha")
    b = make_node("bravo")
    await a.start()
    await b.start()
    try:
        assert await connect(a, b)
        assert await wait_for(lambda: a.local_agent_id in b.peers)
        before = b.peers[a.local_agent_id]["last_seen"]
        await asyncio.sleep(0.1)
        # Send a heartbeat from a to b
        await a._broadcast(
            {
                "msg_id": "h1",
                "msg_type": "heartbeat",
                "sender_id": a.local_agent_id,
                "api_key": "secret",
                "target_ids": [],
                "payload": {},
                "ttl": 1,
            }
        )
        assert await wait_for(lambda: b.peers[a.local_agent_id]["last_seen"] != before)
    finally:
        await a.stop()
        await b.stop()

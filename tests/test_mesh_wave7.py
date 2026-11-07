"""Wave 7 failure-path tests for core.agent_mesh.

Every wire/write failure must be loud (printed [Mesh] diagnostic naming the
peer) while still degrading gracefully (False / {} / closed connection).
"""

from __future__ import annotations

import asyncio
import uuid

from core import agent_mesh
from core.agent_mesh import MeshNetwork, MeshProtocol


def make_node(name: str, api_key: str = "secret") -> MeshNetwork:
    return MeshNetwork(
        local_agent_id=f"{name}-{uuid.uuid4().hex[:6]}",
        name=name,
        host="127.0.0.1",
        port=0,
        api_key=api_key,
    )


class BrokenWriter:
    """A StreamWriter stand-in whose writes always blow up."""

    def write(self, data):
        raise ConnectionResetError("boom")

    async def drain(self):
        raise ConnectionResetError("boom")

    def is_closing(self):
        return False

    def close(self):
        pass

    async def wait_closed(self):
        pass


class RecordingWriter:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def is_closing(self):
        return False

    def close(self):
        pass

    async def wait_closed(self):
        pass


# ---------- dead-code contract ----------


def test_dead_dataclasses_removed():
    assert not hasattr(agent_mesh, "MeshMessage")
    assert not hasattr(agent_mesh, "AgentNode")
    assert not hasattr(MeshNetwork, "_process_queue")


# ---------- broadcast ----------


async def test_broadcast_does_not_mutate_caller_exclude_list():
    node = make_node("a")
    exclude = ["someone"]
    await node.broadcast({"msg_type": "x", "payload": {}}, exclude=exclude)
    assert exclude == ["someone"], "broadcast must not append to the caller's list"


async def test_broadcast_logs_broken_peer_and_reaches_others(capsys):
    node = make_node("a")
    good = RecordingWriter()
    node.peer_connections["peer-bad"] = BrokenWriter()
    node.peer_connections["peer-ok"] = good

    await node.broadcast({"msg_type": "hello", "payload": {}})

    out = capsys.readouterr().out
    assert "peer-bad" in out and "failed" in out
    assert good.data, "healthy peer must still receive the message"


async def test__broadcast_logs_broken_peer(capsys):
    node = make_node("a")
    node.peer_connections["peer-bad"] = BrokenWriter()
    await node._broadcast({"msg_type": "x", "payload": {}})
    out = capsys.readouterr().out
    assert "peer-bad" in out and "broadcast" in out


# ---------- send paths ----------


async def test_send_task_failure_is_loud_and_returns_false(capsys):
    node = make_node("a")
    node.peer_connections["peer-bad"] = BrokenWriter()
    sent = await node.send_task("peer-bad", {"task_id": "t1", "action": "x"})
    assert sent is False
    out = capsys.readouterr().out
    assert "peer-bad" in out and "send_task" in out


async def test_send_raw_failure_is_loud_and_returns_false(capsys):
    node = make_node("a")
    node.peer_connections["peer-bad"] = BrokenWriter()
    msg = {"msg_type": "x", "payload": {}, "target_ids": ["peer-bad"]}
    sent = await node.send_raw(msg)
    assert sent is False
    out = capsys.readouterr().out
    assert "peer-bad" in out and "send_raw" in out


async def test_send_raw_returns_false_without_targets_without_raising():
    node = make_node("a")
    msg = {"msg_type": "x", "payload": {}, "target_ids": ["nobody"]}
    assert await node.send_raw(msg) is False


async def test_forward_logs_broken_peer(capsys):
    node = make_node("a")
    node.peer_connections["peer-bad"] = BrokenWriter()
    await node._forward_message({"msg_type": "gossip", "ttl": 2, "payload": {}})
    out = capsys.readouterr().out
    assert "peer-bad" in out and "forward" in out


# ---------- observers / callbacks ----------


async def test_observer_exception_is_loud_and_handler_still_runs(capsys):
    node = make_node("a")
    seen = []

    def bad_observer(message):
        raise RuntimeError("observer exploded")

    async def spy_handler(message, writer):
        seen.append(message["msg_type"])

    node.set_incoming_observer(bad_observer)
    node.message_handlers["ping"] = spy_handler

    await node._process_message(
        {"msg_type": "ping", "sender_id": "peer-x", "api_key": "secret", "payload": {}},
        RecordingWriter(),
    )
    out = capsys.readouterr().out
    assert "observer exploded" in out
    assert seen == ["ping"], "handler must still run after observer failure"


async def test_chat_stream_callback_failure_is_loud(capsys):
    node = make_node("a")

    def bad_cb(label, chunk):
        raise ValueError("cb blew up")

    node._chat_streams["req1"] = bad_cb
    await node._handle_chat_stream(
        {"payload": {"request_id": "req1", "label": "l", "chunk": "c"}}, RecordingWriter()
    )
    out = capsys.readouterr().out
    assert "req1" in out and "cb blew up" in out


# ---------- protocol violations ----------


async def test_bad_magic_frame_is_loud(capsys):
    node = make_node("a")
    reader = asyncio.StreamReader()
    reader.feed_data(b"NOT-MESH-PROTOCOL!!")
    reader.feed_eof()
    writer = RecordingWriter()
    writer.get_extra_info = lambda name: ("1.2.3.4", 9999) if name == "peername" else None

    await node._handle_connection(reader, writer)

    out = capsys.readouterr().out
    assert "bad frame magic" in out


async def test_wrong_version_frame_is_loud(capsys):
    node = make_node("a")
    reader = asyncio.StreamReader()
    bad_version = MeshProtocol.MAGIC + bytes([MeshProtocol.VERSION + 1]) + b"\x00\x00\x00\x00"
    reader.feed_data(bad_version)
    reader.feed_eof()
    writer = RecordingWriter()
    writer.get_extra_info = lambda name: ("1.2.3.4", 9999) if name == "peername" else None

    await node._handle_connection(reader, writer)

    out = capsys.readouterr().out
    assert "protocol v" in out


# ---------- lifecycle ----------


async def test_stop_reports_background_task_failure(capsys):
    node = make_node("a")

    async def stubborn():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise RuntimeError("loop died badly") from None

    node._heartbeat_task = asyncio.get_event_loop().create_task(stubborn())
    await asyncio.sleep(0)  # let the task start so cancel lands inside sleep(60)
    node._cleanup_task = None
    node._gossip_task = None
    await node.stop()

    out = capsys.readouterr().out
    assert "loop died badly" in out


async def test_gossip_loop_invalid_interval_is_loud(capsys):
    node = make_node("a")
    node.gossip_interval = "not-a-number"
    node.running = False  # loop exits immediately after parsing the interval
    await node._gossip_loop()
    out = capsys.readouterr().out
    assert "gossip_interval" in out


# ---------- surface passthrough ----------


async def test_chat_request_passes_surface_to_pipeline():
    node = make_node("a")
    captured = {}

    async def fake_pipeline(message, wait_bg=False, surface=""):
        captured["surface"] = surface
        yield ("label", "chunk")

    node.set_chat_handler(fake_pipeline)
    writer = RecordingWriter()
    await node._handle_chat_request(
        {
            "sender_id": "peer-x",
            "payload": {"request_id": "r1", "message": "hi", "surface": "telegram"},
        },
        writer,
    )
    # let the fire-and-forget _stream_out task run
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured.get("surface") == "telegram"
    assert b"chat_done" in writer.data


async def test_capabilities_timeout_is_loud(capsys, monkeypatch):
    node = make_node("a")
    node.peer_connections["peer-slow"] = RecordingWriter()

    async def instant_timeout(fut, timeout):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", instant_timeout)
    caps = await node.request_capabilities("peer-slow")
    assert caps == {}
    out = capsys.readouterr().out
    assert "peer-slow" in out and "timed out" in out
    # default handler must be restored even on timeout
    assert node.message_handlers["capabilities"] == node._handle_capabilities

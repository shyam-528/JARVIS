"""Peer-to-peer agent mesh: TCP transport, capability gossip, task relay.

Contract (Wave 7 hardening):
- Auth fails closed: start() refuses to bind without an api_key, and
  messages with a bad key are rejected with the connection dropped.
- Wire failures are loud: every send/write failure prints a [Mesh]-
  prefixed diagnostic naming the peer. Expected peer disconnects on read
  stay quiet; malformed frames and protocol violations do not.
- Fail-soft is documented where intentional (e.g. connection resets
  during shutdown); everything else must surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime


class MeshProtocol:
    MAGIC = b"JARVIS-MESH"
    VERSION = 1
    HEADER_LEN = len(MAGIC) + 1 + 4  # magic + version + length

    @staticmethod
    def encode(message: dict) -> bytes:
        data = json.dumps(message).encode("utf-8")
        length = len(data)
        return (
            MeshProtocol.MAGIC
            + MeshProtocol.VERSION.to_bytes(1, "big")
            + length.to_bytes(4, "big")
            + data
        )

    @staticmethod
    def decode(data: bytes) -> dict | None:
        if not data.startswith(MeshProtocol.MAGIC):
            return None
        if len(data) < MeshProtocol.HEADER_LEN:
            return None
        version = data[len(MeshProtocol.MAGIC)]
        if version != MeshProtocol.VERSION:
            return None
        length = int.from_bytes(
            data[len(MeshProtocol.MAGIC) + 1 : len(MeshProtocol.MAGIC) + 5], "big"
        )
        if len(data) < MeshProtocol.HEADER_LEN + length:
            return None
        payload = data[MeshProtocol.HEADER_LEN : MeshProtocol.HEADER_LEN + length]
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return None


class MeshNetwork:
    def __init__(
        self,
        local_agent_id: str,
        name: str,
        host: str = "0.0.0.0",
        port: int = 8082,
        api_key: str = "",
        capabilities: dict = None,
    ):
        self.local_agent_id = local_agent_id
        self.name = name
        self.host = host
        self.port = port
        self.api_key = api_key
        self.capabilities = capabilities or {}

        self.peers: dict[str, dict] = {}
        self.peer_connections: dict[str, asyncio.StreamWriter] = {}
        self.running = False
        self._server_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._gossip_task: asyncio.Task | None = None
        self._server = None
        self.message_handlers: dict[str, callable] = {}
        # Tool execution hook — set by the orchestrator so received mesh
        # tasks run through the real toolbox. Signature: (action, args_json) -> str
        self.task_executor: callable | None = None
        # Incoming message hook — set by DistributedBrain to observe the full
        # mesh traffic stream (capability ads, inference req/resp, etc.)
        self.incoming_observer: callable | None = None
        # Federated inference hook — async (query, task_type) -> stream of (name, chunk)
        self.inference_fn: callable | None = None
        # Pending send_task_and_wait results, keyed by task_id
        self._pending_results: dict[str, asyncio.Future] = {}
        # In-progress dial attempts (prevents duplicate simultaneous dials)
        self._dialing_lock: set[str] = set()
        # Senders already rejected for bad API keys (dedup warning logs)
        self._auth_rejected: set[str] = set()
        # Host peers should dial (bind 0.0.0.0 but advertise a dialable address)
        self.advertised_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        # Gossip interval override (set by host from config)
        self.gossip_interval: int = 30
        # Unified-brain chat: full-brain chat handler + stream subscribers.
        # chat_fn signature: async (message: str) -> stream of (label, chunk)
        self.chat_fn: callable | None = None
        # request_id -> callback(label, chunk) receiving stream events
        self._chat_streams: dict[str, callable] = {}
        # request_id -> asyncio.Future for the terminal chat_done event
        self._chat_done: dict[str, asyncio.Future] = {}

        # Register default handlers
        self._register_default_handlers()

    def set_task_executor(self, fn) -> None:
        """Attach toolbox-backed execution for incoming mesh tasks."""
        self.task_executor = fn

    def set_incoming_observer(self, fn) -> None:
        """Attach a async (message: dict) -> None observer for all incoming traffic."""
        self.incoming_observer = fn

    def _register_default_handlers(self):
        self.message_handlers.update(
            {
                "ping": self._handle_ping,
                "pong": self._handle_pong,
                "capabilities": self._handle_capabilities,
                "task": self._handle_task,
                "result": self._handle_result,
                "broadcast": self._handle_broadcast,
                "gossip": self._handle_gossip,
                "hello": self._handle_hello,
                "heartbeat": self._handle_heartbeat,
                # Distributed-inference protocol messages pass straight to the
                # observer (DistributedBrain); no toolbox execution involved.
                "capability_advertisement": self._handle_passthrough,
                "inference_request": self._handle_inference_request,
                "inference_response": self._handle_passthrough,
                "health_check": self._handle_passthrough,
                # Unified-brain chat protocol (CLI brain ↔ API server child)
                "chat_request": self._handle_chat_request,
                "chat_stream": self._handle_chat_stream,
                "chat_done": self._handle_chat_done_event,
            }
        )

    # ── Unified-brain chat protocol ────────────────────────────────
    # A client (API server) sends chat_request; the brain (CLI) runs its FULL
    # orchestrator.process() pipeline and streams (label, chunk) events back
    # via chat_stream, terminating with chat_done. History, tools, memory and
    # agents all live in the ONE brain process.

    def set_chat_handler(self, fn) -> None:
        """Attach the full-brain chat pipeline. async (msg) -> stream (label, chunk)."""
        self.chat_fn = fn

    async def _handle_chat_request(self, message: dict, writer: asyncio.StreamWriter):
        sender = message.get("sender_id", "")
        payload = message.get("payload", {})
        request_id = payload.get("request_id", "")
        user_msg = payload.get("message", "")
        wait_bg = bool(payload.get("wait_bg", False))
        surface = str(payload.get("surface", "") or "")

        if not self.chat_fn or not user_msg:
            # No brain attached — tell the caller so it can fall back
            err = {
                "msg_id": str(uuid.uuid4()),
                "msg_type": "chat_done",
                "sender_id": self.local_agent_id,
                "target_ids": [sender],
                "api_key": self.api_key,
                "payload": {"request_id": request_id, "status": "no_brain", "provider": ""},
                "ttl": 1,
            }
            if not writer.is_closing():
                try:
                    writer.write(MeshProtocol.encode(err))
                    await writer.drain()
                except Exception as exc:
                    print(f"[Mesh] no-brain reply to {sender} failed: {exc}")
            return

        async def _stream_out():
            try:
                try:
                    stream = self.chat_fn(user_msg, wait_bg=wait_bg, surface=surface)
                except TypeError:
                    try:
                        stream = self.chat_fn(user_msg, wait_bg=wait_bg)
                    except TypeError:
                        # older single-arg pipelines (and test fakes)
                        stream = self.chat_fn(user_msg)
                async for label, chunk in stream:
                    item = {
                        "msg_id": str(uuid.uuid4()),
                        "msg_type": "chat_stream",
                        "sender_id": self.local_agent_id,
                        "target_ids": [sender],
                        "api_key": self.api_key,
                        "payload": {"request_id": request_id, "label": label, "chunk": chunk},
                        "ttl": 1,
                    }
                    if not writer.is_closing():
                        writer.write(MeshProtocol.encode(item))
                        await writer.drain()
                done = {
                    "msg_id": str(uuid.uuid4()),
                    "msg_type": "chat_done",
                    "sender_id": self.local_agent_id,
                    "target_ids": [sender],
                    "api_key": self.api_key,
                    "payload": {"request_id": request_id, "status": "done", "provider": ""},
                    "ttl": 1,
                }
                if not writer.is_closing():
                    writer.write(MeshProtocol.encode(done))
                    await writer.drain()
            except Exception as exc:
                fail = {
                    "msg_id": str(uuid.uuid4()),
                    "msg_type": "chat_done",
                    "sender_id": self.local_agent_id,
                    "target_ids": [sender],
                    "api_key": self.api_key,
                    "payload": {
                        "request_id": request_id,
                        "status": "error",
                        "error": str(exc)[:300],
                        "provider": "",
                    },
                    "ttl": 1,
                }
                try:
                    if not writer.is_closing():
                        writer.write(MeshProtocol.encode(fail))
                        await writer.drain()
                except Exception as wexc:
                    print(f"[Mesh] chat error-reply to {sender} also failed: {wexc}")

        asyncio.get_event_loop().create_task(_stream_out())

    async def _handle_chat_stream(self, message: dict, writer: asyncio.StreamWriter):
        payload = message.get("payload", {})
        request_id = payload.get("request_id", "")
        cb = self._chat_streams.get(request_id)
        if cb:
            try:
                cb(payload.get("label", ""), payload.get("chunk", ""))
            except Exception as exc:
                print(f"[Mesh] chat stream callback for {request_id} failed: {exc}")

    async def _handle_chat_done_event(self, message: dict, writer: asyncio.StreamWriter):
        payload = message.get("payload", {})
        request_id = payload.get("request_id", "")
        fut = self._chat_done.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    async def send_brain_chat(
        self,
        target_id: str,
        message: str,
        on_event: callable,
        timeout_s: float = 300.0,
        wait_bg: bool = False,
        surface: str = "",
    ) -> dict:
        """Send a chat to the brain node and stream (label, chunk) events back.

        on_event(label, chunk) fires for every stream event (label is the
        provider or 'tool'). wait_bg asks the brain to await background-
        dispatched jobs (bounded) and answer inline instead of handing off.
        Returns the chat_done payload:
        {request_id, status: done|error|no_brain, error?}.
        """
        request_id = str(uuid.uuid4())[:8]
        self._chat_streams[request_id] = on_event
        fut = asyncio.get_event_loop().create_future()
        self._chat_done[request_id] = fut

        req = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "chat_request",
            "sender_id": self.local_agent_id,
            "target_ids": [target_id],
            "api_key": self.api_key,
            "payload": {
                "request_id": request_id,
                "message": message,
                "wait_bg": wait_bg,
                "surface": surface,
            },
            "ttl": 2,
        }
        try:
            sent = await self.send_raw(req)
            if not sent:
                return {"request_id": request_id, "status": "not_connected"}
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return {"request_id": request_id, "status": "timeout"}
        finally:
            self._chat_streams.pop(request_id, None)
            self._chat_done.pop(request_id, None)

    async def _handle_passthrough(self, message: dict, writer: asyncio.StreamWriter):
        """Observer already received it in _process_message; nothing else to do."""
        # DistributedBrain._handle_inference_request runs via the observer.
        pass

    async def _handle_inference_request(self, message: dict, writer: asyncio.StreamWriter):
        """Execute a federated inference request via the LLM router.

        The observer (DistributedBrain) also sees it; this handler performs
        the actual streaming inference and replies with the full text.
        """
        sender = message.get("sender_id", "")
        payload = message.get("payload", {})
        request_id = payload.get("request_id", "")
        query = payload.get("query", "")
        task_type = payload.get("task_type", "general")

        if not query or self.inference_fn is None:
            return  # observer handles pure-relay nodes; nothing more to do here

        try:
            response = ""
            async for _, chunk in self.inference_fn(query, task_type):
                if isinstance(chunk, str):
                    response += chunk
            status, result = "completed", response
        except Exception as e:
            status, result = "failed", str(e)

        reply = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "inference_response",
            "sender_id": self.local_agent_id,
            "target_ids": [sender],
            "api_key": self.api_key,
            "payload": {
                "request_id": request_id,
                "status": status,
                "response": result,
                "provider": self.local_agent_id,
            },
            "ttl": 1,
        }
        if not writer.is_closing():
            try:
                writer.write(MeshProtocol.encode(reply))
                await writer.drain()
            except Exception as exc:
                print(f"[Mesh] inference reply to {sender} failed: {exc}")

    async def start(self):
        if self.running:
            return

        # Fail closed: with an empty api_key, _process_message skips auth
        # entirely, and we bind 0.0.0.0 by default — i.e. the whole LAN could
        # submit toolbox-execution tasks. The orchestrator always generates
        # MESH_API_KEY before constructing us; any other path must too.
        if not self.api_key:
            print(
                "[Mesh] REFUSING to bind without an API key — mesh disabled "
                "(orchestrator generates MESH_API_KEY into .env)"
            )
            return

        self.running = True
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                self.host,
                self.port,
            )
        except OSError as e:
            print(f"[Mesh] could not bind {self.host}:{self.port} ({e}); mesh disabled")
            self.running = False
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._gossip_task = asyncio.create_task(self._gossip_loop())

        print(f"[Mesh] {self.name} started on {self.host}:{self.port}")

    async def stop(self):
        self.running = False

        for task in (self._heartbeat_task, self._cleanup_task, self._gossip_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    print(f"[Mesh] background task raised during shutdown: {exc}")

        if hasattr(self, "_server") and self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception as exc:
                print(f"[Mesh] server socket did not close cleanly: {exc}")

        for writer in list(self.peer_connections.values()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass  # reset connections must not crash shutdown
        self.peer_connections.clear()
        self.peers.clear()

        print("[Mesh] Stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer_addr = writer.get_extra_info("peername")
        print(f"[Mesh] New connection from {peer_addr}")

        try:
            while True:
                # Read fixed 16-byte header: magic(11) + version(1) + length(4)
                header = await reader.readexactly(MeshProtocol.HEADER_LEN)
                if not header.startswith(MeshProtocol.MAGIC):
                    print(f"[Mesh] dropping {peer_addr}: bad frame magic (not a mesh peer?)")
                    break
                version = header[len(MeshProtocol.MAGIC)]
                if version != MeshProtocol.VERSION:
                    print(
                        f"[Mesh] dropping {peer_addr}: protocol v{version} != v{MeshProtocol.VERSION}"
                    )
                    break
                length = int.from_bytes(
                    header[len(MeshProtocol.MAGIC) + 1 : MeshProtocol.HEADER_LEN], "big"
                )
                payload = await reader.readexactly(length)

                message = json.loads(payload.decode("utf-8"))
                await self._process_message(message, writer)

        except asyncio.CancelledError:
            pass
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass  # peer disconnected — normal mesh life
        except Exception as e:
            print(f"[Mesh] Connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            # Forget this writer if we registered it
            for pid, w in list(self.peer_connections.items()):
                if w is writer:
                    del self.peer_connections[pid]
                    self.peers.pop(pid, None)
                    self._auth_rejected.discard(pid)

    def _reject_auth_failure(self, msg_type: str, sender_id: str, writer: asyncio.StreamWriter):
        """Handle a peer that doesn't know our API key.

        Log once per sender (heartbeats repeat every 30s — no spam), then
        drop the connection so a stale/mismatched node stops flooding us.
        """
        if sender_id not in self._auth_rejected:
            self._auth_rejected.add(sender_id)
            print(f"[Mesh] rejected {msg_type} from {sender_id}: bad api key — closing connection")
        with contextlib.suppress(Exception):
            # Fail-soft by design: the writer may already be closing.
            writer.close()

    async def _process_message(self, message: dict, writer: asyncio.StreamWriter):
        msg_type = message.get("msg_type")
        sender_id = message.get("sender_id", "")

        # Auth: reject traffic from nodes that don't know our API key.
        # 'hello' and 'ping' carry the key for the handshake itself.
        if self.api_key:
            offered = message.get("api_key", "")
            if (msg_type not in ("hello", "ping") and offered != self.api_key) or (
                msg_type in ("hello", "ping") and offered and offered != self.api_key
            ):
                self._reject_auth_failure(msg_type, sender_id, writer)
                return

        # Let observers see all authenticated traffic
        if self.incoming_observer is not None:
            try:
                res = self.incoming_observer(message)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                print(f"[Mesh] incoming observer failed on {msg_type} from {sender_id}: {exc}")

        handler = self.message_handlers.get(msg_type)
        if handler:
            await handler(message, writer)
        else:
            await self._forward_message(message, writer)

    async def _forward_message(self, message: dict, exclude_writer: asyncio.StreamWriter = None):
        ttl = message.get("ttl", 3)
        if ttl <= 0:
            return

        message["ttl"] = ttl - 1
        message["path"] = message.get("path", []) + [self.local_agent_id]

        data = MeshProtocol.encode(message)

        for peer_id, writer in list(self.peer_connections.items()):
            if writer is not exclude_writer and not writer.is_closing():
                try:
                    writer.write(data)
                    await writer.drain()
                except Exception as exc:
                    print(f"[Mesh] forward to {peer_id} failed: {exc}")

    # Message handlers
    async def _handle_ping(self, message: dict, writer: asyncio.StreamWriter):
        pong = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "pong",
            "sender_id": self.local_agent_id,
            "target_ids": [message.get("sender_id", "")],
            "api_key": self.api_key,
            "payload": {"timestamp": datetime.now().isoformat()},
            "ttl": 1,
        }
        writer.write(MeshProtocol.encode(pong))
        await writer.drain()

    async def _handle_pong(self, message: dict, writer: asyncio.StreamWriter):
        pass

    async def _handle_capabilities(self, message: dict, writer: asyncio.StreamWriter):
        caps = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "capabilities",
            "sender_id": self.local_agent_id,
            "target_ids": [message.get("sender_id", "")],
            "api_key": self.api_key,
            "correlation_id": message.get("correlation_id", ""),
            "payload": {
                "agent_id": self.local_agent_id,
                "name": self.name,
                "capabilities": self.capabilities,
            },
            "ttl": 1,
        }
        writer.write(MeshProtocol.encode(caps))
        await writer.drain()

    async def _handle_task(self, message: dict, writer: asyncio.StreamWriter):
        sender = message.get("sender_id", "")
        payload = message.get("payload", {})
        task_id = payload.get("task_id", "")
        action = payload.get("action") or payload.get("task_type", "")
        args = payload.get("args", {})

        # Execute through the attached toolbox executor
        if self.task_executor is not None and action:
            try:
                result_text = await self._run_task_executor(action, args)
                status = "completed"
                result = {
                    "status": "completed",
                    "task_id": task_id,
                    "result": result_text,
                    "responder": self.name,
                }
            except Exception as e:
                status = "failed"
                result = {
                    "status": "failed",
                    "task_id": task_id,
                    "error": str(e),
                    "responder": self.name,
                }
            print(f"[Mesh] task {task_id} from {sender} -> {status}")
        else:
            # No executor attached (bare mesh node / test) — acknowledge only
            result = {"status": "received", "task_id": task_id, "responder": self.name}

        response = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "result",
            "sender_id": self.local_agent_id,
            "target_ids": [sender],
            "api_key": self.api_key,
            "payload": result,
            "ttl": 1,
        }
        if not writer.is_closing():
            try:
                writer.write(MeshProtocol.encode(response))
                await writer.drain()
            except Exception:
                pass

    async def _run_task_executor(self, action: str, args) -> str:
        """Call the attached executor with either (action, args_json) or (action, args_dict)."""
        import inspect

        if isinstance(args, dict):
            if len(inspect.signature(self.task_executor).parameters) >= 2:
                import json as _json

                return await self.task_executor(action, _json.dumps(args))
            return await self.task_executor(args)
        # args is a JSON string
        return await self.task_executor(action, args)

    async def _handle_result(self, message: dict, writer: asyncio.StreamWriter):
        """Deliver a task result to any local waiter keyed by task_id."""
        payload = message.get("payload", {})
        task_id = payload.get("task_id", "")
        fut = self._pending_results.get(task_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)
        # also let observers see results (already notified in _process_message)

    async def _handle_broadcast(self, message: dict, writer: asyncio.StreamWriter):
        await self._forward_message(message, writer)

    async def _handle_gossip(self, message: dict, writer: asyncio.StreamWriter):
        sender = message.get("sender_id", "")
        peers = message.get("payload", {}).get("peers", [])
        for peer_info in peers:
            peer_id = peer_info.get("agent_id")
            if peer_id and peer_id != self.local_agent_id:
                # Track/refresh the peer record
                self.peers[peer_id] = {
                    "agent_id": peer_id,
                    "name": peer_info.get("name", "Unknown"),
                    "host": peer_info.get("host", "unknown"),
                    "port": peer_info.get("port", 0),
                    "capabilities": peer_info.get("capabilities", {}),
                    "last_seen": datetime.now().isoformat(),
                }
                # Connect if not already linked. Tie-break mutual-dial races:
                # only the lexicographically smaller agent id dials.
                should_dial = (
                    peer_id not in self.peer_connections
                    and peer_id != sender
                    and peer_id not in self._dialing_lock
                    and self.local_agent_id < peer_id
                )
                host = peer_info.get("host")
                if should_dial and host and host not in ("0.0.0.0", "", "unknown"):
                    self._dialing_lock.add(peer_id)
                    task = asyncio.create_task(self._connect_to_peer(peer_info))
                    task.add_done_callback(lambda _, pid=peer_id: self._dialing_lock.discard(pid))

        # Respond with OUR known peers so the sender learns too
        gossip_response = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "gossip",
            "sender_id": self.local_agent_id,
            "target_ids": [sender],
            "api_key": self.api_key,
            "payload": {
                "peers": [
                    {
                        "agent_id": pid,
                        "name": info.get("name", "Unknown"),
                        "host": info.get("host", "unknown"),
                        "port": info.get("port", 0),
                    }
                    for pid, info in self.peers.items()
                ]
                + [
                    {
                        "agent_id": self.local_agent_id,
                        "name": self.name,
                        "host": self.advertised_host,
                        "port": self.port,
                    }
                ]
            },
            "ttl": 1,
        }
        if not writer.is_closing():
            try:
                writer.write(MeshProtocol.encode(gossip_response))
                await writer.drain()
            except Exception as exc:
                print(f"[Mesh] gossip response to {sender} failed: {exc}")

    async def _handle_hello(self, message: dict, writer: asyncio.StreamWriter):
        peer_id = message.get("sender_id")
        if peer_id and peer_id != self.local_agent_id:
            peer_info = message.get("payload", {})
            self.peers[peer_id] = {
                "agent_id": peer_id,
                "name": peer_info.get("sender_name", "Unknown"),
                "host": peer_info.get("sender_host", "unknown"),
                "port": peer_info.get("sender_port", 0),
                "capabilities": peer_info.get("capabilities", {}),
                "last_seen": datetime.now().isoformat(),
            }
            if peer_id not in self.peer_connections:
                self.peer_connections[peer_id] = writer
            print(f"[Mesh] New peer registered: {self.peers[peer_id]['name']} ({peer_id})")

    async def _connect_to_peer(self, peer_info: dict) -> bool:
        peer_id = peer_info.get("agent_id")
        host = peer_info.get("host")
        port = peer_info.get("port")

        if (
            not host
            or not port
            or peer_id in self.peer_connections
            or peer_id == self.local_agent_id
        ):
            return False

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=5.0,
            )

            self.peer_connections[peer_id] = writer

            hello = {
                "msg_id": str(uuid.uuid4()),
                "msg_type": "hello",
                "sender_id": self.local_agent_id,
                "target_ids": [peer_info["agent_id"]],
                "api_key": self.api_key,
                "payload": {
                    "sender_name": self.name,
                    "sender_host": self.host,
                    "sender_port": self.port,
                    "capabilities": self.capabilities,
                },
                "ttl": 1,
            }
            writer.write(MeshProtocol.encode(hello))
            await writer.drain()

            asyncio.create_task(self._handle_connection(reader, writer))

            return True
        except Exception as e:
            print(f"[Mesh] Failed to connect to {peer_id}: {e}")
            return False

    async def _handle_heartbeat(self, message: dict, writer: asyncio.StreamWriter):
        """Peer liveness signal — refresh peer record."""
        sender = message.get("sender_id", "")
        if sender and sender in self.peers:
            self.peers[sender]["last_seen"] = datetime.now().isoformat()

    async def _heartbeat_loop(self):
        while self.running:
            await asyncio.sleep(30)

            heartbeat = {
                "msg_id": str(uuid.uuid4()),
                "msg_type": "heartbeat",
                "sender_id": self.local_agent_id,
                "target_ids": [],
                "api_key": self.api_key,
                "payload": {"timestamp": datetime.now().isoformat()},
                "ttl": 1,
            }

            data = MeshProtocol.encode(heartbeat)
            for peer_id, writer in list(self.peer_connections.items()):
                if not writer.is_closing():
                    try:
                        writer.write(data)
                        await writer.drain()
                    except Exception as exc:
                        print(f"[Mesh] heartbeat to {peer_id} failed: {exc}")

    async def _cleanup_loop(self):
        while self.running:
            await asyncio.sleep(60)

            dead = []
            for peer_id, writer in list(self.peer_connections.items()):
                if writer.is_closing():
                    dead.append(peer_id)

            for peer_id in dead:
                del self.peer_connections[peer_id]
                self.peers.pop(peer_id, None)

    async def _gossip_loop(self):
        """Periodically exchange known-peer lists so the mesh self-heals
        and new nodes are found transitively."""
        interval = 30
        try:
            # Config can override the interval via attribute set by the host
            interval = int(getattr(self, "gossip_interval", 30) or 30)
        except (TypeError, ValueError) as exc:
            print(f"[Mesh] invalid gossip_interval {self.gossip_interval!r} ({exc}); using 30s")
        while self.running:
            await asyncio.sleep(interval)
            if not self.peer_connections:
                continue

            peers_snapshot = [
                {
                    "agent_id": pid,
                    "name": info.get("name", "Unknown"),
                    "host": info.get("host", "unknown"),
                    "port": info.get("port", 0),
                }
                # Advertise ourselves + known peers
                for pid, info in self.peers.items()
            ]
            me = {
                "agent_id": self.local_agent_id,
                "name": self.name,
                "host": self.advertised_host,
                "port": self.port,
            }
            all_peers = [me] + peers_snapshot

            gossip = {
                "msg_id": str(uuid.uuid4()),
                "msg_type": "gossip",
                "sender_id": self.local_agent_id,
                "target_ids": [],
                "api_key": self.api_key,
                "payload": {"peers": all_peers},
                "ttl": 1,
            }
            await self._broadcast(gossip)

    async def _broadcast(self, message: dict):
        # Auth: every outbound message must carry our key, or peers that
        # require one will reject (and now drop) the connection.
        if self.api_key:
            message.setdefault("api_key", self.api_key)
        data = MeshProtocol.encode(message)
        for peer_id, writer in list(self.peer_connections.items()):
            if not writer.is_closing():
                try:
                    writer.write(data)
                    await writer.drain()
                except Exception as exc:
                    print(f"[Mesh] broadcast to {peer_id} failed: {exc}")

    async def send_raw(self, message: dict) -> bool:
        """Send a raw protocol message to its first live target peer."""
        if self.api_key:
            message.setdefault("api_key", self.api_key)
        targets = message.get("target_ids") or []
        for target_id in targets:
            writer = self.peer_connections.get(target_id)
            if writer and not writer.is_closing():
                try:
                    writer.write(MeshProtocol.encode(message))
                    await writer.drain()
                    return True
                except Exception as exc:
                    print(f"[Mesh] send_raw to {target_id} failed: {exc}")
                    continue
        return False

    async def send_task(self, target_id: str, task: dict) -> bool:
        if target_id not in self.peer_connections:
            return False
        message = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "task",
            "sender_id": self.local_agent_id,
            "target_ids": [target_id],
            "api_key": self.api_key,
            "payload": task,
            "ttl": 3,
        }
        writer = self.peer_connections[target_id]
        try:
            writer.write(MeshProtocol.encode(message))
            await writer.drain()
            return True
        except Exception as exc:
            print(f"[Mesh] send_task to {target_id} failed: {exc}")
            return False

    async def send_task_and_wait(
        self, target_id: str, action: str, args: dict, timeout_s: float = 60.0
    ) -> dict:
        """Send a toolbox task to a peer and await the result payload."""
        task_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending_results[task_id] = fut

        task = {"task_id": task_id, "action": action, "args": args}
        try:
            sent = await self.send_task(target_id, task)
            if not sent:
                self._pending_results.pop(task_id, None)
                return {"status": "failed", "error": f"peer {target_id} not connected"}
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "error": f"peer {target_id} did not answer in {timeout_s}s",
            }
        finally:
            self._pending_results.pop(task_id, None)

    async def broadcast(self, message: dict, exclude: list[str] = None):
        # Copy: mutating a caller-provided list would leak our id into it.
        exclude = list(exclude or [])
        exclude.append(self.local_agent_id)

        if self.api_key:
            message.setdefault("api_key", self.api_key)
        message["target_ids"] = [pid for pid in self.peer_connections if pid not in exclude]

        if message["target_ids"]:
            data = MeshProtocol.encode(message)
            for pid, writer in list(self.peer_connections.items()):
                if pid not in exclude:
                    try:
                        writer.write(data)
                        await writer.drain()
                    except Exception as exc:
                        print(f"[Mesh] broadcast to {pid} failed: {exc}")

    async def request_capabilities(self, target_id: str) -> dict:
        if target_id not in self.peer_connections:
            return {}

        future = asyncio.get_event_loop().create_future()
        correlation_id = str(uuid.uuid4())

        def handle_caps(msg, writer):
            if msg.get("correlation_id") == correlation_id and not future.done():
                future.set_result(msg.get("payload", {}))

        # Store handler temporarily
        self.message_handlers["capabilities"] = handle_caps

        msg = {
            "msg_id": str(uuid.uuid4()),
            "msg_type": "capabilities",
            "sender_id": self.local_agent_id,
            "target_ids": [target_id],
            "api_key": self.api_key,
            "payload": {},
            "correlation_id": correlation_id,
            "ttl": 2,
        }

        writer = self.peer_connections.get(target_id)
        if writer:
            writer.write(MeshProtocol.encode(msg))
            await writer.drain()

        try:
            return await asyncio.wait_for(future, timeout=5.0)
        except asyncio.TimeoutError:
            print(f"[Mesh] capabilities request to {target_id} timed out")
            return {}
        finally:
            # Restore default capabilities handler
            self.message_handlers["capabilities"] = self._handle_capabilities

    def get_mesh_status(self) -> dict:
        return {
            "local_agent_id": self.local_agent_id,
            "name": self.name,
            "port": self.port,
            "peers": len(self.peer_connections),
            "peer_ids": list(self.peer_connections.keys()),
            "running": self.running,
        }

"""Unified brain proxy: routes chat from the API server to the CLI brain
over the A2A mesh. One conversation, one memory, one set of tools — the
child process serves HTTP while the CLI process thinks.

The child keeps its own (read-mostly) orchestrator for local endpoints
(chain/status/metrics/memory reads share the same SQLite files), but all
chat traffic is forwarded to the CLI brain when reachable. If the brain
is unreachable, it degrades gracefully to its local process() pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib

from core.agent_mesh import MeshNetwork
from core.logging_config import get_logger

logger = get_logger("unified_brain")

# The CLI brain binds the mesh on this port (config a2a.port).
BRAIN_MESH_PORT = 8082
RECONNECT_INTERVAL = 5.0


class UnifiedBrainProxy:
    def __init__(self, local_orch):
        self.local = local_orch
        self.mesh: MeshNetwork | None = None
        self.brain_id: str | None = None
        self.connected = False
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ── lifecycle ─────────────────────────────────────────────────

    async def start(self, api_key: str = ""):
        """Client-only mesh node: dials the CLI brain's mesh server."""
        if not self.local.cfg.a2a.enabled:
            logger.info("a2a disabled; child uses its local brain")
            return
        self.mesh = MeshNetwork(
            local_agent_id=f"api-{id(self) % 100000:05d}",
            name="JARVIS-API",
            host="127.0.0.1",
            port=0,  # client-only: no server bind
            api_key=api_key or self.local.cfg.a2a.api_key,
            capabilities={"role": "api-frontend"},
        )
        self._task = asyncio.create_task(self._maintain_connection())
        logger.info("unified brain proxy started (dialing CLI on :%s)", BRAIN_MESH_PORT)

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self.mesh:
            await self.mesh.stop()

    async def _maintain_connection(self):
        """Keep a connection to the CLI brain alive, redialing on loss."""
        while not self._stopping:
            try:
                ok = await self.mesh._connect_to_peer(
                    {
                        "agent_id": "jarvis-cli",
                        "host": "127.0.0.1",
                        "port": BRAIN_MESH_PORT,
                        "name": "JARVIS-CLI",
                    }
                )
                if ok:
                    # The CLI's hello handler registers us under our real id;
                    # track the peer id under which we can reach it.
                    self.connected = True
                    self.brain_id = "jarvis-cli"
                    logger.info("connected to CLI brain")
                    # Wait for the connection to drop (writer closed)
                    while not self._stopping:
                        w = self.mesh.peer_connections.get(self.brain_id)
                        if w is None or w.is_closing():
                            break
                        await asyncio.sleep(1.0)
                    self.connected = False
                    logger.info("CLI brain connection lost; redialing")
                else:
                    await asyncio.sleep(RECONNECT_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("brain dial failed: %s", exc)
                await asyncio.sleep(RECONNECT_INTERVAL)

    # ── chat relay ────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return (
            self.connected
            and self.brain_id is not None
            and self.brain_id in (self.mesh.peer_connections if self.mesh else {})
        )

    async def chat_stream(self, message: str, wait_bg: bool = False, surface: str = "web"):
        """Yield (label, chunk) from the unified CLI brain.

        Falls back to the child's local pipeline when the brain is not
        reachable, so chat NEVER hard-fails.
        """
        if not self.available:
            logger.info("brain unreachable; using local fallback")
            async for label, chunk in self.local.process(message, wait_bg=wait_bg):
                yield label, chunk
            return

        events: asyncio.Queue = asyncio.Queue()

        def on_event(label: str, chunk: str):
            events.put_nowait((label, chunk))

        # send_brain_chat blocks until chat_done; run it in a task while we
        # drain the event queue.
        send_task = asyncio.create_task(
            self.mesh.send_brain_chat(
                self.brain_id, message, on_event, wait_bg=wait_bg, surface=surface
            )
        )

        try:
            while True:
                # Wait for either a stream event or completion.
                get_ev = asyncio.create_task(events.get())
                try:
                    await asyncio.wait(
                        [get_ev, send_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    get_ev.cancel()
                    raise

                if get_ev.done() and not get_ev.cancelled():
                    try:
                        label, chunk = get_ev.result()
                    except asyncio.CancelledError:
                        pass
                    else:
                        yield label, chunk
                    get_ev.cancel()
                    continue

                if send_task.done():
                    if not get_ev.done():
                        get_ev.cancel()
                    # drain any remaining queued events
                    while not events.empty():
                        label, chunk = events.get_nowait()
                        yield label, chunk
                    if not send_task.cancelled():
                        try:
                            result = send_task.result()
                        except Exception as exc:
                            logger.warning("send_brain_chat raised: %s", exc)
                            result = None
                        if isinstance(result, dict) and result.get("status") == "no_brain":
                            logger.warning("CLI reports no brain attached; local fallback")
                            async for label, chunk in self.local.process(
                                message, wait_bg=wait_bg, surface=surface
                            ):
                                yield label, chunk
                    return
        finally:
            if not send_task.done():
                send_task.cancel()
            if not get_ev.done():
                get_ev.cancel()

    def stats(self) -> dict:
        return {
            "unified": self.available,
            "brain_id": self.brain_id,
        }

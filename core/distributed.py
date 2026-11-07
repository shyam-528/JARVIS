from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field

from core.agent_mesh import MeshNetwork
from core.config import Config
from core.providers import OpenAICompatClient
from core.router import FailoverRouter


@dataclass
class RemoteProvider:
    """A remote provider from another JARVIS instance."""

    agent_id: str
    name: str
    models: list[str]
    capabilities: list[str]
    latency_ms: float = 0
    last_ping: float = 0
    healthy: bool = True
    token_budget: int = 1_000_000
    used_tokens: int = 0


@dataclass
class FederatedRequest:
    """A request routed to the distributed brain."""

    id: str
    query: str
    task_type: str
    preferred_capabilities: list[str]
    created: float = field(default_factory=time.time)
    assigned_agent: str | None = None
    result: str | None = None
    status: str = "pending"  # pending, assigned, completed, failed


class DistributedBrain:
    """Distributed brain: P2P model sharing, federated inference via A2A mesh.

    Simple implementation using mesh broadcast for capability discovery
    and direct task routing for federated inference.
    """

    def __init__(
        self, router: FailoverRouter, client: OpenAICompatClient, config: Config, mesh: MeshNetwork
    ):
        self.router = router
        self.client = client
        self.config = config
        self.mesh = mesh

        # Remote providers discovered via mesh
        self.remote_providers: dict[str, RemoteProvider] = {}
        self.local_capabilities = self._get_local_capabilities()

        # Federated request queue
        self.pending_requests: dict[str, FederatedRequest] = {}
        # In-flight remote inference futures, keyed by request_id
        self._inflight_inference: dict[str, asyncio.Future] = {}

        # Routing policy
        self.prefer_local = True
        self.max_remote_latency = 5000  # ms

        self._started = False

    def _get_local_capabilities(self) -> dict:
        """Get local model capabilities for advertising."""
        models = []
        for p in self.router.providers:
            models.append(
                {
                    "name": p.model,
                    "provider": p.name,
                    "supports_tools": p.supports_tools,
                    "tags": getattr(p, "tags", []),
                    "budget": p.daily_token_budget,
                }
            )
        return {
            "agent_id": self.mesh.local_agent_id,
            "models": models,
            "capabilities": ["text", "reasoning", "coding", "memory", "rag", "tools"],
        }

    async def start(self):
        """Start the distributed brain."""
        if self._started:
            return
        self._started = True

        # Advertise local capabilities
        await self._advertise_capabilities()

        # Start background tasks
        asyncio.create_task(self._periodic_capability_sync())
        asyncio.create_task(self._health_monitor())
        asyncio.create_task(self._process_federated_queue())

    async def _advertise_capabilities(self):
        """Advertise local capabilities to mesh."""
        with contextlib.suppress(Exception):
            await self.mesh.broadcast(
                {
                    "msg_id": str(uuid.uuid4()),
                    "msg_type": "capability_advertisement",
                    "sender_id": self.mesh.local_agent_id,
                    "api_key": self.mesh.api_key,
                    "payload": self.local_capabilities,
                    "timestamp": time.time(),
                }
            )

    def attach_mesh_observer(self, mesh) -> None:
        """Bridge all incoming mesh traffic into the distributed brain.

        The mesh calls the observer for every authenticated message it
        receives, letting us track capability advertisements, answer
        inference requests, and collect inference responses.
        """
        mesh.set_incoming_observer(self._on_mesh_message)

    async def _on_mesh_message(self, message: dict) -> None:
        """Entry point from the mesh observer — dispatch to existing handlers."""
        try:
            await self._handle_incoming_message(message)
        except Exception as e:
            print(f"[Distributed] message handling error: {e}")

    async def _handle_incoming_message(self, message: dict):
        """Handle incoming mesh messages for distributed inference."""
        msg_type = message.get("msg_type", "")
        payload = message.get("payload", {})
        source = message.get("sender_id", "")

        if msg_type == "capability_advertisement":
            self._handle_capability_advertisement(payload, source)
        elif msg_type == "inference_request":
            await self._handle_inference_request(payload, source)
        elif msg_type == "inference_response":
            self._handle_inference_response(payload)
        elif msg_type == "health_check":
            self._handle_health_check(payload, source)

    def _handle_capability_advertisement(self, payload: dict, source: str):
        """Handle capability advertisement from peer."""
        if source == self.mesh.local_agent_id:
            return

        for model_info in payload.get("models", []):
            provider_key = f"{source}:{model_info['provider']}:{model_info['name']}"

            if provider_key not in self.remote_providers:
                self.remote_providers[provider_key] = RemoteProvider(
                    agent_id=source,
                    name=f"{source}:{model_info['name']}",
                    models=[model_info["name"]],
                    capabilities=model_info.get("tags", []),
                    token_budget=model_info.get("budget", 1000000),
                )
                print(f"[Distributed] Discovered remote model: {provider_key}")

    async def _handle_inference_request(self, payload: dict, source: str):
        """Handle inference request from another agent."""
        request_id = payload.get("request_id", "")
        query = payload.get("query", "")
        task_type = payload.get("task_type", "general")

        try:
            response = ""
            async for _, chunk in self.router.chat(
                [{"role": "user", "content": query}], None, task_type
            ):
                if isinstance(chunk, str):
                    response += chunk

            # Send response back
            await self.mesh.send_task(
                source,
                {
                    "type": "inference_response",
                    "request_id": request_id,
                    "response": response,
                    "provider": self.mesh.local_agent_id,
                },
            )
        except Exception as e:
            await self.mesh.send_task(
                source,
                {
                    "type": "inference_response",
                    "request_id": request_id,
                    "error": str(e),
                    "provider": self.mesh.local_agent_id,
                },
            )

    def _handle_inference_response(self, payload: dict):
        """Handle inference response from another agent (observer relay)."""
        request_id = payload.get("request_id", "")
        fut = self._inflight_inference.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)
        # Also service legacy pending_requests entries
        if request_id in self.pending_requests:
            req = self.pending_requests[request_id]
            if payload.get("status") == "completed":
                req.status = "completed"
                req.result = payload.get("response", "")
                req.assigned_agent = payload.get("provider")
            else:
                req.status = "failed"
                req.result = f"Error: {payload.get('response') or payload.get('error')}"

    def _handle_health_check(self, payload: dict, source: str):
        """Handle health check from peer."""
        now = time.time()
        for _key, provider in list(self.remote_providers.items()):
            if provider.agent_id == source:
                provider.last_ping = now
                provider.healthy = True

    async def _periodic_capability_sync(self):
        """Periodically re-advertise capabilities."""
        while True:
            await asyncio.sleep(300)  # 5 minutes
            await self._advertise_capabilities()

    async def _health_monitor(self):
        """Monitor remote provider health."""
        while True:
            await asyncio.sleep(60)  # 1 minute
            now = time.time()
            for _key, provider in list(self.remote_providers.items()):
                if now - provider.last_ping > 300:  # 5 min timeout
                    provider.healthy = False

    async def _process_federated_queue(self):
        """Process pending federated requests."""
        while True:
            await asyncio.sleep(1)
            for _req_id, req in list(self.pending_requests.items()):
                if req.status == "pending":
                    await self._route_federated_request(req)

    async def _route_federated_request(self, req: FederatedRequest):
        """Route a request to best available provider (local or remote)."""

        # Score local providers
        local_candidates = self.router.available()
        local_scores = []
        for p in local_candidates:
            score = self.router._task_score(p, req.task_type)
            if not self.router.breakers.get(p.name).allows():
                continue
            local_scores.append((score, p))

        # Score remote providers
        remote_scores = []
        for _key, rp in self.remote_providers.items():
            if not rp.healthy:
                continue
            if rp.used_tokens >= rp.token_budget:
                continue
            if rp.latency_ms > self.max_remote_latency:
                continue

            # Check capability match
            task_match = any(
                c in rp.capabilities for c in self._get_task_capabilities(req.task_type)
            )
            if not task_match:
                continue

            remote_scores.append((rp.latency_ms, rp))

        # Prefer local if good enough
        if local_scores and self.prefer_local:
            local_scores.sort(key=lambda x: x[0])
            best_local = local_scores[0][1]
            req.status = "assigned"
            req.assigned_agent = "local"

            # Process locally (async)
            asyncio.create_task(self._execute_local(req, best_local))
            return

        # Use remote
        if remote_scores:
            remote_scores.sort(key=lambda x: x[0])
            best_remote = remote_scores[0][1]
            req.status = "assigned"
            req.assigned_agent = best_remote.agent_id

            await self._send_remote_request(req, best_remote)
            return

        # Fallback: local even if busy
        if local_scores:
            local_scores.sort(key=lambda x: x[0])
            best_local = local_scores[0][1]
            req.status = "assigned"
            req.assigned_agent = "local"
            asyncio.create_task(self._execute_local(req, best_local))
        else:
            req.status = "failed"
            req.result = "No providers available"

    def _get_task_capabilities(self, task_type: str) -> list[str]:
        """Map task type to required capabilities."""
        mapping = {
            "coding": ["coding", "primary"],
            "reasoning": ["reasoning", "primary"],
            "vision": ["vision", "multimodal"],
            "fast": ["fast", "lightweight"],
        }
        return mapping.get(task_type, ["general"])

    async def _execute_local(self, req: FederatedRequest, provider):
        """Execute request locally."""
        try:
            response = ""
            async for _, chunk in self.router.chat(
                [{"role": "user", "content": req.query}], None, req.task_type
            ):
                if isinstance(chunk, str):
                    response += chunk
            req.result = response
            req.status = "completed"
        except Exception as e:
            req.status = "failed"
            req.result = f"Error: {e}"

    async def _send_remote_request(self, req: FederatedRequest, provider: RemoteProvider):
        """Send request to remote agent via mesh and await its response."""
        request_id = req.id

        # Ask the mesh to route an inference_request and await the reply
        fut = asyncio.get_event_loop().create_future()
        self._inflight_inference[request_id] = fut

        ok = await self.mesh.send_raw(
            {
                "msg_id": str(uuid.uuid4()),
                "msg_type": "inference_request",
                "sender_id": self.mesh.local_agent_id,
                "target_ids": [provider.agent_id],
                "api_key": self.mesh.api_key,
                "payload": {
                    "request_id": request_id,
                    "query": req.query,
                    "task_type": req.task_type,
                },
                "ttl": 2,
            }
        )
        if not ok:
            self._inflight_inference.pop(request_id, None)
            req.status = "failed"
            req.result = f"Send failed: peer {provider.agent_id} not connected"
            return

        try:
            payload = await asyncio.wait_for(fut, timeout=45.0)
            if payload.get("status") == "completed":
                req.status = "completed"
                req.result = payload.get("response", "")
                req.assigned_agent = payload.get("provider", provider.agent_id)
            else:
                req.status = "failed"
                req.result = f"Remote error: {payload.get('response') or payload.get('error')}"
        except asyncio.TimeoutError:
            req.status = "failed"
            req.result = "Remote inference timed out (45s)"
        finally:
            self._inflight_inference.pop(request_id, None)

    async def federated_query(
        self,
        query: str,
        task_type: str = "general",
        prefer_local: bool = True,
    ) -> str:
        """Main entry point for federated queries."""

        # Quick local attempt
        if self.prefer_local:
            local_candidates = self.router.available()
            if local_candidates:
                try:
                    response = ""
                    async for _, chunk in self.router.chat(
                        [{"role": "user", "content": query}], None, task_type
                    ):
                        if isinstance(chunk, str):
                            response += chunk
                    if response:
                        return response
                except Exception:
                    pass

        # Federate
        req = FederatedRequest(
            id=str(uuid.uuid4())[:8],
            query=query,
            task_type=task_type,
            preferred_capabilities=self._get_task_capabilities(task_type),
        )

        self.pending_requests[req.id] = req

        # Wait for completion (with timeout)
        start = time.time()
        timeout = 30.0

        while req.status == "pending" and time.time() - start < timeout:
            await asyncio.sleep(0.1)

        if req.status == "completed":
            return req.result or "No response"
        elif req.status == "failed":
            return f"Federated query failed: {req.result}"
        else:
            return "Federated query timed out"

    def get_stats(self) -> dict:
        """Get distributed brain stats."""
        return {
            "local_models": len(self.router.providers),
            "remote_providers": len(self.remote_providers),
            "healthy_remotes": sum(1 for p in self.remote_providers.values() if p.healthy),
            "pending_requests": len(
                [r for r in self.pending_requests.values() if r.status == "pending"]
            ),
            "completed_requests": len(
                [r for r in self.pending_requests.values() if r.status == "completed"]
            ),
        }

    def list_remote_models(self) -> list[dict]:
        """List all available remote models."""
        return [
            {
                "id": key,
                "agent_id": p.agent_id,
                "name": p.name,
                "models": p.models,
                "capabilities": p.capabilities,
                "latency_ms": p.latency_ms,
                "healthy": p.healthy,
                "budget_left": p.token_budget - p.used_tokens,
            }
            for key, p in self.remote_providers.items()
        ]

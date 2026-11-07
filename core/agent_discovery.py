"""A2A agent discovery: Zeroconf (mDNS) advertise/browse with an HTTP fallback.

Failure policy: discovery is best-effort. Individual service/peer failures
degrade gracefully (skip that service or peer, keep scanning) but are NEVER
silent — every swallowed failure prints a loud [A2A Discovery] diagnostic
naming the service, peer, or agent involved.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf


@dataclass
class DiscoveredAgent:
    agent_id: str
    name: str
    host: str
    port: int
    api_key: str = ""
    capabilities: dict = field(default_factory=dict)
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_seen: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "api_key": self.api_key,
            "capabilities": self.capabilities,
            "discovered_at": self.discovered_at,
            "last_seen": self.last_seen,
            "metadata": self.metadata,
        }

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class A2AServiceListener(ServiceListener):
    def __init__(self, callback: Callable[[DiscoveredAgent], None]):
        self.callback = callback
        self.zeroconf: Zeroconf | None = None

    def add_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        info = zeroconf.get_service_info(type_, name)
        if info:
            self._process_service_info(info)

    def update_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        info = zeroconf.get_service_info(type_, name)
        if info:
            self._process_service_info(info)

    def remove_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        pass  # Could notify removal

    def _process_service_info(self, info: ServiceInfo):
        try:
            properties = {
                k.decode(): v.decode() if isinstance(v, bytes) else v
                for k, v in (info.properties or {}).items()
            }

            agent_id = properties.get("agent_id", "")
            if not agent_id:
                return

            addresses = [f"{ip}" for ip in info.parsed_addresses(IPVersion.V4Only)]
            host = addresses[0] if addresses else socket.gethostbyname(info.server.rstrip("."))
            port = info.port

            agent = DiscoveredAgent(
                agent_id=agent_id,
                name=properties.get("name", "Unknown"),
                host=host,
                port=port,
                api_key=properties.get("api_key", ""),
                capabilities=json.loads(properties.get("capabilities", "{}")),
                metadata={
                    "service_name": info.name,
                    "server": info.server,
                    "properties": properties,
                },
            )
            self.callback(agent)
        except Exception as e:
            print(f"[A2A Discovery] Error processing service {getattr(info, 'name', '?')}: {e}")


class A2ADiscovery:
    SERVICE_TYPE = "_jarvis-a2a._tcp.local."

    def __init__(
        self,
        agent_id: str,
        name: str,
        port: int,
        api_key: str = "",
        capabilities: dict = None,
        on_discovered: Callable[[DiscoveredAgent], None] = None,
        interface: str = "",
    ):
        self.agent_id = agent_id
        self.name = name
        self.port = port
        self.api_key = api_key
        self.capabilities = capabilities or {}
        self.on_discovered = on_discovered

        self.zeroconf: Zeroconf | None = None
        self.service_info: ServiceInfo | None = None
        self.browser: ServiceBrowser | None = None
        self.running = False

        # Get local IP
        self.local_ip = self._get_local_ip()

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as exc:
            # fail-soft: offline/no-route host still advertises on loopback,
            # but say so loudly — peers cannot reach us at 127.0.0.1
            print(
                f"[A2A Discovery] could not determine local IP "
                f"({type(exc).__name__}: {exc}); falling back to 127.0.0.1"
            )
            return "127.0.0.1"

    def _create_service_info(self) -> ServiceInfo:
        properties = {
            "agent_id": self.agent_id,
            "name": self.name,
            "api_key": self.api_key,
            "capabilities": json.dumps(
                {
                    "skills": ["task_delegation", "knowledge_sharing"],
                    "version": "1.0.0",
                }
            ),
        }

        return ServiceInfo(
            type_=self.SERVICE_TYPE,
            name=f"{self.name}.{self.SERVICE_TYPE}",
            addresses=[socket.inet_aton(self.local_ip)],
            port=self.port,
            properties=properties,
            server=f"{self.agent_id}.local.",
        )

    def start(self):
        if self.running:
            return

        self.zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        self.service_info = self._create_service_info()

        # Register our service
        self.zeroconf.register_service(self.service_info)

        # Browse for other agents
        listener = A2AServiceListener(self._on_service_discovered)
        self.browser = ServiceBrowser(self.zeroconf, self.SERVICE_TYPE, listener)

        self.running = True
        print(
            f"[A2A Discovery] Started advertising as {self.name} ({self.agent_id}) on {self.local_ip}:{self.port}"
        )

    def _on_service_discovered(self, agent: DiscoveredAgent):
        if agent.agent_id != self.agent_id and self.on_discovered:
            self.on_discovered(agent)

    def stop(self):
        if not self.running:
            return

        if self.browser:
            self.browser.cancel()
        if self.service_info and self.zeroconf:
            self.zeroconf.unregister_service(self.service_info)
        if self.zeroconf:
            self.zeroconf.close()

        self.running = False
        print("[A2A Discovery] Stopped")


class A2ANetworkManager:
    """High-level network manager combining discovery, client connections, and mesh."""

    def __init__(
        self,
        agent_id: str,
        name: str,
        port: int = 8081,
        api_key: str = "",
        capabilities: dict = None,
    ):
        self.agent_id = agent_id
        self.name = name
        self.port = port
        self.api_key = api_key
        self.capabilities = capabilities or {}

        self.discovery: A2ADiscovery | None = None
        self.known_agents: dict[str, DiscoveredAgent] = {}
        self.clients: dict[str, Any] = {}  # A2AClient instances
        self._running = False

    async def start(self):
        # Start discovery
        self.discovery = A2ADiscovery(
            agent_id=self.agent_id,
            name=self.name,
            port=self.port,
            api_key=self.api_key,
            capabilities=self.capabilities,
            on_discovered=self._on_agent_discovered,
        )
        self.discovery.start()
        self._running = True
        print(f"[A2A Network] Started {self.name} ({self.agent_id})")

    def _on_agent_discovered(self, agent: DiscoveredAgent):
        if agent.agent_id in self.known_agents:
            # Update last seen
            self.known_agents[agent.agent_id].last_seen = datetime.now().isoformat()
            return

        self.known_agents[agent.agent_id] = agent
        print(
            f"[A2A Network] Discovered agent: {agent.name} ({agent.agent_id}) at {agent.host}:{agent.port}"
        )

    async def connect_to_agent(self, agent_id: str) -> bool:
        # TODO: Create A2AClient and connect to self.known_agents[agent_id]
        # This would use the A2AClient from a2a.py
        return agent_id in self.known_agents

    def get_known_agents(self) -> list[DiscoveredAgent]:
        return list(self.known_agents.values())

    def get_agent(self, agent_id: str) -> DiscoveredAgent | None:
        return self.known_agents.get(agent_id)

    def stop(self):
        if self.discovery:
            self.discovery.stop()
        self._running = False
        print("[A2A Network] Stopped")


# Simple HTTP-based discovery fallback (no zeroconf required)
class HTTPDiscovery:
    """Fallback discovery using HTTP endpoints."""

    def __init__(
        self,
        agent_id: str,
        name: str,
        port: int,
        api_key: str = "",
        known_peers: list[str] = None,
    ):
        self.agent_id = agent_id
        self.name = name
        self.port = port
        self.api_key = api_key
        self.known_peers = known_peers or []
        self.known_agents: dict[str, dict] = {}
        self._running = False
        self._scan_task: asyncio.Task | None = None

    async def start(self):
        self._running = True
        # Announce to known peers
        await self._announce_to_peers()
        # Start periodic scanning
        self._scan_task = asyncio.create_task(self._scan_loop())

    async def _announce_to_peers(self):
        for peer in self.known_peers:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{peer}/a2a/register",
                        json={
                            "agent_id": self.agent_id,
                            "name": self.name,
                            "port": self.port,
                            "api_key": "",
                        },
                        timeout=5.0,
                    )
            except Exception as exc:
                print(
                    f"[A2A Discovery] announce to peer {peer} failed: {type(exc).__name__}: {exc}"
                )

    async def _scan_loop(self):
        while self._running:
            await asyncio.sleep(60)  # Scan every minute
            for peer in self.known_peers:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get(f"{peer}/a2a/agents")
                        if resp.status_code == 200:
                            agents = resp.json().get("agents", [])
                            for agent_id in agents:
                                if agent_id not in self.known_agents:
                                    # Fetch full details
                                    await self._fetch_agent_details(peer, agent_id)
                except Exception as exc:
                    print(
                        f"[A2A Discovery] scan of peer {peer} failed: {type(exc).__name__}: {exc}"
                    )

    async def _fetch_agent_details(self, peer_url: str, agent_id: str):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{peer_url}/a2a/agents/{agent_id}")
                if resp.status_code == 200:
                    self.known_agents[agent_id] = resp.json()
        except Exception as exc:
            print(
                f"[A2A Discovery] fetch of agent '{agent_id}' from {peer_url} "
                f"failed: {type(exc).__name__}: {exc}"
            )

    def get_known_agents(self) -> list[dict]:
        return list(self.known_agents.values())

    def stop(self):
        self._running = False
        if self._scan_task is not None:
            self._scan_task.cancel()
            self._scan_task = None


def create_discovery(
    agent_id: str,
    name: str,
    port: int,
    api_key: str = "",
    use_zeroconf: bool = True,
    fallback_peers: list[str] = None,
) -> A2ADiscovery | HTTPDiscovery:
    """Factory to create appropriate discovery mechanism."""
    if use_zeroconf:
        try:
            return A2ADiscovery(
                agent_id=agent_id,
                name=name,
                port=port,
                api_key=api_key,
            )
        except Exception as e:
            print(f"[A2A Discovery] Zeroconf failed, falling back to HTTP: {e}")

    return HTTPDiscovery(
        agent_id=agent_id,
        name=name,
        port=port,
        api_key=api_key,
        known_peers=fallback_peers or [],
    )

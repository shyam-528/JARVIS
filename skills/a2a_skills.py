"""Agent-to-agent mesh tools: discover peers, delegate/query/broadcast tasks.

Registered only when the agent mesh is available; handlers return error
strings on mesh failures (fail-soft).
"""

from __future__ import annotations

import json
from typing import Any

from core.tools import Tool, ToolBox


def register_a2a(box: ToolBox, mesh: Any):
    async def a2a_discover_peers() -> str:
        """Discover peers on the A2A mesh network."""
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        peers = []
        for peer_id, peer_info in mesh.peers.items():
            peers.append(
                f"- {peer_info.get('name', 'Unknown')} ({peer_id}) at {peer_info.get('host', 'unknown')}:{peer_info.get('port', 0)}"
            )

        if not peers:
            return "No peers discovered on the mesh"

        return "Discovered peers:\n" + "\n".join(peers)

    async def a2a_send_task(
        target_agent: str,
        action: str,
        args: str = "{}",
        task_description: str = "",
    ) -> str:
        """Run a toolbox action on a remote JARVIS peer and return its result.

        action: any tool name the peer has (e.g. web_search, get_weather,
        search_knowledge). args: JSON object of the tool's parameters.
        """
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        if target_agent not in mesh.peer_connections:
            connected = ", ".join(mesh.peer_connections.keys()) or "none"
            return f"Target agent '{target_agent}' not connected (connected: {connected})"

        try:
            json.loads(args)  # validate
        except json.JSONDecodeError as e:
            return f"args must be valid JSON: {e}"

        result = await mesh.send_task_and_wait(target_agent, action, json.loads(args))
        status = result.get("status", "unknown")
        if status == "completed":
            out = result.get("result", "")
            return f"[{target_agent}] {out[:2000]}"
        return f"[{target_agent}] {status}: {result.get('error', '')}"

    async def a2a_broadcast(
        message: str,
        message_type: str = "broadcast",
        exclude: str = "",
    ) -> str:
        """Broadcast a message to all connected peers."""
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        exclude_list = [e.strip() for e in exclude.split(",")] if exclude else []

        await mesh.broadcast(
            {
                "msg_type": message_type,
                "sender_id": mesh.local_agent_id,
                "payload": {"message": message},
                "ttl": 3,
            },
            exclude=exclude_list,
        )

        return f"Broadcast sent to {len(mesh.peer_connections) - len(exclude_list)} peers"

    async def a2a_query_peer(
        target_agent: str,
        query: str,
    ) -> str:
        """Query another agent's knowledge."""
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        if target_agent not in mesh.peer_connections:
            return f"Target agent '{target_agent}' not connected"

        result = await mesh.request_capabilities(target_agent)
        if result:
            return f"Peer capabilities: {result}"
        return f"Failed to query {target_agent}"

    async def a2a_request_capabilities(target_agent: str) -> str:
        """Get capabilities of a peer agent."""
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        if target_agent not in mesh.peer_connections:
            return f"Target agent '{target_agent}' not connected"

        caps = await mesh.request_capabilities(target_agent)
        if caps:
            return f"Capabilities of {target_agent}:\n{json.dumps(caps, indent=2)}"
        return f"Failed to get capabilities from {target_agent}"

    async def a2a_mesh_status() -> str:
        """Get current mesh network status."""
        if not hasattr(mesh, "running"):
            return "Mesh network not initialized"

        status = mesh.get_mesh_status()
        lines = [
            f"Agent: {status['name']} ({status['local_agent_id']})",
            f"Port: {status['port']}",
            f"Status: {'Running' if status['running'] else 'Stopped'}",
            f"Connected peers: {status['peers']}",
        ]
        if status["peer_ids"]:
            lines.append("Connected peers: " + ", ".join(status["peer_ids"]))
        return "\n".join(lines)

    async def a2a_connect_peer(
        host: str,
        port: int,
        peer_id: str = "",
    ) -> str:
        """Manually connect to a peer."""
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        ok = await mesh._connect_to_peer(
            {
                "agent_id": peer_id or f"{host}:{port}",
                "host": host,
                "port": port,
                "name": f"{host}:{port}",
            }
        )
        if ok:
            return f"Connected to peer at {host}:{port}"
        return f"Failed to connect to {host}:{port} (unreachable or already connected)"

    async def a2a_send_message(
        target_agent: str,
        message: str,
        message_type: str = "chat",
    ) -> str:
        """Send a simple message to another agent."""
        if not hasattr(mesh, "running") or not mesh.running:
            return "Mesh network not running"

        if target_agent not in mesh.peer_connections:
            return f"Target agent '{target_agent}' not connected"

        await mesh.send_task(
            target_agent,
            {
                "task_type": "message",
                "description": message,
                "payload": {"text": message, "type": message_type},
            },
        )
        return f"Message sent to {target_agent}"

    box.register(
        Tool(
            name="a2a_discover",
            description="Discover peers on the A2A mesh network",
            parameters={"type": "object", "properties": {}},
            handler=a2a_discover_peers,
        )
    )
    box.register(
        Tool(
            name="a2a_task",
            description="Run a toolbox action on a remote JARVIS peer and get its result (e.g. web_search, get_weather, search_knowledge)",
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "Target agent ID (see a2a_status for connected peers)",
                    },
                    "action": {"type": "string", "description": "Tool name to run on the peer"},
                    "args": {
                        "type": "string",
                        "default": "{}",
                        "description": "JSON object of tool parameters",
                    },
                    "task_description": {
                        "type": "string",
                        "default": "",
                        "description": "Optional human note about the task",
                    },
                },
                "required": ["target_agent", "action"],
            },
            handler=a2a_send_task,
        )
    )
    box.register(
        Tool(
            name="a2a_broadcast",
            description="Broadcast a message to all connected peers",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "message_type": {"type": "string", "default": "broadcast"},
                    "exclude": {
                        "type": "string",
                        "description": "Comma-separated agent IDs to exclude",
                    },
                },
                "required": ["message"],
            },
            handler=a2a_broadcast,
        )
    )
    box.register(
        Tool(
            name="a2a_query",
            description="Query another agent's knowledge",
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["target_agent", "query"],
            },
            handler=a2a_query_peer,
        )
    )
    box.register(
        Tool(
            name="a2a_capabilities",
            description="Get capabilities of a peer agent",
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {"type": "string"},
                },
                "required": ["target_agent"],
            },
            handler=a2a_request_capabilities,
        )
    )
    box.register(
        Tool(
            name="a2a_status",
            description="Get mesh network status",
            parameters={"type": "object", "properties": {}},
            handler=a2a_mesh_status,
        )
    )
    box.register(
        Tool(
            name="a2a_connect",
            description="Manually connect to a peer",
            parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer"},
                    "peer_id": {"type": "string", "default": ""},
                },
                "required": ["host", "port"],
            },
            handler=a2a_connect_peer,
        )
    )
    box.register(
        Tool(
            name="a2a_message",
            description="Send a simple message to another agent",
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {"type": "string"},
                    "message": {"type": "string"},
                    "message_type": {"type": "string", "default": "chat"},
                },
                "required": ["target_agent", "message"],
            },
            handler=a2a_send_message,
        )
    )

"""Wave 7 failure-path tests for core.agent_discovery.

Contract: discovery is best-effort — individual peer/service failures degrade
gracefully but are NEVER silent (loud [A2A Discovery] print naming the peer).
"""

from __future__ import annotations

import socket

from core import agent_discovery
from core.agent_discovery import A2ADiscovery, HTTPDiscovery, create_discovery


def test_local_ip_fallback_is_loud(capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", boom)
    d = A2ADiscovery("agent-1", "jarvis", 9000)
    assert d.local_ip == "127.0.0.1"
    out = capsys.readouterr().out
    assert "no route" in out and "127.0.0.1" in out


def test_create_discovery_falls_back_loudly(capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("zeroconf broken")

    monkeypatch.setattr(agent_discovery, "A2ADiscovery", boom)
    d = create_discovery("a1", "jarvis", 9000, use_zeroconf=True)
    assert isinstance(d, HTTPDiscovery)
    assert "zeroconf broken" in capsys.readouterr().out


async def test_announce_failure_is_loud(capsys):
    d = HTTPDiscovery("a1", "jarvis", 9000, known_peers=["http://127.0.0.1:1"])
    await d._announce_to_peers()  # connection refused, fast
    out = capsys.readouterr().out
    assert "127.0.0.1:1" in out and "failed" in out


async def test_fetch_agent_details_failure_is_loud(capsys):
    d = HTTPDiscovery("a1", "jarvis", 9000)
    await d._fetch_agent_details("http://127.0.0.1:1", "agent-x")
    out = capsys.readouterr().out
    assert "agent-x" in out and "127.0.0.1:1" in out and "failed" in out


async def test_scan_loop_survives_peer_failure_loudly(capsys, monkeypatch):
    d = HTTPDiscovery("a1", "jarvis", 9000, known_peers=["http://127.0.0.1:1"])
    d._running = True

    # run one iteration then stop
    async def stop_soon(delay=0):
        d._running = False

    monkeypatch.setattr(agent_discovery.asyncio, "sleep", stop_soon)
    await d._scan_loop()
    out = capsys.readouterr().out
    assert "127.0.0.1:1" in out and "failed" in out

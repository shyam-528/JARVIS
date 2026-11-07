"""Web exercise: boot the real API (TestClient) and hit every endpoint index.html calls.

Usage: python scripts/web_exercise.py
Mint a token directly via AuthManager (same DB the app uses), revoke after.
Prints PASS/FAIL per endpoint. Exit 1 on any FAIL.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JARVIS_SERVICE_INSTANCE", "1")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main():
    from fastapi.testclient import TestClient

    import api.main as M

    with TestClient(M.app) as client:
        tok = M.auth_manager.create_token("web-exercise", 1)
        H = {"Authorization": f"Bearer {tok}"}

        r = client.get("/app")
        check("GET /app", r.status_code == 200 and "JARVIS" in r.text, str(r.status_code))
        r = client.get("/static/manifest.json")
        check("GET /static/manifest.json", r.status_code == 200, str(r.status_code))
        r = client.get("/static/sw.js")
        check("GET /static/sw.js", r.status_code == 200, str(r.status_code))
        for icon in ("icon-192.png", "icon-512.png", "mic-96.png", "eye-96.png", "briefing-96.png"):
            r = client.get(f"/static/icons/{icon}")
            check(f"GET /static/icons/{icon}", r.status_code == 200, str(r.status_code))

        gets = [
            "/api/status",
            "/api/health",
            "/api/chain",
            "/api/stats",
            "/api/agents",
            "/api/memory",
            "/api/tasks",
            "/api/automation",
            "/api/ml/stats",
            "/api/ml/suggestions",
            "/api/proactive",
            "/api/tools",
            "/api/plugins",
            "/api/workflows",
            "/api/workflows/runs",
            "/api/conversations",
            "/api/conversations/search?q=test",
            "/api/conversations/current",
            "/api/knowledge/stats",
            "/api/knowledge/sources",
            "/api/vision/monitors",
            "/api/vision/shots",
            "/api/voice/config",
            "/api/audio",
            "/api/web/usage",
            "/api/web/insights",
            "/api/web/settings",
            "/api/metrics",
            "/api/brains",
            "/api/telegram/status",
        ]
        for path in gets:
            try:
                r = client.get(path, headers=H)
                check(f"GET {path}", r.status_code == 200, str(r.status_code))
            except Exception as exc:
                check(f"GET {path}", False, f"{type(exc).__name__}: {exc}"[:120])

        # history search must NOT hit the {session_id} route (was 404 before fix)
        r = client.get("/api/conversations/search", params={"q": "zzz-no-match"}, headers=H)
        try:
            check("search shape is list", isinstance(r.json(), list), str(r.status_code))
        except Exception as exc:
            check("search shape is list", False, str(exc)[:100])

        # knowledge search round-trip
        r = client.post("/api/knowledge/search", json={"query": "test", "k": 2}, headers=H)
        check("POST /api/knowledge/search", r.status_code == 200, str(r.status_code))

        # voice TTS (Kokoro first now) — accept wav/mpeg or 503 fallback
        try:
            r = client.post("/api/voice/say", json={"text": "Web check."}, headers=H)
            ok = (r.status_code == 200 and len(r.content) > 1000) or r.status_code == 503
            check("POST /api/voice/say", ok, f"{r.status_code} {len(r.content)}b")
        except Exception as exc:
            check("POST /api/voice/say", False, str(exc)[:120])

        # unauthenticated API access stays rejected
        r = client.get("/api/status")
        check(
            "GET /api/status w/o token -> 401/403", r.status_code in (401, 403), str(r.status_code)
        )

        # cleanup test token
        try:
            M.auth_manager.revoke_token(tok)
            check("revoke test token", True)
        except Exception as exc:
            check("revoke test token", False, str(exc)[:100])

    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()

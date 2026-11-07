"""Offline public-web integration tests; run with unittest or pytest."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.public import COOKIE, Settings, context_messages, create_app, provider_reply
from api.public_store import token_hash


class PublicWebTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / "data" / "web-test-temp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base, ignore_cleanup_errors=True)
        self.settings = Settings(
            Path(self.temp.name) / "web.db",
            secure_cookies=False,
            provider_key="test-key",
            provider_model="test-model",
        )
        self.calls = []

        async def reply(settings, messages):
            self.calls.append(messages)
            return "A helpful answer."

        self.app = create_app(self.settings, reply)
        self.client = TestClient(self.app, base_url=self.settings.origin)
        self.client.__enter__()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.client.__exit__(None, None, None))
        self.headers = {"Origin": self.settings.origin, "Content-Type": "application/json"}

    def post(self, url, body=None):
        return self.client.post(url, json=body or {}, headers=self.headers)

    def signup(self, email="alice@example.com"):
        result = self.post(
            "/api/auth/signup",
            {"email": email, "name": "Alice", "password": "a long test passphrase"},
        )
        self.assertEqual(result.status_code, 201, result.text)
        return result

    def conversation(self):
        result = self.post("/api/conversations")
        self.assertEqual(result.status_code, 201)
        return result.json()["id"]

    def test_signup_login_logout_and_secret_storage(self):
        response = self.signup()
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        token = self.client.cookies.get(COOKIE)
        with self.app.state.store.connect() as db:
            stored = db.execute("SELECT password FROM users").fetchone()[0]
            saved_token = db.execute("SELECT token FROM sessions").fetchone()[0]
        self.assertNotIn("passphrase", stored)
        self.assertEqual(saved_token, token_hash(token))
        self.assertNotEqual(saved_token, token)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.post("/api/auth/logout").status_code, 200)
        self.assertIsNone(self.app.state.store.authenticate(token))
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.assertEqual(
            self.post(
                "/api/auth/login",
                {"email": "ALICE@example.com", "password": "a long test passphrase"},
            ).status_code,
            200,
        )

    def test_cross_account_read_write_delete_are_blocked(self):
        self.signup()
        first = self.conversation()
        self.assertEqual(
            self.post(
                f"/api/conversations/{first}/messages", {"message": "Alice's confidential idea"}
            ).status_code,
            200,
        )
        self.signup("bob@example.com")
        self.assertEqual(self.client.get("/api/conversations").json(), [])
        self.assertEqual(self.client.get(f"/api/conversations/{first}/messages").status_code, 404)
        self.assertEqual(
            self.post(
                f"/api/conversations/{first}/messages", {"message": "Steal the idea"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/api/conversations/{first}", headers=self.headers).status_code, 404
        )
        second = self.conversation()
        self.post(f"/api/conversations/{second}/messages", {"message": "Bob's question"})
        self.assertNotIn("Alice's confidential idea", str(self.calls[-1]))
        self.assertIn("Bob's question", str(self.calls[-1]))

    def test_chat_history_is_owner_scoped_and_persists(self):
        self.signup()
        conversation = self.conversation()
        self.post(f"/api/conversations/{conversation}/messages", {"message": "First question"})
        self.post(f"/api/conversations/{conversation}/messages", {"message": "Follow up"})
        messages = self.client.get(f"/api/conversations/{conversation}/messages").json()
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(self.calls[-1][1]["content"], "First question")
        self.assertEqual(self.client.get("/api/conversations").json()[0]["title"], "First question")
        self.post("/api/auth/logout")
        self.post(
            "/api/auth/login", {"email": "alice@example.com", "password": "a long test passphrase"}
        )
        self.assertEqual(
            len(self.client.get(f"/api/conversations/{conversation}/messages").json()), 4
        )

    def test_expired_session_is_rejected(self):
        self.signup()
        with self.app.state.store.connect() as db:
            db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)

    def test_csrf_and_invalid_host_rejected(self):
        self.signup()
        self.assertEqual(self.client.post("/api/conversations", json={}).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/conversations", json={}, headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(
            self.client.post(
                "/api/conversations",
                content="{}",
                headers={"Origin": self.settings.origin, "Content-Type": "text/plain"},
            ).status_code,
            415,
        )

    def test_input_validation_and_duplicate_email(self):
        self.assertEqual(
            self.post(
                "/api/auth/signup", {"name": "A", "email": "invalid", "password": "short"}
            ).status_code,
            422,
        )
        self.signup()
        response = self.post(
            "/api/auth/signup",
            {"name": "Another", "email": "ALICE@example.com", "password": "a long test passphrase"},
        )
        self.assertEqual(response.status_code, 409)
        cid = self.conversation()
        for text in ("   ", "x" * 8001):
            self.assertEqual(
                self.post(f"/api/conversations/{cid}/messages", {"message": text}).status_code, 422
            )
        self.assertEqual(
            self.client.post(
                "/api/conversations", content="x" * 40001, headers=self.headers
            ).status_code,
            413,
        )

    def test_delete_cascades_to_messages(self):
        self.signup()
        cid = self.conversation()
        self.post(f"/api/conversations/{cid}/messages", {"message": "Delete me"})
        self.assertEqual(
            self.client.delete(f"/api/conversations/{cid}", headers=self.headers).status_code, 200
        )
        with self.app.state.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.client.get(f"/api/conversations/{cid}/messages").status_code, 404)

    def test_daily_limit_is_per_user(self):
        self.settings.daily_limit = 1
        self.signup()
        cid = self.conversation()
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Hello"}).status_code, 200
        )
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Again"}).status_code, 429
        )
        self.signup("bob@example.com")
        cid = self.conversation()
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Hello"}).status_code, 200
        )

    def test_busy_conversation_rejects_overlap(self):
        account = self.signup().json()["user"]
        cid = self.conversation()
        lease = self.app.state.store.reserve(account["id"], cid)
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Overlap"}).status_code, 409
        )
        self.app.state.store.release(account["id"], cid, lease)
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Ready"}).status_code, 200
        )

    def test_provider_failure_does_not_save_half_a_turn_or_hold_lock(self):
        async def fail(settings, messages):
            raise HTTPException(503, "Temporarily unavailable")

        self.app.state.responder = fail
        account = self.signup().json()["user"]
        cid = self.conversation()
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Hello"}).status_code, 503
        )
        self.assertEqual(self.client.get(f"/api/conversations/{cid}/messages").json(), [])
        lease = self.app.state.store.reserve(account["id"], cid)
        self.app.state.store.release(account["id"], cid, lease)

    def test_missing_provider_is_clear_and_does_not_use_quota(self):
        self.settings.provider_key = ""
        self.signup()
        cid = self.conversation()
        self.assertFalse(self.client.get("/api/health").json()["ai_configured"])
        self.assertEqual(
            self.post(f"/api/conversations/{cid}/messages", {"message": "Hello"}).status_code, 503
        )
        self.assertEqual(self.client.get("/api/auth/me").json()["usage"], 0)

    def test_login_rate_limit_and_generic_errors(self):
        for _ in range(20):
            self.assertEqual(
                self.post(
                    "/api/auth/login", {"email": "absent@example.com", "password": "wrong"}
                ).status_code,
                401,
            )
        self.assertEqual(
            self.post(
                "/api/auth/login", {"email": "absent@example.com", "password": "wrong"}
            ).status_code,
            429,
        )

    def test_signup_can_be_closed(self):
        self.settings.signup_enabled = False
        self.assertEqual(
            self.post(
                "/api/auth/signup",
                {"name": "Test", "email": "test@example.com", "password": "a long test passphrase"},
            ).status_code,
            403,
        )

    def test_public_service_has_no_desktop_routes(self):
        self.signup()
        for path in (
            "/api/tools",
            "/api/memory",
            "/api/tasks",
            "/api/vision/capture",
            "/api/auth/token",
            "/ws",
        ):
            self.assertEqual(self.client.get(path).status_code, 404)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)

    def test_nonlocal_insecure_deployment_rejected(self):
        with self.assertRaises(ValueError):
            create_app(
                Settings(self.settings.db_path, origin="https://example.com", secure_cookies=False)
            )

    def test_context_is_bounded_and_does_not_start_with_orphan_assistant(self):
        history = [
            {"role": role, "content": "x" * 4000}
            for _ in range(30)
            for role in ("user", "assistant")
        ]
        messages = context_messages(history, "hi")
        self.assertLess(sum(len(m["content"]) for m in messages), 25000)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "hi")

    def test_provider_adapter_sends_no_tools_or_credentials_in_body(self):
        seen = []

        def handle(request):
            import json

            seen.append(json.loads(request.content))
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch("api.public.httpx.AsyncClient", return_value=client):
            self.assertEqual(
                asyncio.run(provider_reply(self.settings, [{"role": "user", "content": "Hi"}])),
                "Answer",
            )
        self.assertNotIn("tools", seen[0])
        self.assertNotIn("test-key", str(seen[0]))

    def test_upstream_error_body_is_not_exposed(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text="secret provider error test-key")
            )
        )
        with (
            patch("api.public.httpx.AsyncClient", return_value=client),
            self.assertRaises(HTTPException) as result,
        ):
            asyncio.run(provider_reply(self.settings, []))
        self.assertEqual(result.exception.status_code, 503)
        self.assertNotIn("test-key", result.exception.detail)

    def test_malformed_key_has_actionable_error_without_network_call(self):
        for key in ("gsk_nonascii_\u00d8", "key\nwith-newline", "key with spaces"):
            self.settings.provider_key = key
            with patch("api.public.httpx.AsyncClient") as factory:
                with self.assertRaises(HTTPException) as result:
                    asyncio.run(provider_reply(self.settings, []))
                factory.assert_not_called()
            self.assertEqual(result.exception.status_code, 503)
            self.assertIn("invalid characters", result.exception.detail)
            self.assertNotIn(key, result.exception.detail)

    def test_quota_model_and_connection_errors_are_distinguishable(self):
        for status, expected in (
            (429, "usage limit"),
            (404, "configuration"),
            (401, "credentials"),
        ):
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, status=status: httpx.Response(
                        status, text="private upstream details"
                    )
                )
            )
            with (
                patch("api.public.httpx.AsyncClient", return_value=client),
                self.assertRaises(HTTPException) as result,
            ):
                asyncio.run(provider_reply(self.settings, []))
            self.assertIn(expected, result.exception.detail)
            self.assertNotIn("private upstream details", result.exception.detail)

        def offline(request):
            raise httpx.ConnectError("private connection details")

        client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
        with (
            patch("api.public.httpx.AsyncClient", return_value=client),
            self.assertRaises(HTTPException) as result,
        ):
            asyncio.run(provider_reply(self.settings, []))
        self.assertIn("could not connect", result.exception.detail)


if __name__ == "__main__":
    unittest.main()

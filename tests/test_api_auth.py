"""Tests for api/auth.py (AuthManager) and the /api/auth/token mint route.

Locks the quick-win hardening: tokens default to api.token_expiry_days
(config, 30d), expiry is enforced on verify, and the bootstrap mint gate is
expiry-aware so an all-expired token DB re-opens minting instead of locking
the user out forever (that re-open is the refresh flow).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import api.state
from api.auth import AuthManager
from api.routes import system as system_routes


def _make_manager(tmp_path) -> AuthManager:
    return AuthManager(tmp_path / "api_tokens.db")


def _make_request(payload: dict, bearer: str | None = None) -> Request:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json")]
    if bearer:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    scope = {"type": "http", "method": "POST", "path": "/api/auth/token", "headers": headers}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _expire_all(manager: AuthManager) -> None:
    """Force every token's expires_at into the past."""
    from api.auth import _connect

    conn = _connect(manager.db_path)
    conn.execute("UPDATE tokens SET expires_at = ?", ("2000-01-01 00:00:00",))
    conn.commit()
    conn.close()


# ---------- AuthManager ----------


def test_create_and_verify_roundtrip(tmp_path):
    m = _make_manager(tmp_path)
    token = m.create_token("hud", expires_days=30)
    assert m.verify_token(token)
    assert not m.verify_token("wrong-token")
    assert m.has_valid_token()


def test_expired_token_is_rejected(tmp_path):
    m = _make_manager(tmp_path)
    token = m.create_token("old", expires_days=30)
    _expire_all(m)
    assert not m.verify_token(token), "expired token must not verify"
    assert not m.has_valid_token(), "expired tokens must not count as valid"


def test_never_expiring_token_still_supported(tmp_path):
    m = _make_manager(tmp_path)
    token = m.create_token("legacy", expires_days=None)
    assert m.verify_token(token)
    assert m.has_valid_token()


def test_revoked_token_is_rejected(tmp_path):
    m = _make_manager(tmp_path)
    token = m.create_token("revoke-me", expires_days=30)
    assert m.revoke_token(token)
    assert not m.verify_token(token)
    assert not m.has_valid_token()


# ---------- mint route ----------


def _stub_state(manager: AuthManager, expiry_days: int = 45):
    old_auth, old_cfg = api.state.auth_manager, api.state.config
    api.state.auth_manager = manager
    api.state.config = SimpleNamespace(api=SimpleNamespace(token_expiry_days=expiry_days))
    return old_auth, old_cfg


def _restore_state(old):
    api.state.auth_manager, api.state.config = old


def test_mint_bootstrap_then_requires_auth(tmp_path):
    m = _make_manager(tmp_path)
    old = _stub_state(m)
    try:
        # No tokens yet: bootstrap mint is open.
        r1 = asyncio.run(system_routes.create_token(_make_request({"name": "hud"})))
        assert r1["token"] and m.verify_token(r1["token"])
        # A valid token now exists: unauthenticated mint must 401.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(system_routes.create_token(_make_request({"name": "evil"})))
        assert exc.value.status_code == 401
        # Authenticated mint still works.
        r2 = asyncio.run(
            system_routes.create_token(_make_request({"name": "second"}, bearer=r1["token"]))
        )
        assert m.verify_token(r2["token"])
    finally:
        _restore_state(old)


def test_mint_reopens_after_all_tokens_expire(tmp_path):
    """The refresh flow: once every token expires, bootstrap minting re-opens
    (otherwise the dashboard would be locked out permanently)."""
    m = _make_manager(tmp_path)
    old = _stub_state(m)
    try:
        r1 = asyncio.run(system_routes.create_token(_make_request({"name": "hud"})))
        _expire_all(m)
        assert not m.verify_token(r1["token"])
        # No auth header, but minting is open again because nothing valid exists.
        r2 = asyncio.run(system_routes.create_token(_make_request({"name": "hud-renewed"})))
        assert m.verify_token(r2["token"])
    finally:
        _restore_state(old)


def test_mint_default_expiry_comes_from_config(tmp_path):
    m = _make_manager(tmp_path)
    old = _stub_state(m, expiry_days=45)
    try:
        asyncio.run(system_routes.create_token(_make_request({"name": "hud"})))
        row = m.list_tokens()[0]
        expires = time.mktime(time.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S"))
        delta_days = (expires - time.time()) / 86400
        assert 44.0 < delta_days <= 45.0, f"expected ~45d expiry, got {delta_days:.2f}d"
    finally:
        _restore_state(old)


def test_mint_explicit_expiry_clamped(tmp_path):
    m = _make_manager(tmp_path)
    old = _stub_state(m)
    try:
        asyncio.run(system_routes.create_token(_make_request({"name": "hud", "expires_days": 365})))
        row = m.list_tokens()[0]
        expires = time.mktime(time.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S"))
        delta_days = (expires - time.time()) / 86400
        assert delta_days <= 90.0, "explicit expiry must be clamped to 90d"
    finally:
        _restore_state(old)

"""Breaker + TPM persistence across restarts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ProviderConfig
from core.providers import AuthFailed, PermanentError
from core.router import FailoverRouter, UsageLedger


def _prov(name="a"):
    return ProviderConfig(
        name=name,
        base_url="https://x.test/v1",
        model="m",
        api_key="k",
        priority=0,
        daily_token_budget=10**9,
    )


def _router(tmp_path, providers=None):
    ledger = UsageLedger(tmp_path / "usage.db")
    return FailoverRouter(providers or [_prov()], client=None, ledger=ledger)


def test_breaker_survives_reload(tmp_path):
    r1 = _router(tmp_path)
    r1.breakers.record_failure(_prov(), PermanentError("dead"))
    assert not r1.breakers.get("a").allows()
    r2 = _router(tmp_path)  # fresh process, same dir
    assert not r2.breakers.get("a").allows()
    assert r2.breakers.describe("a")[0] == "dead"


def test_expired_entries_dont_block(tmp_path):
    r1 = _router(tmp_path)
    r1.breakers.record_failure(_prov(), AuthFailed("nope"))
    # rewrite remaining as expired
    import json

    p = tmp_path / "router_state.json"
    data = json.loads(p.read_text())
    data["breakers"]["a"]["remaining"] = 0
    p.write_text(json.dumps(data))
    r2 = _router(tmp_path)
    assert r2.breakers.get("a").allows()


def test_success_clears_persisted_state(tmp_path):
    r1 = _router(tmp_path)
    r1.breakers.record_failure(_prov(), AuthFailed("nope"))
    r1.breakers.record_success("a")
    r2 = _router(tmp_path)
    assert r2.breakers.get("a").allows()
    assert r2.breakers.get("a").failures == 0


def test_tpm_limit_persists(tmp_path):
    r1 = _router(tmp_path)
    r1.ledger.learn_tpm_limit("a", 8000)
    r1._persist_router_state()
    r2 = _router(tmp_path)
    assert r2.ledger.TPM_LIMIT.get("a") == 8000


def test_corrupt_state_ignored(tmp_path):
    (tmp_path / "router_state.json").write_text("not json{{{")
    r = _router(tmp_path)  # must not raise
    assert r.breakers.get("a").allows()

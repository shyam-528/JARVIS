"""Budget guardrails: tiers, alerts once/day, pressure penalty."""

from core.budget_guard import (
    due_alerts,
    format_alert,
    record_alerts,
    tier_of,
    tier_pressure,
    tier_status,
)
from core.config import ProviderConfig


def _p(name, budget=1000):
    return ProviderConfig(
        name=name,
        base_url="https://x.test/v1",
        model="m",
        api_key="k",
        priority=0,
        daily_token_budget=budget,
    )


class FakeLedger:
    def __init__(self, used):
        self.used = used

    def today(self, name):
        return self.used.get(name, 0)


def test_tier_of():
    assert tier_of("groq-gpt-oss-120b") == "groq"
    assert tier_of("zen-mimo") == "other"  # zen providers pruned from config (2026-09-22)
    assert tier_of("openrouter-backup") == "openrouter"
    assert tier_of("weird") == "other"


def test_tier_status_pct():
    ps = [_p("groq-a", 1000), _p("groq-b", 1000)]
    st = tier_status(ps, FakeLedger({"groq-a": 900, "groq-b": 900}))
    assert st["groq"]["pct"] == 10.0
    assert st["groq"]["left"] == 200


def test_alert_once_per_day(tmp_path):
    ps = [_p("groq-a", 1000)]
    led = FakeLedger({"groq-a": 950})
    state = tmp_path / "alerts.json"
    assert due_alerts(ps, led, state) == ["groq"]
    record_alerts(state, ["groq"])
    assert due_alerts(ps, led, state) == []
    msg = format_alert(tier_status(ps, led), ["groq"])
    assert "groq" in msg and "5.0%" in msg


def test_paid_tiers_never_alert(tmp_path):
    ps = [_p("openrouter-backup", 100)]
    led = FakeLedger({"openrouter-backup": 100})
    assert due_alerts(ps, led, tmp_path / "a.json") == []


def test_pressure_ramps():
    ps = [_p("groq-a", 1000)]
    assert tier_pressure(ps, FakeLedger({"groq-a": 0}), "groq") == 0.0
    mid = tier_pressure(ps, FakeLedger({"groq-a": 900}), "groq")
    assert 0.0 < mid < 1.0
    assert tier_pressure(ps, FakeLedger({"groq-a": 1000}), "groq") == 1.0

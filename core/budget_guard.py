"""Budget guardrails: tier caps, low-budget alerts, load shedding.

Free tiers die nightly from quota burn. This module:
- groups providers into tiers (groq/gemini/nim/openrouter) by name prefix,
- reports remaining % per tier from config budgets + usage ledger,
- alerts once per day per tier at 20% remaining (Telegram default),
- gives the router a pressure penalty so low tiers are tried last.
"""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic

WARN_PCT = 20.0


def tier_of(provider_name: str) -> str:
    name = (provider_name or "").lower()
    for tier in ("groq", "gemini", "nim", "openrouter", "local"):
        if name.startswith(tier):
            return tier
    return "other"


def tier_status(providers, ledger) -> dict[str, dict]:
    """{tier: {budget, used, left, pct}} from config budgets + today's ledger."""
    out: dict[str, dict] = {}
    for p in providers:
        if not getattr(p, "enabled", True):
            continue
        tier = tier_of(p.name)
        entry = out.setdefault(tier, {"budget": 0, "used": 0})
        with contextlib.suppress(Exception):
            entry["budget"] += int(p.daily_token_budget)
        with contextlib.suppress(Exception):
            entry["used"] += int(ledger.today(p.name))
    for _tier, entry in out.items():
        budget = entry["budget"] or 1
        left = max(0, budget - entry["used"])
        entry["left"] = left
        entry["pct"] = round(left / budget * 100.0, 1)
    return out


def tier_pressure(providers, ledger, tier: str) -> float:
    """0.0 healthy -> 1.0 exhausted. Router penalty input."""
    try:
        status = tier_status(providers, ledger)
        pct = status.get(tier, {}).get("pct", 100.0)
        if pct >= 25.0:
            return 0.0
        return round((25.0 - pct) / 25.0, 3)
    except Exception:
        return 0.0


def due_alerts(providers, ledger, state_path: Path | str) -> list[str]:
    """Tier names below WARN_PCT not yet alerted today. Records nothing."""
    try:
        state_file = Path(state_path)
        # P0: shared corrupt-file contract — a corrupt alert log quarantines
        # aside and reads as {} (worst case: one repeat alert today, safe side).
        state = load_json_store(state_file, default={}, owner="budget_guard", expect=dict)
    except Exception:
        state = {}
        state_file = None
    today = date.today().isoformat()
    status = tier_status(providers, ledger)
    due = []
    for tier, entry in sorted(status.items()):
        if tier in ("openrouter", "local", "other"):
            continue
        if entry["pct"] < WARN_PCT and state.get(tier) != today:
            due.append(tier)
    return due


def record_alerts(state_path: Path | str, tiers: list[str]) -> None:
    try:
        state_file = Path(state_path)
        state = load_json_store(state_file, default={}, owner="budget_guard", expect=dict)
        today = date.today().isoformat()
        for tier in tiers:
            state[tier] = today
        # P0: atomic write; a failed alert-log write stays loud via the helper.
        save_json_atomic(state_file, state)
    except Exception:
        pass


def format_alert(status: dict, tiers: list[str]) -> str:
    lines = ["⚠️ Budget low — free tiers nearly out for today:"]
    for tier in tiers:
        e = status.get(tier, {})
        lines.append(f"  - {tier}: {e.get('pct', '?')}% left ({e.get('left', 0):,} tokens)")
    lines.append("Expect failover hops tonight. Paid backup covers total blackout if enabled.")
    return "\n".join(lines)

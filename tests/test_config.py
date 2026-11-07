"""Wave 0 hardening tests: core/config.py + core/db.py failure paths."""

from __future__ import annotations

import pytest

from core.config import key_report, load_config
from core.db import connect


def _write(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_empty_yaml_boots_on_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.jarvis_name == "JARVIS MARK 3"
    assert cfg.providers == []


def test_blank_yaml_boots_on_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "# nothing here\n"))
    assert cfg.providers == []


def test_malformed_provider_fails_loudly(tmp_path):
    path = _write(tmp_path, "providers:\n  - name: broken\n    key_env: SOME_KEY\n")
    with pytest.raises(ValueError, match="broken.*base_url.*model"):
        load_config(path)


def test_provider_missing_key_env_fails_loudly(tmp_path):
    path = _write(
        tmp_path,
        "providers:\n  - base_url: https://x.test/v1\n    model: m\n",
    )
    with pytest.raises(ValueError, match=r"providers\[0\].*name.*key_env"):
        load_config(path)


def test_missing_api_key_skips_provider_and_reports(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_KEY", raising=False)
    path = _write(
        tmp_path,
        "providers:\n"
        "  - name: nokey\n"
        "    key_env: DEFINITELY_UNSET_KEY\n"
        "    base_url: https://x.test/v1\n"
        "    model: m\n",
    )
    cfg = load_config(path)
    assert cfg.providers == []
    report = key_report(path)
    assert any("nokey" in m for m in report.missing_env)


def test_disabled_provider_is_reported_not_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVE0_TEST_KEY", "secret")
    path = _write(
        tmp_path,
        "providers:\n"
        "  - name: disabled-one\n"
        "    enabled: false\n"
        "    key_env: WAVE0_TEST_KEY\n"
        "    base_url: https://x.test/v1\n"
        "    model: m\n",
    )
    cfg = load_config(path)
    assert cfg.providers == []
    assert "disabled-one" in key_report(path).disabled


def test_active_provider_loads_with_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("WAVE0_TEST_KEY", "secret")
    path = _write(
        tmp_path,
        "providers:\n"
        "  - name: active-one\n"
        "    key_env: WAVE0_TEST_KEY\n"
        "    base_url: https://x.test/v1/\n"
        "    model: m\n",
    )
    cfg = load_config(path)
    assert len(cfg.providers) == 1
    p = cfg.providers[0]
    assert p.name == "active-one"
    assert p.api_key == "secret"
    assert p.base_url == "https://x.test/v1"  # trailing slash stripped
    assert p.daily_token_budget == 1_000_000


def test_db_connect_sets_wal_and_busy_timeout(tmp_path):
    conn = connect(tmp_path / "t.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode.lower() == "wal"
        assert busy == 30000
    finally:
        conn.close()


def test_db_connect_accepts_str_path(tmp_path):
    conn = connect(str(tmp_path / "t2.db"))
    conn.close()

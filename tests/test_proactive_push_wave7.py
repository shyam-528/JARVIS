"""Wave 7 failure-path tests for core.proactive_push (PushGate).

Contract: send() returns {"delivered", "reason"} — never raises. State files
guard USER PREFERENCES: corrupt/unwritable mute/count/dedup files are loud
prints, not silent preference loss.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.proactive_push import DEFAULT_MAX_PER_DAY, PushGate, _max_per_day


@pytest.fixture
def gate(tmp_path):
    return PushGate(tmp_path, max_per_day=6)


# ---------- state-file corruption is loud ----------


def test_corrupt_mute_file_is_loud_and_fails_open(gate, capsys):
    gate._mute_path.write_text("{not json", encoding="utf-8")
    assert gate.muted("digest") is False
    out = capsys.readouterr().out
    assert "corrupt store proactive_mute.json" in out
    # P0: quarantined aside (timestamped, nothing deleted), not left corrupt
    assert not gate._mute_path.exists()
    backups = list(gate._mute_path.parent.glob("proactive_mute.json.corrupt-*"))
    assert len(backups) == 1
    assert "quarantined" in out


def test_missing_mute_file_is_quiet(gate, capsys):
    assert gate.muted("digest") is False
    assert capsys.readouterr().out == ""


def test_mute_write_failure_is_loud(gate, capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(gate._mute_path), "write_text", boom)
    gate.set_muted("digest", True)
    out = capsys.readouterr().out
    assert "failed to persist mute" in out and "digest" in out


def test_corrupt_count_file_is_loud_and_resets(gate, capsys):
    gate._count_path.write_text("{not json", encoding="utf-8")
    assert gate.today_count() == 0
    out = capsys.readouterr().out
    assert "corrupt store proactive_push.json" in out
    # P0: quarantined aside, not left corrupt to re-announce every check
    assert list(gate._count_path.parent.glob("proactive_push.json.corrupt-*"))
    assert gate.today_count() == 0  # second read: missing file, quiet
    assert capsys.readouterr().out == ""


def test_count_write_failure_is_loud(gate, capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(type(gate._count_path), "write_text", boom)
    assert gate._bump() == 1
    assert "failed to persist daily count" in capsys.readouterr().out


def test_unreadable_dedup_log_is_loud(gate, capsys, monkeypatch):
    gate._sent_path.write_text("k1\n", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("io error")

    monkeypatch.setattr(type(gate._sent_path), "read_text", boom)
    assert gate.seen("k1") is False  # dedup lost for the session
    assert "dedup" in capsys.readouterr().out


def test_dedup_append_failure_is_loud(gate, capsys, monkeypatch):
    import builtins

    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path).endswith("proactive_sent.jsonl"):
            raise OSError("disk full")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    gate._mark_sent("nudge:bill:x:y")
    out = capsys.readouterr().out
    assert "failed to persist dedup key" in out and "nudge:bill:x:y" in out


def test_bad_env_cap_is_loud_and_defaults(monkeypatch, capsys):
    monkeypatch.setenv("PROACTIVE_MAX_PER_DAY", "six")
    assert _max_per_day() == DEFAULT_MAX_PER_DAY
    assert "PROACTIVE_MAX_PER_DAY" in capsys.readouterr().out


# ---------- send() contract: outcome always in the return dict ----------


async def test_send_notify_exception_returns_reason(gate):
    class BadMgr:
        async def notify(self, **kw):
            raise ConnectionError("telegram down")

    orch = SimpleNamespace(notify_manager=BadMgr())
    result = await gate.send("digest", "k1", "t", "m", orch=orch)
    assert result["delivered"] is False
    assert "telegram down" in result["reason"]


async def test_send_notify_error_dict_returns_reason(gate):
    class ErrMgr:
        async def notify(self, **kw):
            return {"success": False, "error": "ntfy 503"}

    orch = SimpleNamespace(notify_manager=ErrMgr())
    result = await gate.send("digest", "k1", "t", "m", orch=orch)
    assert result["delivered"] is False
    assert "ntfy 503" in result["reason"]


async def test_send_voice_failure_still_reports_delivered(gate):
    class OkMgr:
        async def notify(self, **kw):
            return {"success": True}

    orch = SimpleNamespace(notify_manager=OkMgr())

    async def bad_speak(text):
        raise RuntimeError("no audio device")

    orch.speak_alert = bad_speak
    result = await gate.send("digest", "k1", "t", "m", speak="hello", orch=orch)
    assert result["delivered"] is True  # text delivered; voice best-effort
    assert gate.seen("k1")
    assert gate.today_count() == 1


async def test_send_gates_still_work(gate):
    gate.set_muted("digest", True)
    result = await gate.send("digest", "k1", "t", "m", orch=SimpleNamespace())
    assert result == {"delivered": False, "reason": "digest muted"}

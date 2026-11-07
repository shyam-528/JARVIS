"""Auto-speak policy: quiet hours, master switch, scheduler hook."""

from datetime import datetime

from core.voice_alerts import alerts_enabled, in_quiet_hours, parse_quiet, should_speak


def test_parse_quiet():
    assert parse_quiet("22-7") == (22, 7)
    assert parse_quiet("garbage") == (22, 7)


def test_quiet_wrap():
    assert in_quiet_hours(datetime(2026, 1, 1, 23), "22-7") is True
    assert in_quiet_hours(datetime(2026, 1, 1, 6), "22-7") is True
    assert in_quiet_hours(datetime(2026, 1, 1, 12), "22-7") is False
    assert in_quiet_hours(datetime(2026, 1, 1, 12), "9-17") is True
    assert in_quiet_hours(datetime(2026, 1, 1, 20), "9-17") is False


def test_master_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_ALERTS", "")
    assert alerts_enabled(None) is False
    assert alerts_enabled(True) is True
    monkeypatch.setenv("JARVIS_VOICE_ALERTS", "1")
    assert alerts_enabled(None) is True
    assert alerts_enabled(False) is False


def test_should_speak(monkeypatch):
    monkeypatch.setenv("JARVIS_VOICE_ALERTS", "1")
    assert should_speak(None, datetime(2026, 1, 1, 12)) is True
    assert should_speak(None, datetime(2026, 1, 1, 23)) is False
    monkeypatch.setenv("JARVIS_VOICE_ALERTS", "")
    assert should_speak(None, datetime(2026, 1, 1, 12)) is False


def test_scheduler_hook_only_notify_actions():
    import asyncio

    from core.config import Config
    from core.scheduler import TaskScheduler

    spoken = []

    class Orch:
        async def speak_alert(self, text):
            spoken.append(text)
            return True

    import tempfile
    from pathlib import Path

    cfg = Config.__new__(Config)
    cfg.db_path = Path(tempfile.mkdtemp()) / "state.db"
    sched = TaskScheduler(cfg)
    sched._orchestrator = Orch()
    asyncio.run(sched._maybe_voice("notify_reminder", {"message": "study", "title": "R"}))
    asyncio.run(sched._maybe_voice("run_workflow", {}))
    asyncio.run(sched._maybe_voice("notify_briefing", {"kind": "morning"}))
    assert len(spoken) == 2
    assert "study" in spoken[0]
    assert "Telegram" in spoken[1]

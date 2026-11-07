"""Proactive push: gate (mute/cap/dedup), digest + meeting builders and tools."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.proactive_push import (
    PushGate,
    bill_key,
    compose_digest,
    digest_key,
    due_meetings,
    find_bill_events,
    find_overdue,
    find_stale_projects,
    meeting_key,
    nudge_bucket,
    stale_week_key,
)
from core.tools import ToolBox


def _gate(tmp_path, **kw):
    return PushGate(tmp_path, **kw)


def test_mute_blocks_and_unblocks(tmp_path):
    g = _gate(tmp_path)
    assert not g.muted("digest")
    g.set_muted("digest", True)
    assert _gate(tmp_path).muted("digest")  # persisted
    g.set_muted("digest", False)
    assert not _gate(tmp_path).muted("digest")


def test_cap_blocks_after_limit(tmp_path):
    g = _gate(tmp_path, max_per_day=2)
    assert g.today_count() == 0
    g._bump()
    g._bump()
    assert g.today_count() == 2


def test_dedup_seen_and_prune(tmp_path):
    g = _gate(tmp_path)
    assert not g.seen("k1")
    g._mark_sent("k1")
    assert _gate(tmp_path).seen("k1")


class _Notify:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    async def notify(self, message="", title="", **kw):
        self.calls.append((title, message))
        return {"success": self.ok} if self.ok else {"success": False, "error": "down"}


def _orch(tmp_path, notify=None, spoken=None):
    notify = notify or _Notify()

    async def speak(text):
        spoken.append(text)
        return True

    return SimpleNamespace(
        cfg=SimpleNamespace(db_path=tmp_path / "usage.db"),
        scheduler=SimpleNamespace(list_tasks=lambda: []),
        notify_manager=SimpleNamespace(notify=notify.notify),
        speak_alert=speak,
    ), notify


async def test_send_happy_path_telegram_plus_voice(tmp_path):
    spoken = []
    orch, notify = _orch(tmp_path, spoken=spoken)
    g = PushGate(tmp_path)
    res = await g.send("digest", "k-happy", "T", "hello sir", "good morning sir", orch)
    assert res == {"delivered": True, "reason": "sent"}
    assert len(notify.calls) == 1
    assert spoken and "good morning" in spoken[0].lower()
    assert g.today_count() == 1
    assert g.seen("k-happy")


async def test_send_respects_mute_cap_dup(tmp_path):
    spoken = []
    orch, notify = _orch(tmp_path, spoken=spoken)
    g = PushGate(tmp_path, max_per_day=1)
    g.set_muted("meeting", True)
    assert (await g.send("meeting", "k1", "T", "m", "s", orch))["reason"] == "meeting muted"
    assert (await g.send("digest", "k2", "T", "m", "s", orch))["delivered"] is True
    assert (await g.send("digest", "k2", "T", "m", "s", orch))["reason"] == "duplicate"
    assert (await g.send("digest", "k3", "T", "m", "s", orch))["reason"] == "daily cap reached"
    assert len(notify.calls) == 1  # only the first digest went out


async def test_send_force_bypasses_gates(tmp_path):
    spoken = []
    orch, notify = _orch(tmp_path, spoken=spoken)
    g = PushGate(tmp_path, max_per_day=1)
    g.set_muted("digest", True)
    g._bump()
    res = await g.send("digest", "k9", "T", "manual", "hi", orch, force=True)
    assert res["delivered"] is True
    assert len(notify.calls) == 1


async def test_send_notify_failure_not_counted(tmp_path):
    orch, _ = _orch(tmp_path, notify=_Notify(ok=False))
    g = PushGate(tmp_path)
    res = await g.send("digest", "kf", "T", "m", "s", orch)
    assert res["delivered"] is False
    assert g.today_count() == 0
    assert not g.seen("kf")


def test_compose_digest_full_and_empty():
    now = datetime(2026, 9, 14, 7, 30)
    msg, speak = compose_digest(
        [{"label": "take pills (21:00)"}],
        [{"when": "10:00", "title": "Standup"}],
        [{"from": "boss@x.com", "subject": "deploy today"}],
        now,
    )
    assert "take pills" in msg and "Standup" in msg and "boss@x.com" in msg
    assert "sir" in speak.lower()
    msg2, speak2 = compose_digest([], [], [], now)
    assert "quiet" in msg2.lower() or "No reminders" in msg2
    assert speak2.strip()


def test_due_meetings_window():
    now = datetime(2026, 9, 14, 9, 0)

    def _ev(mins, title="Sync"):
        return {"start": now + timedelta(minutes=mins), "summary": title}

    got = due_meetings([_ev(20), _ev(120), _ev(-10), _ev(35)], now)
    assert [(g["in_min"], g["event"]["summary"]) for g in got] == [(20, "Sync"), (35, "Sync")]
    assert due_meetings([], now) == []


def test_keys_stable_format():
    assert digest_key().startswith("digest:")
    k = meeting_key(None, "Team Sync!", datetime(2026, 9, 14, 10, 30))
    assert k.startswith("meeting:") and "1000" not in k and "1030" in k


async def test_tools_register_and_run_without_crash(tmp_path):
    from skills.proactive_push import register_proactive_push

    spoken = []
    orch, _ = _orch(tmp_path, spoken=spoken)
    box = ToolBox()
    register_proactive_push(box, orch)
    assert "proactive_digest" in box.names()
    assert "proactive_meeting_watch" in box.names()
    # no scheduler/mail/calendar configured: must degrade gracefully, never raise
    out = await box.execute("proactive_digest", "{}")
    assert "Digest" in out
    out2 = await box.execute("proactive_meeting_watch", "{}")
    assert "Meeting watch" in out2


def _once_task(tid="t1", name="take pills", run_at=None, enabled=True, ran=False):
    now = datetime.now()
    return SimpleNamespace(
        id=tid,
        name=name,
        enabled=enabled,
        trigger_type="once",
        trigger_spec={"run_at": (run_at or (now - timedelta(hours=2))).isoformat()},
        run_count=1 if ran else 0,
        last_run="x" if ran else None,
        next_run=None,
    )


def test_find_overdue_only_unrun_past_once_tasks():
    now = datetime.now()
    tasks = [
        _once_task("a", "take pills"),
        _once_task("b", "future", run_at=now + timedelta(hours=5)),
        _once_task("c", "done one", ran=True),
        _once_task("d", "disabled", enabled=False),
        SimpleNamespace(
            id="e",
            name="daily brief",
            enabled=True,
            trigger_type="daily",
            trigger_spec={},
            run_count=0,
            last_run=None,
            next_run=None,
        ),
    ]
    got = find_overdue(tasks, now)
    assert [g["id"] for g in got] == ["a"]
    assert got[0]["overdue_min"] >= 100


def test_nudge_bucket_windows():
    start = datetime.fromtimestamp(nudge_bucket(datetime(2026, 9, 14, 9, 5)) * 6 * 3600)
    assert nudge_bucket(start) == nudge_bucket(start + timedelta(hours=5))
    assert nudge_bucket(start) != nudge_bucket(start + timedelta(hours=7))


def test_find_bill_events_keywords_and_dates():
    now = datetime(2026, 9, 14, 9, 0)
    evs = [
        {"start": now + timedelta(days=2), "summary": "Electricity bill due"},
        {"start": now + timedelta(days=1), "summary": "Team standup"},
        {"start": now - timedelta(days=1), "summary": "Old invoice"},
        {"start": now + timedelta(days=20), "summary": "Insurance renewal"},
        {"start": None, "summary": "Bill mystery"},
    ]
    got = find_bill_events(evs, now)
    titles = [g["title"] for g in got]
    assert any("Electricity" in t for t in titles)
    assert any("Insurance" in t for t in titles)
    assert not any("standup" in t.lower() for t in titles)
    assert not any("Old invoice" in t for t in titles)
    assert bill_key("Electricity bill due", "Tue 10:00").startswith("nudge:bill:")
    assert stale_week_key(now, "myrepo").startswith("nudge:stale:")


def test_find_stale_projects_tmp_git(tmp_path):
    import shutil
    import subprocess

    git = shutil.which("git") or r"C:\Program Files\Git\bin\git.exe"
    if not shutil.which("git") and not Path(git).exists():
        pytest.skip("git not available")
    if not shutil.which("git"):
        import os

        os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", ""), str(Path(git).parent)])

    def mk(name, days_ago):
        import os

        d = tmp_path / name
        d.mkdir()
        when = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
        env = dict(
            os.environ,
            GIT_AUTHOR_DATE=when,
            GIT_COMMITTER_DATE=when,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@t",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@t",
        )
        subprocess.run(["git", "init", "-q", str(d)], check=True, env=env)
        (d / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(d), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True, env=env)
        return d

    mk("fresh", 1)
    mk("stale", 10)
    (tmp_path / "notrepo").mkdir()
    got = find_stale_projects([str(tmp_path)], stale_days=7)
    assert [g["name"] for g in got] == ["stale"]
    assert got[0]["days"] >= 7


async def test_nudge_sweep_sends_once_then_dedups(tmp_path, monkeypatch):
    from skills.proactive_push import register_proactive_push

    spoken = []
    now = datetime.now()
    tasks = [_once_task("a", "take pills", run_at=now - timedelta(hours=3))]
    orch, notify = _orch(tmp_path, spoken=spoken)
    orch.scheduler = SimpleNamespace(list_tasks=lambda: tasks)
    monkeypatch.setattr(
        "core.calendar_feed.fetch_upcoming_events",
        lambda days=7: [{"start": now + timedelta(days=2), "summary": "Electricity bill due"}],
    )
    import core.proactive_push as _pp

    monkeypatch.setattr(_pp, "find_stale_projects", lambda **kw: [])
    box = ToolBox()
    register_proactive_push(box, orch)
    out1 = await box.execute("proactive_nudge_sweep", "{}")
    assert "2 sent" in out1  # overdue + bill
    assert len(notify.calls) == 2
    assert spoken  # voice line went out too
    out2 = await box.execute("proactive_nudge_sweep", "{}")
    assert "0 sent" in out2  # dedup: same window, nothing repeats
    assert len(notify.calls) == 2


async def test_nudge_sweep_muted_kind_stays_quiet(tmp_path, monkeypatch):
    from skills.proactive_push import register_proactive_push

    spoken = []
    orch, notify = _orch(tmp_path, spoken=spoken)
    orch.scheduler = SimpleNamespace(
        list_tasks=lambda: [
            _once_task("a", "take pills", run_at=datetime.now() - timedelta(hours=3))
        ]
    )
    monkeypatch.setattr("core.calendar_feed.fetch_upcoming_events", lambda days=7: [])
    import core.proactive_push as _pp

    monkeypatch.setattr(_pp, "find_stale_projects", lambda **kw: [])
    from core.proactive_push import PushGate as _G

    _G(orch.cfg.db_path.parent).set_muted("nudge", True)
    box = ToolBox()
    register_proactive_push(box, orch)
    out = await box.execute("proactive_nudge_sweep", "{}")
    assert "0 sent" in out
    assert len(notify.calls) == 0

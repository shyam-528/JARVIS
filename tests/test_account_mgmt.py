"""Account defect (changes.txt): Gmail + Jarvis-side calendar management.

"Check my account" must reach the inbox tools (never "no access"); "draft a
reply for <person>" drafts without a mail word; "mark <day> as <event>"
creates a briefing-visible reminder; drafts live IN Gmail (IMAP APPEND),
and "send it" sends via SMTP with Gmail-copy cleanup.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

from skills.nl_shortcuts import _parse_day, try_mail_shortcut


class FakeToolbox:
    def __init__(self, search_hit="[777] Top hit"):
        self.calls = []
        self.search_hit = search_hit

    async def execute(self, tool, args_json):
        args = json.loads(args_json or "{}")
        self.calls.append((tool, args))
        if tool == "mail_search":
            return self.search_hit
        return f"ok:{tool}"


class FakeOrch:
    def __init__(self, **kw):
        self.toolbox = FakeToolbox(**kw)
        self.history = []


class FakeConsole:
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        self.printed.append(str(args[0]) if args else "")


def _run(orch, text):
    con = FakeConsole()
    hit = asyncio.run(try_mail_shortcut(orch, text, con))
    return hit, orch.toolbox.calls, con.printed


# ---------- account words reach the inbox ----------


def test_check_my_account_reads_unread():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "check my account")
    assert hit is True
    assert ("mail_unread", {"limit": 8}) in calls


def test_anything_in_my_account_reads_unread():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "anything new in my account?")
    assert hit is True
    assert ("mail_unread", {"limit": 8}) in calls


def test_money_contexts_fall_through():
    for text in ("my bank account balance", "delete my account", "check my salary account"):
        orch = FakeOrch()
        hit, calls, _ = _run(orch, text)
        assert hit is False, text
        assert calls == []


# ---------- draft a reply for a person ----------


def test_draft_reply_for_person_by_name():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "draft a reply for shyam")
    assert hit is True
    assert calls[0] == ("mail_search", {"query": "shyam", "limit": 3})
    assert calls[1] == ("mail_draft", {"uid": "777"})


def test_draft_reply_no_hit_falls_through():
    orch = FakeOrch(search_hit="no matches found")
    hit, calls, _ = _run(orch, "draft a reply for xyzabc")
    assert hit is False
    assert ("mail_draft", {"uid": "777"}) not in [(c[0], c[1]) for c in calls]


# ---------- mark days ----------


def test_mark_weekday_as_event():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "mark friday as dentist")
    assert hit is True
    assert len(calls) == 1 and calls[0][0] == "mark_day"
    args = calls[0][1]
    assert args["title"] == "dentist"
    wd = (4 - date.today().weekday()) % 7 or 7
    assert args["date"] == (date.today() + timedelta(days=wd)).isoformat()


def test_mark_event_on_date():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "mark diwali party on october 20")
    assert hit is True
    args = calls[0][1]
    assert args["title"] == "diwali party"
    assert args["date"].endswith("-10-20")


def test_whats_on_day():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "what's on tomorrow")
    assert hit is True
    assert calls[0][0] == "day_events"
    assert calls[0][1]["date"] == (date.today() + timedelta(days=1)).isoformat()


def test_unparseable_day_falls_to_brain():
    orch = FakeOrch()
    hit, calls, _ = _run(orch, "mark diwali as holiday")
    assert hit is False
    assert calls == []


def test_parse_day_units():
    assert _parse_day("today") == date.today().isoformat()
    assert _parse_day("tomorrow") == (date.today() + timedelta(days=1)).isoformat()
    assert _parse_day("2026-12-25") == "2026-12-25"
    assert _parse_day("diwali") == ""
    assert _parse_day("") == ""
    assert _parse_day("not a date at all") == ""


# ---------- Gmail-native drafts (fake IMAP) ----------


class FakeBox:
    listed = [b'(\\HasNoChildren) "/" "[Gmail]/Drafts"']
    appended = []
    stored = []

    def list(self):
        return "OK", list(self.listed)

    def append(self, mailbox, flags, dt, data):
        FakeBox.appended.append((mailbox, bytes(data)))
        return "OK", [b"[APPENDUID 99 4242] done"]

    def select(self, mailbox):
        return "OK", [b"1"]

    def uid(self, *args):
        FakeBox.stored.append(args)
        return "OK", [b""]

    def expunge(self):
        return "OK", [b""]

    def close(self):
        return "OK", [b""]

    def logout(self):
        return "OK", [b""]


def test_save_gmail_draft_appends(monkeypatch):
    import core.mail as mail

    FakeBox.appended = []
    monkeypatch.setattr(mail, "_open", lambda: FakeBox())
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("MAIL_USER", "t@t.com")
    monkeypatch.setenv("MAIL_PASS", "x")
    res = mail.save_gmail_draft("a@b.com", "Re: hi", "hello there")
    assert res.get("uid") == "4242", res
    assert FakeBox.appended and FakeBox.appended[0][0] == "[Gmail]/Drafts"
    raw = FakeBox.appended[0][1]
    assert b"a@b.com" in raw and b"hello there" in raw


def test_delete_gmail_draft_marks_deleted(monkeypatch):
    import core.mail as mail

    FakeBox.stored = []
    monkeypatch.setattr(mail, "_open", lambda: FakeBox())
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("MAIL_USER", "t@t.com")
    monkeypatch.setenv("MAIL_PASS", "x")
    res = mail.delete_gmail_draft("4242")
    assert res == {"deleted": True}
    assert FakeBox.stored and FakeBox.stored[0][1] == "4242"


def test_delete_gmail_draft_empty_uid_noop():
    import core.mail as mail

    assert mail.delete_gmail_draft("") == {"deleted": False, "reason": "no uid"}


# ---------- mark_day / day_events tools (real scheduler) ----------


def _sched(tmp_path):
    from core.scheduler import TaskScheduler

    cfg = SimpleNamespace(db_path=tmp_path / "s.db")
    return TaskScheduler(cfg)


def test_mark_day_and_day_events_roundtrip(tmp_path):
    from core.tools import ToolBox

    sched = _sched(tmp_path)
    try:
        from skills.scheduler_skills import register_scheduler

        box = ToolBox()
        register_scheduler(box, sched)
        out = asyncio.run(
            box.execute("mark_day", json.dumps({"date": "2099-05-01", "title": "Diwali"}))
        )
        assert "marked 2099-05-01" in out and "Diwali" in out
        out2 = asyncio.run(box.execute("day_events", json.dumps({"date": "2099-05-01"})))
        assert "Diwali" in out2
        out3 = asyncio.run(box.execute("day_events", json.dumps({"date": "2099-05-02"})))
        assert "nothing marked" in out3
        bad = asyncio.run(box.execute("mark_day", json.dumps({"date": "tomorrow", "title": "x"})))
        assert bad.startswith("error:")
    finally:
        sched._conn.close()

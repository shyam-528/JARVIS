"""Wave 5 failure-path tests for the comms stack.

Covers the previously untested decision/failure cores of telegram.py,
whatsapp_inbox.py, wa_skills.py and mail_skills.py: owner gate, loud API
failure logging, bridge-down error dicts, number verification before
send, contact resolution, and the "state-write failures are logged /
unsaved drafts are reported, never announced" contract. No network,
no hardware — every boundary is faked.
"""

from __future__ import annotations

import json

from core import whatsapp_inbox as wa
from core.telegram import TelegramBot
from core.tools import ToolBox
from skills import mail_skills as ms
from skills import wa_skills as ws

# ---------- telegram: state + owner gate ----------


def _bot(tmp_path) -> TelegramBot:
    return TelegramBot("tok", orch=None, state_path=tmp_path / "tg.json")


def test_corrupt_state_file_relinks_loudly(tmp_path, capsys):
    (tmp_path / "tg.json").write_text("not json{{", encoding="utf-8")
    bot = _bot(tmp_path)
    assert bot.chat_id is None
    out = capsys.readouterr().out
    assert "corrupt store tg.json" in out
    # P0: quarantined aside (recoverable), not left corrupt to re-announce
    assert list(tmp_path.glob("tg.json.corrupt-*"))
    assert "quarantined" in out


async def test_first_chat_becomes_owner(tmp_path):
    bot = _bot(tmp_path)
    sent = []

    async def fake_send(text, **kw):
        sent.append(text)
        return True

    bot.send_message = fake_send
    await bot._handle_update({"message": {"chat": {"id": 111}, "text": "hi"}})
    assert bot.chat_id == "111"
    assert any("sole authorized user" in s for s in sent)
    # linked state persisted
    assert json.loads((tmp_path / "tg.json").read_text())["chat_id"] == "111"


async def test_other_chat_refused_and_never_processed(tmp_path):
    bot = _bot(tmp_path)
    bot.chat_id = "111"
    calls = []

    async def fake_call(method, **kw):
        calls.append(method)
        return {"ok": True}

    async def boom(text):
        raise AssertionError("unauthorized chat reached a command handler")

    bot._call = fake_call
    bot._handle_command = boom
    await bot._handle_update({"message": {"chat": {"id": 222}, "text": "/status"}})
    assert calls == ["sendMessage"]  # the refusal, nothing else


# ---------- telegram: _call / send_* failure contract ----------


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _FakeClient:
    def __init__(self, data=None, exc=None):
        self.data = data
        self.exc = exc

    async def post(self, *a, **k):
        if self.exc:
            raise self.exc
        return _Resp(self.data)


async def test_call_not_ok_returns_none_and_logs(tmp_path, capsys):
    bot = _bot(tmp_path)
    bot._client = _FakeClient({"ok": False, "description": "chat not found"})
    assert await bot._call("sendMessage") is None
    assert "sendMessage failed: chat not found" in capsys.readouterr().out


async def test_call_exception_returns_none_and_logs(tmp_path, capsys):
    bot = _bot(tmp_path)
    bot._client = _FakeClient(exc=TimeoutError("slow"))
    assert await bot._call("getMe") is None
    assert "getMe error: TimeoutError" in capsys.readouterr().out


async def test_send_message_false_when_call_fails(tmp_path):
    bot = _bot(tmp_path)
    bot.chat_id = "1"
    bot._client = _FakeClient({"ok": False, "description": "x"})
    assert await bot.send_message("hello") is False


async def test_send_photo_false_without_owner(tmp_path):
    bot = _bot(tmp_path)
    assert await bot.send_photo(b"\xff\xd8") is False  # never tries the API


# ---------- whatsapp_inbox: bridge failure + send-guard contract ----------


def test_available_false_when_bridge_down(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr("httpx.get", boom)
    assert wa.available() is False


def test_status_reports_bridge_down(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr("httpx.get", boom)
    st = wa.status()
    assert st["connected"] is False
    assert "bridge down" in st["error"]


def test_chats_error_dict_when_bridge_down(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr("httpx.get", boom)
    assert "error" in wa.chats()


def test_check_rejects_digitless_number_without_network(monkeypatch):
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network touched"))
    )
    res = wa.check("no digits here!")
    assert res["exists"] is False
    assert "no digits" in res["error"]


def test_send_requires_chat_and_text():
    assert "error" in wa.send("", "hi")
    assert "error" in wa.send("123@s.whatsapp.net", "")


def test_send_bare_number_blocked_when_not_on_whatsapp(monkeypatch):
    monkeypatch.setattr(wa, "check", lambda n: {"exists": False, "error": "nope"})
    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent unverified number")),
    )
    assert wa.send("919999988888", "hi")["error"] == "nope"


def test_send_pn_jid_reresolves_to_lid(monkeypatch):
    monkeypatch.setattr(
        wa, "check", lambda n: {"exists": True, "jid": "123@s.whatsapp.net", "lid": "999@lid"}
    )
    posted = {}

    class R:
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, **k):
        posted.update(json or {})
        return R()

    monkeypatch.setattr("httpx.post", fake_post)
    assert wa.send("123@s.whatsapp.net", "hi") == {"success": True}
    assert posted["chat"] == "999@lid"  # PN send would silently never arrive


def test_recent_inbound_filters_and_sorts(monkeypatch):
    monkeypatch.setattr(wa, "chats", lambda limit=15: {"chats": [{"id": "c1"}, {"id": "c2"}]})

    def fake_messages(chat, limit=10):
        if chat == "c2":
            return {"messages": []}
        return {
            "messages": [
                {"id": "a", "from": "x", "text": "old", "ts": 1},
                {"id": "b", "from": "me", "text": "mine", "mine": True, "ts": 9},
                {"id": "c", "from": "y", "text": "  ", "ts": 8},
                {"id": "d", "from": "z", "text": "new", "ts": 5},
            ]
        }

    monkeypatch.setattr(wa, "messages", fake_messages)
    out = wa.recent_inbound()
    assert [m["id"] for m in out] == ["d", "a"]  # newest first, mine/blank dropped
    assert all(m["chat"] in ("c1", "c2") for m in out)


def test_recent_inbound_empty_when_chats_error(monkeypatch):
    monkeypatch.setattr(wa, "chats", lambda limit=15: {"error": "down"})
    assert wa.recent_inbound() == []


# ---------- wa_skills: contact book + draft persistence contract ----------


def _contacts(tmp_path, monkeypatch, data=None):
    p = tmp_path / "contacts.json"
    if data is not None:
        p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ws, "_contacts_path", lambda: p)
    return p


def test_resolve_chat_passthrough_and_digits(tmp_path, monkeypatch):
    _contacts(tmp_path, monkeypatch, {})
    assert ws.resolve_chat("") is None
    assert ws.resolve_chat("123@s.whatsapp.net") == "123@s.whatsapp.net"
    assert ws.resolve_chat("+91 99999 88888") == "919999988888@s.whatsapp.net"


def test_resolve_chat_contact_book(tmp_path, monkeypatch):
    _contacts(tmp_path, monkeypatch, {"aslam": "111@s.whatsapp.net", "asha": "222@s.whatsapp.net"})
    assert ws.resolve_chat("Aslam") == "111@s.whatsapp.net"
    assert ws.resolve_chat("asla") == "111@s.whatsapp.net"  # unique prefix
    assert ws.resolve_chat("as") is None  # ambiguous prefix
    assert ws.resolve_chat("nobody") is None


def test_save_contacts_failure_is_loud(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a dir")
    monkeypatch.setattr(ws, "_contacts_path", lambda: blocker / "c.json")
    ws._save_contacts({"a": "b"})  # must not raise
    assert "contact book NOT saved" in capsys.readouterr().out


async def test_wa_send_unknown_contact_errors(tmp_path, monkeypatch):
    _contacts(tmp_path, monkeypatch, {})
    box = ToolBox()
    ws.register_wa(box)
    res = await box.execute("wa_send", json.dumps({"chat": "Nobody", "text": "hi"}))
    assert res.startswith("error: unknown contact")


class _Router:
    async def chat(self, *a, **k):
        yield ("", "Draft body here")


async def test_wa_draft_save_failure_reported_not_announced(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a dir")
    monkeypatch.setattr(ws, "_drafts_path", lambda: blocker / "wa_drafts.json")
    box = ToolBox()
    ws.register_wa(box, router=_Router())
    res = await box.execute("wa_draft", json.dumps({"chat": "c1@s.whatsapp.net", "text": "hi"}))
    assert res.startswith("error:")
    assert "could not save" in res
    assert "send it" not in res  # unsaved draft must never look sendable
    assert "draft NOT saved" in capsys.readouterr().out


# ---------- mail_skills: draft persistence contract ----------


def test_fmt_error_passthrough_and_empty():
    assert ms._fmt({"error": "boom"}) == "error: boom"
    assert ms._fmt({"emails": []}) == "No matching mail."
    out = ms._fmt({"emails": [{"id": "1", "date": "d", "from": "f", "subject": "s"}]})
    assert "[1] d | f | s" in out


def test_save_draft_without_recipient_refused(tmp_path, monkeypatch):
    p = tmp_path / "mail_drafts.json"
    monkeypatch.setattr(ms, "_drafts_path", lambda: p)
    assert ms._save_draft({"from": "", "subject": "x"}, "body") == ""
    assert not p.exists()


def test_save_draft_parses_recipient(tmp_path, monkeypatch):
    p = tmp_path / "mail_drafts.json"
    monkeypatch.setattr(ms, "_drafts_path", lambda: p)
    did = ms._save_draft({"from": "Bob <bob@x.com>", "subject": "Hi", "id": "7"}, "body")
    assert did.startswith("d")
    drafts = json.loads(p.read_text())
    assert drafts[0]["to"] == "bob@x.com"
    assert drafts[0]["subject"] == "Re: Hi"


def test_save_draft_write_failure_returns_empty_and_logs(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a dir")
    monkeypatch.setattr(ms, "_drafts_path", lambda: blocker / "mail_drafts.json")
    assert ms._save_draft({"from": "a@b.c", "subject": "s"}, "body") == ""
    assert "draft NOT saved" in capsys.readouterr().out


async def test_broken_draft_never_blind_sent(tmp_path, monkeypatch):
    p = tmp_path / "mail_drafts.json"
    p.write_text(
        json.dumps([{"id": "d1", "to": "", "subject": "s", "body": "b", "for_uid": "999"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(ms, "_drafts_path", lambda: p)
    monkeypatch.setattr(ms._mail, "header", lambda uid: {"error": "gone"})
    monkeypatch.setattr(
        ms._mail,
        "send",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("blind send attempted")),
    )
    box = ToolBox()
    ms.register_mailtriage(box)
    res = await box.execute("mail_send_draft", json.dumps({"id": "d1"}))
    assert res.startswith("error:")
    assert "can't be resolved" in res


# ---------- watchers: baseline-first (no spam on first run) ----------


class _Notify:
    def __init__(self):
        self.calls = []

    async def notify(self, message="", title=""):
        self.calls.append((title, message))
        return {"success": True}


async def test_mail_watcher_first_run_baselines_silently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # state paths are cwd-relative
    monkeypatch.setattr(ms._mail, "configured", lambda: True)
    monkeypatch.setattr(
        ms._mail,
        "unread",
        lambda limit=25: {"emails": [{"id": "1", "from": "a@b.c", "subject": "hi", "date": "d"}]},
    )
    notify = _Notify()
    box = ToolBox()
    ms.register_mailwatch(box, notify)
    res = await box.execute("mail_check_new", "{}")
    assert "baseline recorded" in res
    assert notify.calls == []
    assert (tmp_path / "data" / "mail_seen.json").exists()


async def test_wa_watcher_first_run_baselines_silently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wa, "status", lambda: {"connected": True})
    monkeypatch.setattr(
        wa,
        "recent_inbound",
        lambda: [{"id": "w1", "chat": "c1", "from": "Aslam", "text": "hi", "ts": 1}],
    )
    notify = _Notify()
    box = ToolBox()
    ws.register_wawatch(box, notify)
    res = await box.execute("wa_check_new", "{}")
    assert "baseline recorded" in res
    assert notify.calls == []
    assert (tmp_path / "data" / "wa_seen.json").exists()


async def test_wa_watcher_bridge_down_reports_not_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wa, "status", lambda: {"connected": False, "error": "pair first"})
    box = ToolBox()
    ws.register_wawatch(box, _Notify())
    res = await box.execute("wa_check_new", "{}")
    assert "bridge not connected" in res


# ---------- boot smoke: all comms tools register ----------


def test_comms_tools_register():
    box = ToolBox()
    ms.register_mail(box)
    ms.register_mailtriage(box)
    ms.register_mailwatch(box, _Notify())
    ws.register_wa(box)
    ws.register_wawatch(box, _Notify())
    names = set(box.names())
    expected = {
        "mail_unread",
        "mail_search",
        "mail_read",
        "mail_triage",
        "mail_draft",
        "mail_send_draft",
        "mail_check_new",
        "wa_chats",
        "wa_read",
        "wa_send",
        "wa_contact",
        "wa_triage",
        "wa_draft",
        "wa_send_draft",
        "wa_check_new",
    }
    assert expected <= names

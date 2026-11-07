"""Autonomous mail triage: tiers, drafts, approval-gated send."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import mail_triage as tri


def test_vip_most_important(monkeypatch):
    monkeypatch.setenv("MAIL_VIP", "boss@corp.com")
    assert tri.classify({"from": "Boss <boss@corp.com>", "subject": "hi"})[0] == "most_important"


def test_urgent_keywords(monkeypatch):
    monkeypatch.setenv("MAIL_VIP", "")
    tier, _ = tri.classify({"from": "x@y.com", "subject": "Invoice #42 overdue"}, "")
    assert tier == "most_important"
    tier, _ = tri.classify({"from": "x@y.com", "subject": "hello"}, "")
    assert tier == "important"


def test_bulk_low(monkeypatch):
    monkeypatch.setenv("MAIL_VIP", "")
    assert tri.classify({"from": "news@shop.com", "subject": "sale", "bulk": True})[0] == "low"
    assert tri.classify({"from": "notify@github.com", "subject": "run failed", "bulk": True})[
        0
    ] in ("important", "low")


def test_summarize_counts():
    out = tri.summarize({"most_important": [{"id": "1", "from": "a", "subject": "s"}], "low": []})
    assert "most_important (1)" in out


async def test_draft_and_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("MAIL_USER", "me@x.com")
    monkeypatch.setenv("MAIL_PASS", "pw")
    from core import mail as mailmod
    from core.tools import ToolBox
    from skills.mail_skills import register_mailtriage

    monkeypatch.setattr(
        mailmod, "read", lambda uid, max_chars=2000: {"id": uid, "body": "Please send the report."}
    )
    monkeypatch.setattr(
        mailmod,
        "header",
        lambda uid: {"id": uid, "from": "Boss <boss@x.com>", "subject": "Report", "date": "d"},
    )
    monkeypatch.setattr(
        mailmod,
        "unread",
        lambda limit=50: {
            "emails": [{"id": "99", "from": "Boss <boss@x.com>", "subject": "Report", "date": "d"}],
            "total": 1,
        },
    )

    async def fake_chat(messages, tools=None, task_type="fast"):
        yield "x", "Thanks — on it. Best regards"

    router = type("R", (), {"chat": staticmethod(fake_chat)})()
    box = ToolBox()
    register_mailtriage(box, router)
    r = await box.execute("mail_draft", '{"uid": "99"}')
    assert "NOT sent" in r and "Thanks" in r
    # no id + exactly one draft -> sends via fake SMTP
    import smtplib

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, m):
            self.sent = m

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    r2 = await box.execute("mail_send_draft", "{}")
    assert r2.startswith("sent to")
    r3 = await box.execute("mail_send_draft", "{}")
    assert "no pending drafts" in r3


async def test_send_unknown_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    from core.tools import ToolBox
    from skills.mail_skills import register_mailtriage

    box = ToolBox()
    register_mailtriage(box, None)
    assert "no draft" in await box.execute("mail_send_draft", '{"id": "d1"}')


def _mailbox(monkeypatch, tmp_path, router=None):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    from core.tools import ToolBox
    from skills.mail_skills import register_mailtriage

    box = ToolBox()
    register_mailtriage(box, router)
    return box


async def _router():
    async def fake_chat(messages, tools=None, task_type="fast"):
        yield "x", "Thanks — on it. Best regards"

    return type("R", (), {"chat": staticmethod(fake_chat)})()


async def test_draft_resolves_read_mail_via_header(tmp_path, monkeypatch):
    """Uid absent from unread list (already read) still drafts WITH address."""
    from core import mail as mailmod

    box = _mailbox(monkeypatch, tmp_path, await _router())
    monkeypatch.setattr(
        mailmod, "read", lambda uid, max_chars=2000: {"id": uid, "body": "Here is the API key."}
    )
    monkeypatch.setattr(
        mailmod,
        "header",
        lambda uid: {"id": uid, "from": "Tejas <tejas@x.com>", "subject": "API key", "date": "d"},
    )
    monkeypatch.setattr(mailmod, "unread", lambda limit=50: {"emails": [], "total": 0})
    r = await box.execute("mail_draft", '{"uid": "725"}')
    assert "NOT sent" in r and "tejas@x.com" in r


async def test_draft_refuses_when_unresolvable(tmp_path, monkeypatch):
    """Never save an address-less draft that could never send."""

    from core import mail as mailmod

    box = _mailbox(monkeypatch, tmp_path, await _router())
    monkeypatch.setattr(mailmod, "read", lambda uid, max_chars=2000: {"id": uid, "body": "hi"})
    monkeypatch.setattr(
        mailmod, "header", lambda uid: {"error": "no mail 999 (id may have shifted)"}
    )
    r = await box.execute("mail_draft", '{"uid": "999"}')
    assert "error" in r
    assert not (tmp_path / "data" / "mail_drafts.json").exists()


def _write_legacy_broken_draft(tmp_path):
    import json

    drafts = [
        {
            "id": "d17167",
            "to": "",
            "subject": "Re: ",
            "body": "Hi Tejas, thanks.",
            "for_uid": "725",
            "created": "2026-09-11 14:29",
        }
    ]
    (tmp_path / "data" / "mail_drafts.json").write_text(json.dumps(drafts))


class _FakeSMTP:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, m):
        self.sent = m


async def test_send_broken_draft_repairs_then_confirms(tmp_path, monkeypatch):
    """Legacy empty-to draft: first send repairs + asks to confirm (no blind
    send to a re-resolved address); second send delivers."""
    import json
    import smtplib

    from core import mail as mailmod

    box = _mailbox(monkeypatch, tmp_path)
    _write_legacy_broken_draft(tmp_path)
    monkeypatch.setattr(
        mailmod,
        "header",
        lambda uid: {"id": uid, "from": "Tejas <tejas@x.com>", "subject": "API key", "date": "d"},
    )
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("MAIL_USER", "me@x.com")
    monkeypatch.setenv("MAIL_PASS", "pw")
    r1 = await box.execute("mail_send_draft", "{}")
    assert "repaired" in r1 and "tejas@x.com" in r1 and "again" in r1
    saved = json.loads((tmp_path / "data" / "mail_drafts.json").read_text())
    assert saved[0]["to"] == "tejas@x.com"
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    r2 = await box.execute("mail_send_draft", "{}")
    assert r2.startswith("sent to tejas@x.com")


async def test_send_broken_draft_unresolvable(tmp_path, monkeypatch):
    from core import mail as mailmod

    box = _mailbox(monkeypatch, tmp_path)
    _write_legacy_broken_draft(tmp_path)
    monkeypatch.setattr(
        mailmod, "header", lambda uid: {"error": "no mail 725 (id may have shifted)"}
    )
    r = await box.execute("mail_send_draft", "{}")
    assert "no recipient" in r and "/mail send d17167 <address>" in r


async def test_send_broken_draft_to_override(tmp_path, monkeypatch):
    """Explicit address = confirmation: sends immediately."""
    import smtplib

    box = _mailbox(monkeypatch, tmp_path)
    _write_legacy_broken_draft(tmp_path)
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("MAIL_USER", "me@x.com")
    monkeypatch.setenv("MAIL_PASS", "pw")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    r = await box.execute("mail_send_draft", '{"id": "d17167", "to": "tejas@x.com"}')
    assert r.startswith("sent to tejas@x.com")


def test_expiry_keywords_most_important(monkeypatch):
    monkeypatch.delenv("MAIL_VIP", raising=False)
    from core import mail_triage as tri

    for subj in (
        "Your API key expires in 3 days",
        "Subscription ending soon",
        "Payment failed - action needed",
        "Security alert: new sign-in",
        "Your free trial ends tomorrow",
    ):
        tier, reason = tri.classify({"from": "service@example.com", "subject": subj})
        assert tier == "most_important", subj
        assert ("expiry" in reason) or ("urgent" in reason)  # either high-priority class is correct


def test_expiry_bulk_sender_only_important(monkeypatch):
    monkeypatch.delenv("MAIL_VIP", raising=False)
    from core import mail_triage as tri

    tier, _ = tri.classify(
        {"from": "news@shop.example", "subject": "Sale ending soon!", "bulk": True}
    )
    assert tier == "important"


def test_normal_mail_unaffected(monkeypatch):
    monkeypatch.delenv("MAIL_VIP", raising=False)
    from core import mail_triage as tri

    tier, _ = tri.classify({"from": "friend@example.com", "subject": "Lunch tomorrow?"})
    assert tier == "important"  # direct personal mail, unchanged

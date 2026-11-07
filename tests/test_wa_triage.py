"""WhatsApp triage: tiers, drafts, approval, watcher baseline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.whatsapp_inbox as wamod
from core.tools import ToolBox
from skills.wa_skills import register_wa, register_wawatch


def _box(router=None):
    box = ToolBox()
    register_wa(box, router)
    return box


async def test_triage_tiers(monkeypatch):
    items = [
        {"id": "1", "chat": "c", "from": "boss@corp.com", "text": "call me now", "ts": 3},
        {"id": "2", "chat": "c", "from": "promo@shop.com", "text": "SALE -50%", "ts": 2},
        {"id": "3", "chat": "c", "from": "mom", "text": "dinner sunday?", "ts": 1},
    ]
    monkeypatch.setenv("MAIL_VIP", "boss@corp.com")
    monkeypatch.setattr(wamod, "recent_inbound", lambda *a, **k: list(items))
    monkeypatch.setattr(wamod, "status", lambda: {"connected": True})
    box = _box()
    out = await box.execute("wa_triage", "{}")
    assert "most_important (1)" in out
    assert "low (1)" in out
    assert "important (1)" in out


async def test_draft_and_send(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)

    async def fake_chat(messages, tools=None, task_type="fast"):
        yield "x", "Sure, will do. Best regards"

    router = type("R", (), {"chat": staticmethod(fake_chat)})()
    box = _box(router)
    r = await box.execute("wa_draft", '{"chat": "91999@s.whatsapp.net", "text": "send report"}')
    assert "NOT sent" in r and "Sure" in r
    r2 = await box.execute("wa_send_draft", "{}")
    assert r2.startswith("sent to") or "error" in r2  # bridge down in CI -> graceful error


async def test_watcher_baseline_then_new(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    monkeypatch.setattr(wamod, "status", lambda: {"connected": True})
    items = [{"id": "1", "chat": "c", "from": "a", "text": "hi", "ts": 1}]
    monkeypatch.setattr(wamod, "recent_inbound", lambda *a, **k: list(items))

    box = ToolBox()
    sent = []

    class Manager:
        async def notify(self, **kw):
            sent.append(kw)
            return {"success": True}

    register_wawatch(box, Manager(), None)
    assert "baseline" in await box.execute("wa_check_new", "{}")
    assert "no new WhatsApp" in await box.execute("wa_check_new", "{}")
    items.append({"id": "2", "chat": "c", "from": "b", "text": "urgent: call", "ts": 2})
    r = await box.execute("wa_check_new", "{}")
    assert "triaged 1" in r and len(sent) == 1


async def test_bridge_down_graceful(monkeypatch):
    monkeypatch.setattr(wamod, "status", lambda: {"connected": False, "error": "pair first"})
    monkeypatch.setattr(wamod, "chats", lambda limit=20: {"error": "bridge down"})
    monkeypatch.setattr(wamod, "recent_inbound", lambda *a, **k: [])
    box = _box()
    assert "pair" in await box.execute("wa_triage", "{}")
    box2 = ToolBox()
    register_wawatch(box2, None)
    assert "not connected" in await box2.execute("wa_check_new", "{}")


async def test_direct_send(monkeypatch):
    from skills.wa_skills import register_wa as _reg

    box = ToolBox()
    _reg(box, None)
    assert "required" in await box.execute("wa_send", '{"chat": "", "text": ""}')
    monkeypatch.setattr(
        wamod,
        "send",
        lambda chat, text: {"success": True} if chat == "91999@s.whatsapp.net" else {"error": "x"},
    )
    assert "sent to" in await box.execute("wa_send", '{"chat": "91999", "text": "hi"}')


async def test_contacts_and_name_send(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    from skills.wa_skills import register_wa as _reg2
    from skills.wa_skills import resolve_chat

    box = ToolBox()
    _reg2(box, None)
    assert "saved contact Aslam" in await box.execute(
        "wa_contact", '{"action": "add", "name": "Aslam", "chat": "919999999999"}'
    )
    assert resolve_chat("aslam") == "919999999999@s.whatsapp.net"
    assert resolve_chat("ASLAM") == "919999999999@s.whatsapp.net"
    assert resolve_chat("asl") == "919999999999@s.whatsapp.net"
    assert resolve_chat("nobody") is None
    assert "contacts" in await box.execute("wa_contact", '{"action": "list"}')
    monkeypatch.setattr(
        wamod,
        "send",
        lambda chat, text: (
            {"success": True} if chat == "919999999999@s.whatsapp.net" else {"error": "x"}
        ),
    )
    assert "sent to" in await box.execute("wa_send", '{"chat": "aslam", "text": "hi"}')
    assert "unknown contact" in await box.execute("wa_send", '{"chat": "stranger", "text": "hi"}')
    assert "deleted" in await box.execute("wa_contact", '{"action": "del", "name": "aslam"}')
    assert resolve_chat("aslam") is None


def _resp(status=200, payload=None):
    class R:
        status_code = status

        def json(self):
            if isinstance(payload, Exception):
                raise payload
            return payload

    return R()


async def test_check_prefers_lid(monkeypatch):
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _resp(
            200, {"exists": True, "jid": "79042@s.whatsapp.net", "lid": "123@lid"}
        ),
    )
    from core import whatsapp_inbox as wai

    assert wai.check("79042") == {"exists": True, "jid": "79042@s.whatsapp.net", "lid": "123@lid"}


async def test_check_old_bridge_not_misreported(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(404, ValueError("no json")))
    from core import whatsapp_inbox as wai

    r = wai.check("79042")
    assert r["exists"] is False and "too old" in r["error"]


async def test_send_uses_lid(monkeypatch):
    import httpx

    seen = {}
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _resp(
            200, {"exists": True, "jid": "79042@s.whatsapp.net", "lid": "123@lid"}
        ),
    )

    def fake_post(url, **kw):
        seen.update(kw.get("json", {}))
        return _resp(200, {"ok": True, "id": "ABC"})

    monkeypatch.setattr(httpx, "post", fake_post)
    from core import whatsapp_inbox as wai

    assert wai.send("79042@s.whatsapp.net", "hi") == {"success": True}
    assert seen.get("chat") == "123@lid"

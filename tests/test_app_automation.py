"""App automations: WhatsApp channel + web-watch monitor."""

from core.notify import Channel, NotificationConfig, NotificationSender
from core.tools import ToolBox
from skills.webwatch import _fingerprint, register_webwatch


class _Resp:
    def __init__(self, status=200, text="Message queued."):
        self.status_code = status
        self.text = text


class _Client:
    def __init__(self, text="Message queued."):
        self.text = text

    async def get(self, url):
        assert "callmebot.com" in url and "apikey=" in url
        return _Resp(text=self.text)


def _sender(**kw):
    cfg = NotificationConfig(whatsapp_phone="919999999999", whatsapp_apikey="abc123", **kw)
    s = NotificationSender(cfg)
    s._client = _Client()
    return s


async def test_whatsapp_send_ok():
    r = await _sender()._send_whatsapp("hi", "T")
    assert r["success"] and r["channel"] == "whatsapp"


async def test_whatsapp_bad_response():
    s = _sender()
    s._client = _Client(text="ERROR: unauthorized")
    r = await s._send_whatsapp("hi", "")
    assert not r["success"]


async def test_whatsapp_skipped_unconfigured():
    s = NotificationSender(NotificationConfig())
    r = await s.send("hi", channel=Channel.WHATSAPP)
    assert not r["success"]


async def test_fingerprint_ignores_tags():
    d1, _ = _fingerprint("<html><b>Price 99</b><script>var x=1</script></html>")
    d2, _ = _fingerprint("<html><i>Price 99</i><script>var y=2</script></html>")
    d3, _ = _fingerprint("<html><b>Price 109</b></html>")
    assert d1 == d2 and d1 != d3


async def test_watch_baseline_then_change(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    import skills.webwatch as ww

    pages = {"html": "<p>Price 99</p>"}

    async def fake_fetch(url):
        return pages["html"]

    monkeypatch.setattr(ww, "_fetch", fake_fetch)
    box = ToolBox()
    register_webwatch(box)
    r1 = await box.execute("web_watch", '{"url": "https://x.test/item"}')
    assert "baseline" in r1
    r2 = await box.execute("web_watch", '{"url": "https://x.test/item"}')
    assert "unchanged" in r2
    pages["html"] = "<p>Price 79</p>"
    r3 = await box.execute("web_watch", '{"url": "https://x.test/item"}')
    assert "CHANGED" in r3
    r4 = await box.execute("web_unwatch", "{}")
    assert "x.test" in r4

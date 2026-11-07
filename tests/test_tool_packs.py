"""Tool packs: small core payload, keyword packs, load_pack expansion."""

from core.tools import CORE_TOOLS, default_toolbox, detect_packs, pack_tools


def _box():
    from core.code_agent import CodeAgent

    try:
        return default_toolbox(code_agent=CodeAgent())
    except Exception:
        return default_toolbox()


def test_core_small():
    box = _box()
    got = box.schemas_for(CORE_TOOLS)
    names = [t["function"]["name"] for t in got]
    assert "load_pack" in names
    assert "read_file" in names
    assert len(got) <= 40, f"core too big: {len(got)}"


def test_detect_packs():
    assert "mail" in detect_packs("do i have unread mail?")
    assert "browser" in detect_packs("open that website and click login")
    assert "github" in detect_packs("open a pull request")
    assert detect_packs("what is python") == []


def test_pack_contents():
    box = _box()
    mail = pack_tools(box, "mail")
    assert set(mail) == {"mail_unread", "mail_search", "mail_read"}
    assert pack_tools(box, "nope") == []
    browser = pack_tools(box, "browser")
    assert "browser_navigate" in browser and len(browser) >= 5


def test_schemas_for_skips_unknown():
    box = _box()
    got = box.schemas_for(["read_file", "does_not_exist"])
    assert [t["function"]["name"] for t in got] == ["read_file"]


def test_load_pack_tool():
    import asyncio

    box = _box()
    out = asyncio.run(box.execute("load_pack", '{"pack": "mail"}'))
    assert "mail_unread" in out
    out2 = asyncio.run(box.execute("load_pack", '{"pack": "nope"}'))
    assert "unknown pack" in out2


def test_plain_chat_stays_small():
    # "hi" triggers no packs -> core only
    box = _box()
    from core.tools import detect_packs as dp

    names = list(CORE_TOOLS)
    assert len(box.schemas_for(names)) <= 40
    assert dp("hi") == []

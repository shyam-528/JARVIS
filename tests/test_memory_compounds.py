"""Memory that compounds: anniversary, editable prefs, per-project memory."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _memory(tmp_path):
    from core.memory import MemoryStore

    return MemoryStore(tmp_path / "memory.db")


def _persistence(tmp_path):
    from core.persistence import ConversationStore

    return ConversationStore(tmp_path / "conversations.db")


def _prefs(tmp_path):
    from core.preferences import PreferenceManager

    return PreferenceManager(tmp_path / "prefs.json")


# ---------- anniversary ----------


def test_anniversary_finds_same_month_day_prior_years(tmp_path):
    import os

    os.environ["JARVIS_NO_EMBEDDINGS"] = "1"
    from core.anniversary import (
        anniversary_text,
        memory_anniversaries,
        session_anniversaries,
    )

    mem = _memory(tmp_path)
    now = datetime.now()
    same = now.replace(year=now.year - 1).strftime("%Y-%m-%d %H:%M")
    other = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    mem.conn.execute(
        "INSERT INTO memories (ts, kind, text) VALUES (?, 'fact', 'bought the red bike')", (same,)
    )
    mem.conn.execute(
        "INSERT INTO memories (ts, kind, text) VALUES (?, 'fact', 'random other day')", (other,)
    )
    mem.conn.commit()
    got = memory_anniversaries(mem, now)
    assert [g["text"] for g in got] == ["bought the red bike"]
    assert got[0]["years_ago"] == 1

    per = _persistence(tmp_path)
    sid = per.create_session("Anniversary chat")
    old_ts = (now.replace(year=now.year - 2) - timedelta(days=1)).timestamp()
    per.conn.execute(
        "UPDATE sessions SET created_at = ?, updated_at = ? WHERE id = ?", (old_ts, old_ts, sid)
    )
    per.conn.commit()
    sess = session_anniversaries(per, now)
    assert any(s["title"] == "Anniversary chat" and s["years_ago"] == 2 for s in sess)

    text = anniversary_text(mem, per, now)
    assert "year ago today" in text and "red bike" in text


def test_anniversary_quiet_past(tmp_path):
    import os

    os.environ["JARVIS_NO_EMBEDDINGS"] = "1"
    from core.anniversary import anniversary_text

    assert anniversary_text(_memory(tmp_path), _persistence(tmp_path)) == ""


# ---------- editable preferences ----------


def test_prefs_set_list_forget(tmp_path):
    prefs = _prefs(tmp_path)
    assert prefs.list_all() == {}
    assert prefs.set_value("Brief Time", "7:30 AM", "workflow") is True
    assert prefs.get("brief_time") == "7:30 AM"
    assert prefs.set_value("", "x") is False
    assert prefs.set_value("k", "") is False
    prefs.set_value("brief_time", "8 AM")  # overwrite
    assert prefs.get("brief_time") == "8 AM"
    # persisted
    assert _prefs(tmp_path).get("brief_time") == "8 AM"
    assert prefs.remove("brief_time") is True
    assert prefs.remove("brief_time") is False


async def test_prefs_tools(tmp_path):
    from core.tools import ToolBox
    from skills.memory import register_prefs

    box = ToolBox()
    register_prefs(box, _prefs(tmp_path))
    assert {"prefs_list", "prefs_set", "prefs_forget"} <= set(box.names())
    assert "No learned" in await box.execute("prefs_list", "{}")
    assert "set." in await box.execute("prefs_set", '{"key": "tea", "value": "masala chai"}')
    out = await box.execute("prefs_list", "{}")
    assert "masala chai" in out
    assert "forgot" in await box.execute("prefs_forget", '{"key": "tea"}')


# ---------- per-project memory ----------


def test_project_remember_recall_standup(tmp_path):
    from core.project_memory import ProjectMemory

    pm = ProjectMemory(tmp_path)
    assert pm.recall("mark3") == ""
    assert pm.remember("mark3", "convention", "pytest before commit")["ok"] is True
    assert pm.remember("mark3", "thread", "fix storm drain")["ok"] is True
    assert pm.remember("mark3", "standup", "shipped phase C")["ok"] is True
    assert pm.remember("mark3", "standup", "started storm queue")["ok"] is True  # replaces
    card = pm.recall("MARK3")  # case-insensitive resolve
    assert "pytest before commit" in card and "fix storm drain" in card
    assert "started storm queue" in card and "shipped phase C" not in card
    assert pm.remember("mark3", "bogus", "x")["ok"] is False
    assert pm.remember("", "note", "x")["ok"] is False
    names = [p["name"] for p in pm.list()]
    assert names == ["mark3"]


def test_project_forget(tmp_path):
    from core.project_memory import ProjectMemory

    pm = ProjectMemory(tmp_path)
    pm.remember("site", "thread", "fix header css")
    pm.remember("site", "note", "uses tailwind")
    assert pm.forget("site", "header")["ok"] is True
    assert "header" not in pm.recall("site")
    assert pm.forget("nope")["ok"] is False
    assert pm.forget("site")["ok"] is True
    assert pm.recall("site") == ""


async def test_project_tools(tmp_path):
    from core.tools import ToolBox
    from skills.memory import register_anniversary, register_project_memory

    box = ToolBox()
    register_project_memory(box, tmp_path)
    assert {"project_remember", "project_recall", "project_list"} <= set(box.names())
    assert "noted" in await box.execute(
        "project_remember",
        '{"project": "jarvis-web", "kind": "convention", "text": "tsc before build"}',
    )
    assert "tsc before build" in await box.execute("project_recall", '{"project": "jarvis-web"}')
    assert "jarvis-web" in await box.execute("project_list", "{}")

    import os

    os.environ["JARVIS_NO_EMBEDDINGS"] = "1"
    register_anniversary(box, _memory(tmp_path), _persistence(tmp_path))
    assert "memory_anniversary" in box.names()
    assert "quiet past" in await box.execute("memory_anniversary", "{}")


def test_memory_pack_picks_new_tools():
    from core.tools import ToolBox, detect_packs, pack_tools

    box = ToolBox()
    for name in (
        "memory_anniversary",
        "prefs_list",
        "prefs_set",
        "project_remember",
        "project_recall",
    ):
        box.register(
            __import__("core.tools", fromlist=["Tool"]).Tool(
                name=name,
                description="x",
                parameters={"type": "object", "properties": {}},
                handler=lambda: "x",
            )
        )
    assert "memory" in detect_packs("a year ago today, what did we do?")
    assert "memory" in detect_packs("show my preferences")
    assert "memory" in detect_packs("what open threads does the project have?")
    got = pack_tools(box, "memory")
    assert {"memory_anniversary", "prefs_list", "project_recall"} <= set(got)


def test_anniversary_survives_odd_rows(tmp_path):
    import os

    os.environ["JARVIS_NO_EMBEDDINGS"] = "1"
    from core.anniversary import memory_anniversaries, session_anniversaries

    mem = _memory(tmp_path)
    mem.conn.execute("INSERT INTO memories (ts, kind, text) VALUES ('not-a-date', 'fact', 'junk')")
    mem.conn.commit()
    assert memory_anniversaries(mem) == []
    per = _persistence(tmp_path)
    assert session_anniversaries(per) == []

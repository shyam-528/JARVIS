"""Storm-proof answers: queue while brains are down, deliver when back."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.router import AllProvidersDown
from core.storm_queue import MAX_PENDING, StormQueue, should_queue
from core.tools import ToolBox


def test_should_queue_only_total_outage():
    assert should_queue(AllProvidersDown("down"))
    assert not should_queue(ValueError("bug"))
    assert not should_queue(RuntimeError("boom"))


def test_enqueue_dedupe_and_guards(tmp_path):
    q = StormQueue(tmp_path)
    r1 = q.enqueue("cli", "  what is the time? ")
    assert r1["queued"] and r1["id"]
    r2 = q.enqueue("telegram", "what is the time?")
    assert r2["queued"] and r2["id"] == r1["id"]  # duplicate
    assert q.enqueue("cli", "")["queued"] is False
    assert q.enqueue("web", "/tasks")["queued"] is False
    assert q.status()["pending"] == 1


def test_cap_and_expiry(tmp_path):
    from datetime import datetime, timedelta

    q = StormQueue(tmp_path)
    for i in range(MAX_PENDING):
        assert q.enqueue("cli", f"question number {i}")["queued"] is True
    assert "full" in q.enqueue("cli", "one more")["reason"]
    # age one item past expiry -> next enqueue expires it, freeing a slot
    items = q._load()
    items[0]["ts"] = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    q._save(items)
    assert q.enqueue("cli", "fresh question")["queued"] is True


def test_claim_release_deliver(tmp_path):
    q = StormQueue(tmp_path)
    q.enqueue("cli", "q one")
    q.enqueue("tg", "q two")
    got = q.claim(limit=1)
    assert len(got) == 1 and got[0]["status"] == "inflight"
    assert len(q.pending()) == 2  # inflight still counts
    q.release(got[0]["id"])
    assert q._load()[0]["status"] == "queued"
    got = q.claim()
    assert len(got) == 2
    q.mark_delivered(got[0]["id"], "preview")
    assert q.status()["pending"] == 1


def _orch(tmp_path, brains=True, answer="The answer.", fail=None):
    notes = []

    class FakeRouter:
        def available(self):
            return ["fake-brain"] if brains else []

        async def chat(self, msgs):
            if fail is not None:
                raise fail
            yield "fake-brain", answer

    async def notify(message="", title="", **kw):
        notes.append((title, message))
        return {"success": True}

    async def speak(text):
        notes.append(("voice", text))
        return True

    orch = SimpleNamespace(
        cfg=SimpleNamespace(db_path=tmp_path / "usage.db"),
        router=FakeRouter(),
        notify_manager=SimpleNamespace(notify=notify),
        speak_alert=speak,
    )
    orch._notes = notes
    return orch


async def test_drain_skipped_while_down(tmp_path):
    from skills.storm_skills import register_storm

    orch = _orch(tmp_path, brains=False)
    box = ToolBox()
    register_storm(box, orch)
    assert {"storm_drain", "storm_status"} <= set(box.names())
    StormQueue(tmp_path).enqueue("cli", "will it rain?")
    out = await box.execute("storm_drain", "{}")
    assert "still down" in out and len(StormQueue(tmp_path).pending()) == 1


async def test_drain_delivers_telegram_plus_voice(tmp_path):
    from skills.storm_skills import register_storm

    orch = _orch(tmp_path, brains=True, answer="Sunny, sir.")
    box = ToolBox()
    register_storm(box, orch)
    StormQueue(tmp_path).enqueue("telegram", "weather?")
    out = await box.execute("storm_drain", "{}")
    assert "1 delivered" in out
    titles = [t for t, _ in orch._notes]
    assert "Storm answer" in titles and "voice" in titles
    body = next(m for t, m in orch._notes if t == "Storm answer")
    assert "weather?" in body and "Sunny" in body
    assert StormQueue(tmp_path).status()["pending"] == 0


async def test_drain_keeps_queued_on_mid_drain_outage(tmp_path):
    from skills.storm_skills import register_storm

    orch = _orch(tmp_path, brains=True, fail=AllProvidersDown("quota died again"))
    box = ToolBox()
    register_storm(box, orch)
    StormQueue(tmp_path).enqueue("cli", "still there?")
    out = await box.execute("storm_drain", "{}")
    assert "kept queued" in out
    assert StormQueue(tmp_path).status()["pending"] == 1


async def test_drain_caps_per_run(tmp_path):
    from skills.storm_skills import register_storm

    orch = _orch(tmp_path, brains=True)
    box = ToolBox()
    register_storm(box, orch)
    for i in range(5):
        StormQueue(tmp_path).enqueue("cli", f"bulk question {i}")
    out = await box.execute("storm_drain", "{}")
    assert "3 delivered" in out
    assert StormQueue(tmp_path).status()["pending"] == 2


async def test_status_tool(tmp_path):
    from skills.storm_skills import register_storm

    orch = _orch(tmp_path)
    box = ToolBox()
    register_storm(box, orch)
    assert "empty" in await box.execute("storm_status", "{}")
    StormQueue(tmp_path).enqueue("cli", "hello?")
    assert "1 pending" in await box.execute("storm_status", "{}")

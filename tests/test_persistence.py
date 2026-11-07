from __future__ import annotations

import pytest

from core.persistence import ConversationStore


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


def test_create_session(store):
    sid = store.create_session("Test Chat")
    assert sid
    session = store.get_session(sid)
    assert session is not None
    assert session["title"] == "Test Chat"
    assert session["message_count"] == 0


def test_add_and_get_messages(store):
    sid = store.create_session()
    store.add_message(sid, "user", "Hello Jarvis")
    store.add_message(sid, "assistant", "Hello, sir!", provider="groq")
    msgs = store.get_messages(sid)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "Hello Jarvis"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["provider"] == "groq"


def test_auto_title_from_first_user_message(store):
    sid = store.create_session()
    store.add_message(
        sid,
        "user",
        "What is the meaning of life and everything else in the universe and beyond that we can comprehend",
    )
    session = store.get_session(sid)
    assert "What is the meaning" in session["title"]


def test_list_sessions_ordered(store):
    import time

    sid1 = store.create_session("First")
    store.add_message(sid1, "user", "hello")
    time.sleep(0.05)
    sid2 = store.create_session("Second")
    store.add_message(sid2, "user", "world")
    sessions = store.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["id"] == sid2  # most recent first


def test_delete_session(store):
    sid = store.create_session()
    store.add_message(sid, "user", "test")
    assert store.delete_session(sid) is True
    assert store.get_session(sid) is None
    assert store.get_messages(sid) == []


def test_search_full_text(store):
    sid = store.create_session("Python Chat")
    store.add_message(sid, "user", "Tell me about Python programming")
    store.add_message(sid, "assistant", "Python is a great language for data science")
    results = store.search("Python programming")
    assert len(results) >= 1
    assert any("Python" in r["content"] for r in results)


def test_search_no_results(store):
    sid = store.create_session()
    store.add_message(sid, "user", "hello world")
    results = store.search("quantum entanglement physics")
    assert results == []


def test_get_last_n_messages(store):
    sid = store.create_session()
    for i in range(10):
        store.add_message(sid, "user", f"message {i}")
    msgs = store.get_last_n_messages(sid, n=3)
    assert len(msgs) == 3
    assert msgs[0]["content"] == "message 7"
    assert msgs[2]["content"] == "message 9"


def test_latest_session_id(store):
    assert store.latest_session_id() is None
    sid = store.create_session("Latest")
    assert store.latest_session_id() == sid
    sid2 = store.create_session("Newer")
    store.add_message(sid2, "user", "hi")
    assert store.latest_session_id() == sid2


def test_archive_session(store):
    sid = store.create_session()
    store.archive_session(sid, archived=True)
    sessions = store.list_sessions()
    assert all(s["id"] != sid for s in sessions)
    store.archive_session(sid, archived=False)
    sessions = store.list_sessions()
    assert any(s["id"] == sid for s in sessions)


def test_total_counts(store):
    assert store.total_sessions() == 0
    assert store.total_messages() == 0
    sid = store.create_session()
    store.add_message(sid, "user", "hi")
    store.add_message(sid, "assistant", "hello")
    assert store.total_sessions() == 1
    assert store.total_messages() == 2

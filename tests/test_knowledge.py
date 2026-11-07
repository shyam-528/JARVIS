from __future__ import annotations

import pytest

from core.knowledge import DocumentIndexer, KnowledgeStore, chunk_text


@pytest.fixture
def store(tmp_path):
    return KnowledgeStore(tmp_path / "knowledge.db")


@pytest.fixture
def indexer(store):
    return DocumentIndexer(store)


def test_chunk_text_short():
    chunks = chunk_text("Hello world, this is a short text.")
    assert len(chunks) == 1
    assert chunks[0] == "Hello world, this is a short text."


def test_chunk_text_long_splits():
    text = " ".join([f"This is sentence number {i}." for i in range(200)])
    chunks = chunk_text(text, max_chars=500, overlap=0)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 600


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_store_add_and_search(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    chunks = ["Python is a programming language", "JavaScript runs in browsers", "The sky is blue"]
    store.add_chunks("test_source", chunks, "hash1")
    st = store.stats()
    assert st["total_chunks"] == 3
    results = store.search("programming language python", k=2)
    assert len(results) <= 2
    assert results[0]["source"] == "test_source"


def test_store_remove_source(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunks("src_a", ["chunk one", "chunk two"], "h1")
    store.add_chunks("src_b", ["chunk three"], "h2")
    assert store.stats()["total_chunks"] == 3
    removed = store.remove_source("src_a")
    assert removed == 2
    assert store.stats()["total_chunks"] == 1


def test_store_clear(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunks("src", ["a", "b"], "h")
    n = store.clear()
    assert n == 2
    assert store.stats()["total_chunks"] == 0


def test_store_already_indexed(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    assert not store.already_indexed("abc123")
    store.add_chunks("src", ["data"], "abc123")
    assert store.already_indexed("abc123")


def test_indexer_text_file(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    indexer = DocumentIndexer(store)
    doc = tmp_path / "notes.txt"
    doc.write_text("The capital of France is Paris.\nIt is known for the Eiffel Tower.")
    result = indexer.index_file(doc)
    assert result["indexed"] > 0
    assert not result.get("skipped")


def test_indexer_directory(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    indexer = DocumentIndexer(store)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("Content about machine learning algorithms.")
    (tmp_path / "sub" / "b.md").write_text("# Notes\nDeep learning uses neural networks.")
    (tmp_path / "sub" / "c.exe").write_text("binary")  # should be skipped
    result = indexer.index_directory(str(tmp_path / "sub"))
    assert result["files_found"] == 2
    assert result["files_indexed"] == 2
    assert result["total_chunks"] > 0


def test_indexer_skips_unchanged(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    indexer = DocumentIndexer(store)
    doc = tmp_path / "file.txt"
    doc.write_text("Some content here.")
    r1 = indexer.index_file(doc)
    assert r1["indexed"] > 0
    r2 = indexer.index_file(doc)
    assert r2.get("skipped") is True


def test_indexer_force_reindex(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    indexer = DocumentIndexer(store)
    doc = tmp_path / "file.txt"
    doc.write_text("Content version one.")
    indexer.index_file(doc)
    doc.write_text("Content version two with more info.")
    r = indexer.index_file(doc, force=True)
    assert r["indexed"] > 0
    assert store.stats()["total_chunks"] >= 1


def test_search_with_source_filter(tmp_path):
    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunks("file_a.txt", ["data about cats"], "h1")
    store.add_chunks("file_b.txt", ["data about dogs"], "h2")
    results = store.search("cats", k=5, source_filter="file_a")
    assert all("file_a" in r["source"] for r in results)

from __future__ import annotations

import asyncio

from core.config import Config
from core.rag_v2 import HybridRetriever, RAGv2, SearchResult


def make_cfg(tmp_path):
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return cfg


# ---------- BM25 incremental sync ----------


def test_incremental_sync_adds_only_new_chunks(tmp_path):
    cfg = make_cfg(tmp_path)
    retriever = HybridRetriever(cfg)

    # Add a doc directly to the vector store (simulating indexing)
    retriever.knowledge.add_chunks("doc1.txt", ["alpha beta gamma"], "h1")

    # BM25 knows nothing yet
    before = retriever.bm25.conn.execute("SELECT COUNT(*) FROM bm25_docs").fetchone()[0]
    assert before == 0

    added = retriever.incremental_sync()
    assert added == 1

    # Second sync adds nothing
    assert retriever.incremental_sync() == 0

    # And a new chunk is picked up
    retriever.knowledge.add_chunks("doc2.txt", ["delta epsilon"], "h2")
    assert retriever.incremental_sync() == 1


def test_incremental_sync_survives_restart(tmp_path):
    cfg = make_cfg(tmp_path)
    retriever = HybridRetriever(cfg)
    retriever.knowledge.add_chunks("doc.txt", ["hello world"], "h1")
    retriever.incremental_sync()

    # "Restart": fresh retriever over the same databases
    retriever2 = HybridRetriever(cfg)
    assert retriever2.incremental_sync() == 0  # boot sync already covered it
    hits = retriever2.bm25.search("hello", k=5)
    assert hits and hits[0][0]  # findable by keyword


# ---------- LLM answer generation ----------


def test_generate_answer_uses_attached_llm(tmp_path):
    cfg = make_cfg(tmp_path)
    rag = RAGv2(cfg)

    async def fake_llm(system: str, user: str) -> str:
        assert "ONLY" in system or "context" in system.lower()
        return "The answer is 42 [1]."

    rag.set_llm(fake_llm)
    chunks = [
        SearchResult(
            content="passage text",
            source="doc.txt",
            score=1.0,
            chunk_id="1",
            metadata={},
            citation="[1]",
        )
    ]

    out = asyncio.run(rag._generate_answer("what is the answer?", chunks))
    assert out == "The answer is 42 [1]."


def test_generate_answer_falls_back_without_llm(tmp_path):
    cfg = make_cfg(tmp_path)
    rag = RAGv2(cfg)  # no LLM attached
    chunks = [
        SearchResult(
            content="fallback passage",
            source="doc.txt",
            score=1.0,
            chunk_id="1",
            metadata={},
            citation="[1]",
        )
    ]
    out = asyncio.run(rag._generate_answer("q", chunks))
    assert "fallback passage" in out
    assert "[1]" in out


def test_generate_answer_falls_back_when_llm_errors(tmp_path):
    cfg = make_cfg(tmp_path)
    rag = RAGv2(cfg)

    async def broken_llm(system: str, user: str) -> str:
        raise RuntimeError("provider down")

    rag.set_llm(broken_llm)
    chunks = [
        SearchResult(
            content="resilient passage",
            source="doc.txt",
            score=1.0,
            chunk_id="1",
            metadata={},
            citation="[1]",
        )
    ]
    out = asyncio.run(rag._generate_answer("q", chunks))
    assert "resilient passage" in out  # degraded gracefully, no exception


# ---------- end-to-end retrieve_and_answer with fake LLM ----------


def test_retrieve_and_answer_cites_sources(tmp_path):
    cfg = make_cfg(tmp_path)
    rag = RAGv2(cfg)

    async def fake_llm(system: str, user: str) -> str:
        return "Groq is a fast inference platform [1]."

    rag.set_llm(fake_llm)
    rag.add_document(
        "Groq provides fast AI inference with LPU hardware.",
        "kb-groq.md",
        {},
    )

    resp = asyncio.run(rag.retrieve_and_answer("what is groq?"))
    assert resp.answer == "Groq is a fast inference platform [1]."
    assert resp.citations, "citations must be attached"
    assert any("kb-groq.md" in c.source for c in resp.citations)


# ---------- BM25 keyword leg ----------


def test_bm25_keyword_search_finds_added_doc(tmp_path):
    cfg = make_cfg(tmp_path)
    rag = RAGv2(cfg)
    rag.add_document("zebra crossing markings are white", "animals.md", {})
    rag.add_document("quantum tunneling crosses barriers", "physics.md", {})

    out = asyncio.run(
        rag.retriever.search("zebra", k=2, use_embeddings=False, use_kg=False, rerank=False)
    )
    assert out, "BM25 should find the zebra doc"
    assert "zebra" in out[0].content.lower()

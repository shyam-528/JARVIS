from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from core.config import Config
from core.knowledge import DocumentIndexer, KnowledgeStore

from .db import connect as _db_connect


@dataclass
class SearchResult:
    content: str
    source: str
    score: float
    chunk_id: str
    metadata: dict
    citation: str  # e.g., "[1]"


@dataclass
class RAGResponse:
    answer: str
    citations: list[SearchResult]
    reasoning_trace: list[str]


class BM25Index:
    """BM25 sparse retrieval for keyword matching."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = _db_connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self.doc_freq = defaultdict(int)
        self.doc_len = {}
        self.avgdl = 0.0
        self.k1 = 1.5
        self.b = 0.75

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bm25_docs (
                chunk_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                metadata TEXT,
                tokens TEXT
            )
        """)
        self.conn.commit()

    def add(self, chunk_id: str, content: str, source: str, metadata: dict):
        tokens = self._tokenize(content)
        self.conn.execute(
            "INSERT OR REPLACE INTO bm25_docs VALUES (?, ?, ?, ?, ?)",
            (chunk_id, content, source, json.dumps(metadata), json.dumps(tokens)),
        )
        self.conn.commit()

        # Update doc freq
        unique_tokens = set(tokens)
        for t in unique_tokens:
            self.doc_freq[t] += 1
        self.doc_len[chunk_id] = len(tokens)
        self._recalc_avgdl()

    def rebuild_from_rows(self, rows: list[tuple[str, str, str]]):
        """Rebuild the whole index from (chunk_id, content, source) rows.

        Restores the in-memory doc_freq/doc_len statistics that are lost
        on process restart even though rows persist in sqlite.
        """
        self.conn.execute("DELETE FROM bm25_docs")
        self.doc_freq = defaultdict(int)
        self.doc_len = {}
        for chunk_id, content, source in rows:
            self.add(chunk_id, content, source, {})
        self.conn.commit()

    def _recalc_avgdl(self):
        if self.doc_len:
            self.avgdl = sum(self.doc_len.values()) / len(self.doc_len)

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        q_tokens = self._tokenize(query)
        if not q_tokens:
            return []

        N = len(self.doc_len)
        if N == 0:
            return []

        scores = defaultdict(float)
        for t in q_tokens:
            df = self.doc_freq.get(t, 0)
            if df == 0:
                continue
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)

            # Score all docs containing this term
            for chunk_id, tokens in self._get_docs_with_token(t):
                tf = tokens.count(t)
                dl = self.doc_len.get(chunk_id, 1)
                score = (
                    idf
                    * tf
                    * (self.k1 + 1)
                    / (tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                )
                scores[chunk_id] += score

        return sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]

    def _get_docs_with_token(self, token: str) -> list[tuple[str, list[str]]]:
        # Simple: scan all (could optimize with inverted index)
        rows = self.conn.execute("SELECT chunk_id, tokens FROM bm25_docs").fetchall()
        results = []
        for row in rows:
            tokens = json.loads(row["tokens"])
            if token in tokens:
                results.append((row["chunk_id"], tokens))
        return results

    def close(self):
        self.conn.close()


class CrossEncoderReranker:
    """Cross-encoder reranking for relevance."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        self.model_name = model_name
        self._model = None

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self.model_name)
            except ImportError:
                self._model = False

    def rerank(
        self, query: str, candidates: list[SearchResult], top_k: int = 5
    ) -> list[SearchResult]:
        self._load_model()
        if not self._model:
            return candidates[:top_k]  # fallback

        pairs = [(query, c.content[:512]) for c in candidates]
        scores = self._model.predict(pairs)

        for c, s in zip(candidates, scores, strict=True):
            c.score = float(s)

        return sorted(candidates, key=lambda x: x.score, reverse=True)[:top_k]


class HybridRetriever:
    """Hybrid retrieval: BM25 + Embeddings + Knowledge Graph."""

    def __init__(self, config: Config):
        self.config = config
        self.knowledge = KnowledgeStore(config.db_path.parent / "knowledge.db")
        self.indexer = DocumentIndexer(self.knowledge)

        # BM25 index
        bm25_path = config.db_path.parent / "bm25.db"
        self.bm25 = BM25Index(bm25_path)

        # Reranker
        self.reranker = CrossEncoderReranker()

        # Sync BM25 with knowledge store (restores in-memory stats each boot)
        self._sync_bm25()

    def _sync_bm25(self):
        """Sync BM25 index with knowledge store chunks."""
        try:
            rows = self.knowledge.conn.execute("SELECT id, text, source FROM chunks").fetchall()
            self.bm25.rebuild_from_rows([(str(r[0]), r[1], r[2]) for r in rows])
        except Exception:
            pass  # empty/missing store -> empty index

    def incremental_sync(self) -> int:
        """Add knowledge-store chunks not yet in BM25. Returns count added.

        Cheap after indexing: only new chunk ids are inserted, so BM25 stays
        in lockstep with the vector store without a full rebuild.
        """
        try:
            existing = {
                r[0] for r in self.bm25.conn.execute("SELECT chunk_id FROM bm25_docs").fetchall()
            }
            rows = self.knowledge.conn.execute("SELECT id, text, source FROM chunks").fetchall()
            added = 0
            for row_id, text, source in rows:
                cid = str(row_id)
                if cid not in existing:
                    self.bm25.add(cid, text, source, {})
                    added += 1
            return added
        except Exception:
            return 0

    async def search(
        self,
        query: str,
        k: int = 10,
        use_bm25: bool = True,
        use_embeddings: bool = True,
        use_kg: bool = True,
        rerank: bool = True,
    ) -> list[SearchResult]:
        all_results: dict[str, SearchResult] = {}

        # BM25 search
        if use_bm25:
            bm25_results = self.bm25.search(query, k=k * 2)
            for chunk_id, score in bm25_results:
                doc = self._get_doc(chunk_id)
                if doc:
                    all_results[chunk_id] = SearchResult(
                        content=doc["content"],
                        source=doc["source"],
                        score=score * 0.5,  # weight
                        chunk_id=chunk_id,
                        metadata=doc.get("metadata", {}),
                        citation=f"[{len(all_results) + 1}]",
                    )

        # Embedding search (via KnowledgeStore vector search)
        if use_embeddings:
            try:
                emb_results = self.knowledge.search(query, k=k * 2)
                for d in emb_results:
                    chunk_id = str(d["id"])
                    score = float(d.get("score", 0.0))
                    if chunk_id in all_results:
                        all_results[chunk_id].score += score * 0.5
                    else:
                        all_results[chunk_id] = SearchResult(
                            content=d["text"],
                            source=d.get("source", ""),
                            score=score * 0.5,
                            chunk_id=chunk_id,
                            metadata={},
                            citation=f"[{len(all_results) + 1}]",
                        )
            except Exception:
                pass  # embedding store unavailable -> BM25 only

        # Knowledge Graph search (entities/relations)
        if use_kg:
            try:
                kg_results = await self._search_kg(query, k)
                for chunk_id, score, content in kg_results:
                    if chunk_id in all_results:
                        all_results[chunk_id].score += score * 0.3
                    else:
                        all_results[chunk_id] = SearchResult(
                            content=content,
                            source="knowledge_graph",
                            score=score * 0.3,
                            chunk_id=chunk_id,
                            metadata={},
                            citation=f"[{len(all_results) + 1}]",
                        )
            except Exception:
                pass  # KG leg optional

        # Sort and rerank
        results = sorted(all_results.values(), key=lambda x: x.score, reverse=True)

        if rerank and len(results) > 1:
            results = self.reranker.rerank(query, results, top_k=k)
        else:
            results = results[:k]

        # Renumber citations
        for i, r in enumerate(results):
            r.citation = f"[{i + 1}]"

        return results

    def _get_doc(self, chunk_id: str) -> dict | None:
        row = self.bm25.conn.execute(
            "SELECT content, source, metadata FROM bm25_docs WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if row:
            return {
                "content": row["content"],
                "source": row["source"],
                "metadata": json.loads(row["metadata"]),
            }
        return None

    async def _search_kg(self, query: str, k: int) -> list[tuple[str, float, str]]:
        """Entity-expanded search: re-query the store for capitalized entities."""
        entities = self._extract_entities(query)
        results = []
        for entity in entities[:3]:
            for d in self.knowledge.search(entity, k=2):
                results.append((str(d["id"]), float(d.get("score", 0.0)), d["text"]))
        return results[:k]

    def _extract_entities(self, text: str) -> list[str]:
        # Simple entity extraction (could use NER)
        words = re.findall(r"\b[A-Z][a-z]+\b", text)
        return list(set(words))


class RAGv2:
    """RAG v2 with hybrid retrieval, reranking, citations, and multi-hop."""

    def __init__(self, config: Config):
        self.config = config
        self.retriever = HybridRetriever(config)
        self.knowledge = self.retriever.knowledge  # share one store
        # LLM callable set by the orchestrator after router init:
        #   async def llm(system_prompt: str, user_prompt: str) -> str
        # When absent, answers fall back to an extractive digest.
        self.llm: callable | None = None

    def set_llm(self, fn) -> None:
        """Attach an async (system, user) -> str LLM call for answer generation."""
        self.llm = fn

    async def retrieve_and_answer(
        self,
        query: str,
        max_chunks: int = 5,
        multi_hop: bool = True,
    ) -> RAGResponse:
        # Initial retrieval
        chunks = await self.retriever.search(query, k=max_chunks, rerank=True)

        if not chunks:
            return RAGResponse("No relevant information found.", [], [])

        # Multi-hop: check if answer needs more info
        if multi_hop:
            chunks = await self._multi_hop(query, chunks, max_chunks)

        # Generate answer with citations
        answer = await self._generate_answer(query, chunks)

        return RAGResponse(
            answer=answer,
            citations=chunks,
            reasoning_trace=[f"Retrieved {len(chunks)} chunks via hybrid search"],
        )

    async def retrieve_context(
        self,
        query: str,
        max_chunks: int = 4,
    ) -> list[SearchResult]:
        """Retrieval-only: top passages WITHOUT an LLM answer call.

        Chat turns use this — the main model gets the raw passages as
        context, so the intermediate answer-generation LLM call (and its
        latency + token cost) is skipped entirely.
        """
        chunks = await self.retriever.search(query, k=max_chunks, rerank=True)
        return chunks

    async def _multi_hop(
        self,
        query: str,
        initial_chunks: list[SearchResult],
        max_chunks: int,
    ) -> list[SearchResult]:
        # Extract entities from initial results
        entities = set()
        for c in initial_chunks:
            entities.update(self.retriever._extract_entities(c.content))

        # Search for each entity
        for entity in list(entities)[:3]:
            if len(initial_chunks) >= max_chunks:
                break
            more = await self.retriever.search(entity, k=2)
            for c in more:
                if c.chunk_id not in [x.chunk_id for x in initial_chunks]:
                    initial_chunks.append(c)

        return initial_chunks[:max_chunks]

    async def _generate_answer(self, query: str, chunks: list[SearchResult]) -> str:
        """Answer the query from retrieved chunks, with [n] citations.

        Uses the attached LLM when available; falls back to an extractive
        digest so the pipeline never hard-fails.
        """
        context = "\n\n".join(
            f"{c.citation} (source: {c.source}):\n{c.content[:800]}" for c in chunks
        )

        if self.llm is not None:
            system = (
                "You are a retrieval-augmented answering engine. Answer the user's "
                "question using ONLY the provided context passages. Cite every "
                "claim with the passage number in square brackets, e.g. [1]. "
                "If the context does not contain the answer, say exactly: "
                '"I don\'t have that in my knowledge base." Never invent facts.'
            )
            user = f"Context passages:\n\n{context}\n\nQuestion: {query}"
            try:
                out = await self.llm(system, user)
                if out and out.strip():
                    return out.strip()
            except Exception:
                pass  # LLM unavailable -> extractive fallback below

        # Extractive fallback: passages with citations
        parts = [f"{c.citation} {c.source}: {c.content[:500]}" for c in chunks]
        return f"Most relevant knowledge for '{query}':\n\n" + "\n\n".join(parts)

    def add_document(self, content: str, source: str, metadata: dict) -> str:
        """Add document to both knowledge store and BM25."""
        file_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        self.knowledge.add_chunks(source, [content], file_hash)
        # Fetch the row id just inserted (highest id for this source)
        row = self.knowledge.conn.execute(
            "SELECT id FROM chunks WHERE source = ? ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        chunk_id = str(row[0]) if row else f"{source}-{int(time.time())}"
        self.retriever.bm25.add(chunk_id, content, source, metadata)
        return chunk_id

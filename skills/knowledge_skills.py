"""Knowledge-base tools: index paths, semantic search, QA over indexed docs,
stats, and clear. Registered only when store + indexer are available.
"""

from __future__ import annotations

import contextlib

from core.knowledge import DocumentIndexer, KnowledgeStore
from core.tools import Tool, ToolBox


def register_knowledge(box: ToolBox, store: KnowledgeStore, indexer: DocumentIndexer, rag=None):
    """Register knowledge tools. `rag` (core.rag_v2.RAGv2) enables BM25
    auto-sync after indexing and a cited knowledge_qa tool when available."""

    def _sync_bm25() -> int:
        if rag is None:
            return 0
        try:
            return rag.retriever.incremental_sync()
        except Exception:
            return 0

    async def index_path(path: str, recursive: bool = True, force: bool = False) -> str:
        """Index a file or directory into the knowledge base."""
        p = __import__("pathlib").Path(path)
        if p.is_dir():
            result = indexer.index_directory(str(p), recursive=recursive, force=force)
            if "error" in result:
                return f"Error: {result['error']}"
            n = _sync_bm25()
            return (
                f"Indexed {result['files_indexed']} files ({result['files_skipped']} skipped) "
                f"from {result['directory']} -> {result['total_chunks']} chunks"
                + (f" (+{n} BM25)" if n else "")
            )
        else:
            result = indexer.index_file(p, force=force)
            if result.get("skipped"):
                return f"Skipped: {result.get('reason', 'unknown')}"
            n = _sync_bm25()
            return f"Indexed {result['indexed']} chunks from {result['source']}" + (
                f" (+{n} BM25)" if n else ""
            )

    async def search_knowledge(query: str, k: int = 5, source: str = "") -> str:
        """Semantic search across all indexed documents."""
        results = store.search(query, k=k, source_filter=source)
        if not results:
            return "No results found. Try indexing more files first with index_path."
        lines = []
        for r in results:
            snippet = r["text"][:300].replace("\n", " ")
            lines.append(
                f"[{r['score']:.3f}] {r['source']} (chunk {r['chunk_index']}):\n  {snippet}..."
            )
        return f"Found {len(results)} results:\n\n" + "\n\n".join(lines)

    async def knowledge_qa(question: str) -> str:
        """Answer a question FROM the indexed knowledge base with citations.

        Hybrid retrieval (BM25 + embeddings + rerank) then a cited LLM answer.
        Use this when the user asks about indexed documents/projects/files.
        """
        if rag is None:
            return "Knowledge QA unavailable (RAG not initialized)."
        resp = await rag.retrieve_and_answer(question)
        if not resp.citations:
            return "Nothing relevant in the knowledge base. Index documents first with index_path."
        sources = "\n".join(f"{c.citation} {c.source}" for c in resp.citations)
        return f"{resp.answer}\n\nSources:\n{sources}"

    async def knowledge_stats() -> str:
        """Show knowledge base statistics."""
        st = store.stats()
        sources = store.list_sources()
        lines = [f"Total chunks: {st['total_chunks']}", f"Total sources: {st['total_sources']}"]
        if sources:
            lines.append("\nIndexed sources:")
            for s in sources[:20]:
                lines.append(f"  - {s['source']} ({s['chunks']} chunks)")
        return "\n".join(lines)

    async def clear_knowledge() -> str:
        """Clear the entire knowledge base."""
        n = store.clear()
        # Keep BM25 in lockstep
        if rag is not None:
            with contextlib.suppress(Exception):
                rag.retriever._sync_bm25()
        return f"Cleared {n} chunks from knowledge base."

    box.register(
        Tool(
            name="index_path",
            description="Index a file or directory into the knowledge base for semantic search. Supports text, code, markdown, PDF.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory path to index"},
                    "recursive": {"type": "boolean", "default": True},
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Re-index even if unchanged",
                    },
                },
                "required": ["path"],
            },
            handler=index_path,
        )
    )
    box.register(
        Tool(
            name="search_knowledge",
            description="Semantic search across all indexed documents. Returns relevant chunks with scores.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "k": {"type": "integer", "default": 5, "description": "Number of results"},
                    "source": {
                        "type": "string",
                        "default": "",
                        "description": "Filter by source path substring",
                    },
                },
                "required": ["query"],
            },
            handler=search_knowledge,
        )
    )
    if rag is not None:
        box.register(
            Tool(
                name="knowledge_qa",
                description="Answer a question FROM indexed knowledge with cited sources (hybrid BM25+vector retrieval). Use when the user asks about indexed documents, projects, or files.",
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question to answer from the knowledge base",
                        },
                    },
                    "required": ["question"],
                },
                handler=knowledge_qa,
            )
        )
    box.register(
        Tool(
            name="knowledge_stats",
            description="Show knowledge base statistics (total chunks, sources)",
            parameters={"type": "object", "properties": {}},
            handler=knowledge_stats,
        )
    )
    box.register(
        Tool(
            name="clear_knowledge",
            description="Clear the entire knowledge base (delete all indexed chunks)",
            parameters={"type": "object", "properties": {}},
            handler=clear_knowledge,
        )
    )

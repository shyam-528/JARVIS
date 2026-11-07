"""Long-term fact store (memory v1) — curated, prompt-injected.

Boundary with core/memory_v2.py
-------------------------------
MemoryStore is the *curated* store (``data/memory.db``): facts and notes the
user or tools deliberately saved (``/remember``, the ``/api/memory``
endpoints, the memory tools in ``skills/memory.py``), plus discrete notable
events such as background-agent results (``kind="agent_result"``). Rows never
decay or get auto-deleted — removal is explicit (``forget()``, or the
``/consolidate`` hygiene in ``core/consolidation.py``). Retrieval is hybrid
FTS5 + vector (bge-small embeddings), and the orchestrator injects the top-k
hits into EVERY prompt (``Orchestrator._memory_context``), so this is the
store that shapes answers. Rule of thumb: if a piece of information should
influence future replies, it belongs here. ``memory_v2`` records every turn
automatically and serves as the history/audit layer — do not double-store
the same content in both.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path

from .db import connect as _db_connect


def _sanitize_fts(query: str) -> str:
    terms = [t.strip() for t in query.replace('"', " ").split() if t.strip()]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


class _Embedder:
    def __init__(self) -> None:
        import numpy as np
        from fastembed import TextEmbedding

        self.np = np
        self.model = TextEmbedding("BAAI/bge-small-en-v1.5")

    def embed(self, texts: list[str]) -> list:
        return [self.np.asarray(v, dtype="float32") for v in self.model.embed(texts)]


class MemoryStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(db_path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts TEXT NOT NULL,"
            "kind TEXT NOT NULL DEFAULT 'fact',"
            "text TEXT NOT NULL UNIQUE,"
            "emb BLOB)"
        )
        with contextlib.suppress(sqlite3.OperationalError):
            self.conn.execute("ALTER TABLE memories ADD COLUMN emb BLOB")
        self.conn.commit()
        self.fts = self._init_fts()
        self._embedder: _Embedder | None = None
        self._embedder_failed = False
        # Vector cache: ids/rows/matrix rebuilt lazily, invalidated on writes
        self._vec_cache_ids: list[int] | None = None
        self._vec_cache_rows: dict[int, tuple[str, str]] | None = None
        self._vec_cache_matrix = None
        self._vec_norms = None
        self._vec_cache_dim: int = 0

    def _init_fts(self) -> bool:
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id')"
            )
            self.conn.execute(
                "CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN "
                "INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text); END"
            )
            self.conn.execute(
                "CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN "
                "INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text); END"
            )
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def _get_embedder(self) -> _Embedder | None:
        if self._embedder is not None or self._embedder_failed:
            return self._embedder
        if os.getenv("JARVIS_NO_EMBEDDINGS"):
            self._embedder_failed = True
            return None
        try:
            self._embedder = _Embedder()
        except Exception:
            self._embedder = None
            self._embedder_failed = True
        return self._embedder

    def add(self, text: str, kind: str = "fact") -> str:
        text = " ".join(text.split())
        if not text:
            return "error: empty memory"
        row = self.conn.execute("SELECT id FROM memories WHERE text = ?", (text,)).fetchone()
        if row:
            return "already known"
        emb = None
        embedder = self._get_embedder()
        if embedder is not None:
            try:
                emb = embedder.embed([text])[0].tobytes()
            except Exception:
                emb = None
        self.conn.execute(
            "INSERT INTO memories (ts, kind, text, emb) VALUES (?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M"), kind, text, emb),
        )
        self.conn.commit()
        self._invalidate_vec_cache()
        return "stored"

    def search(self, query: str, k: int = 4) -> list[tuple[int, str, str, float]]:
        scores: dict[int, float] = {}
        rows: dict[int, tuple[str, str]] = {}

        if self.fts:
            match = _sanitize_fts(query)
            if match:
                try:
                    for mid, ts, text, rank in self.conn.execute(
                        "SELECT m.id, m.ts, m.text, bm25(memories_fts) FROM memories_fts "
                        "JOIN memories m ON m.id = memories_fts.rowid "
                        "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                        (match, k * 3),
                    ):
                        rows[mid] = (ts, text)
                        scores[mid] = scores.get(mid, 0.0) + 1.0 / (1.0 + max(0.0, rank))
                except sqlite3.OperationalError:
                    pass

        embedder = self._get_embedder()
        if embedder is not None:
            try:
                mat, ids, rowmap = self._vector_matrix(embedder)
                if mat is not None and len(ids):
                    qvec = embedder.embed([query])[0]
                    # Batched cosine similarity (one matmul, no Python row loop)
                    qn = float((qvec * qvec).sum()) ** 0.5
                    norms = self._vec_norms
                    if norms is not None and norms.shape[0] == mat.shape[0]:
                        denom = qn * norms
                    else:
                        denom = qn * self._np_sqrt((mat * mat).sum(axis=1))
                    np = embedder.np
                    denom = np.where(denom > 0, denom, 1.0)
                    sims = np.nan_to_num((mat @ qvec) / denom, nan=0.0, posinf=0.0, neginf=0.0)
                    for mid, cos in zip(ids, sims, strict=True):
                        if mid not in rows:
                            rows[mid] = rowmap[mid]
                        scores[mid] = scores.get(mid, 0.0) + max(0.0, float(cos)) * 2.0
            except Exception:
                pass

        if not scores:
            like = f"%{query.strip()[:60]}%"
            for mid, ts, text in self.conn.execute(
                "SELECT id, ts, text FROM memories WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                (like, k),
            ):
                rows[mid] = (ts, text)
                scores[mid] = 0.5

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [(mid, rows[mid][0], rows[mid][1], s) for mid, s in ranked if mid in rows]

    def conn_binary_to_vec(self, embedder: _Embedder, blob: bytes):
        return embedder.np.frombuffer(blob, dtype="float32")

    def _invalidate_vec_cache(self):
        self._vec_cache_ids = None
        self._vec_cache_rows = None
        self._vec_cache_matrix = None
        self._vec_norms = None

    @staticmethod
    def _np_sqrt(x):
        import numpy as np

        return np.sqrt(x)

    def _vector_matrix(self, embedder: _Embedder):
        """Build (or reuse) a cached [N, D] embedding matrix for all memories."""
        if self._vec_cache_matrix is not None and self._vec_cache_ids is not None:
            return self._vec_cache_matrix, self._vec_cache_ids, self._vec_cache_rows
        vec_rows = list(
            self.conn.execute("SELECT id, ts, text, emb FROM memories WHERE emb IS NOT NULL")
        )
        if not vec_rows:
            return None, [], {}
        np = embedder.np
        ids: list[int] = []
        rowmap: dict[int, tuple[str, str]] = {}
        vec_list = []
        dim = 0
        for mid, ts, text, blob in vec_rows:
            v = np.frombuffer(blob, dtype="float32")
            if dim == 0:
                dim = v.shape[0]
            if v.shape[0] != dim:
                continue  # skip rows with mismatched dims (model change)
            ids.append(mid)
            rowmap[mid] = (ts, text)
            vec_list.append(v)
        if not ids:
            return None, [], {}
        matrix = np.vstack(vec_list).astype("float32")
        self._vec_cache_ids = ids
        self._vec_cache_rows = rowmap
        self._vec_cache_matrix = matrix
        self._vec_norms = np.sqrt((matrix * matrix).sum(axis=1))
        self._vec_cache_dim = dim
        return matrix, ids, rowmap

    def forget(self, query: str) -> int:
        like = f"%{query.strip()[:60]}%"
        ids = [
            r[0] for r in self.conn.execute("SELECT id FROM memories WHERE text LIKE ?", (like,))
        ]
        for mid in ids:
            self.conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
        self.conn.commit()
        if ids:
            self._invalidate_vec_cache()
        return len(ids)

    def all(self, limit: int = 50) -> list[tuple[int, str, str]]:
        return list(
            self.conn.execute(
                "SELECT id, ts, text FROM memories ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

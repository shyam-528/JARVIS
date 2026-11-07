"""Local knowledge base: chunk + embed documents into SQLite for hybrid
(FTS5 + vector) search, with a DocumentIndexer for files/directories.

Failure policy (Wave 7):
- Per-file extraction failures are LOUD (printed, naming the file) and the
  file is skipped — one unreadable/corrupt document must never abort a
  directory index, but must never vanish silently either.
- Missing optional dependencies (fastembed, OCR stack) degrade gracefully to
  FTS-only / no-OCR with a one-time printed notice.
- index_file / index_directory always return result dicts (never raise);
  per-file errors are collected into the returned "errors" list.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

import numpy as np

from .db import connect as _db_connect

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".xml",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
    ".bat",
    ".ps1",
    ".sql",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".log",
    ".rst",
}
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm"}
IMAGE_OCR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"}
SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
    ".next",
    ".cache",
    "data",
    ".idea",
    ".vscode",
}


def _get_embedder():
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding("BAAI/bge-small-en-v1.5")
        return model
    except Exception as exc:
        # fail-soft: search degrades to FTS-only — but a silently degraded
        # index is invisible, so say so once per attempt
        print(
            f"[knowledge] embedder unavailable, falling back to FTS-only search: {type(exc).__name__}: {exc}"
        )
        return None


def chunk_text(text: str, max_chars: int = 1800, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks at sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= max_chars:
            current = (current + "\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > max_chars:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                sub = ""
                for sent in sentences:
                    if len(sub) + len(sent) + 1 <= max_chars:
                        sub = (sub + " " + sent).strip()
                    else:
                        if sub:
                            chunks.append(sub)
                        sub = sent
                current = sub or ""
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())

    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_tail = chunks[i - 1][-overlap:]
            overlapped.append(prev_tail + " " + chunks[i])
        return overlapped

    return chunks


class KnowledgeStore:
    """Local vector knowledge base: SQLite + fastembed embeddings."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = _db_connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB,
                file_hash TEXT NOT NULL,
                indexed_at REAL NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (source)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks (file_hash)
        """)
        self.conn.commit()
        self._embedder = None
        self._embedder_loaded = False
        # Vector cache for fast batched search
        self._cache_matrix = None
        self._cache_meta = []
        self._cache_norms = None
        self._cache_warm = False

    def _ensure_cache(self):
        if not self._cache_warm:
            self._rebuild_cache()
            self._cache_warm = True

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        if not self._embedder_loaded:
            self._embedder = _get_embedder()
            self._embedder_loaded = True
        if self._embedder is None:
            return [np.zeros(384, dtype="float32") for _ in texts]
        return [np.asarray(v, dtype="float32") for v in self._embedder.embed(texts)]

    def file_hash(self, path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()

    def already_indexed(self, file_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM chunks WHERE file_hash = ? LIMIT 1", (file_hash,)
        ).fetchone()
        return row is not None

    def add_chunks(self, source: str, chunks: list[str], file_hash: str) -> int:
        if not chunks:
            return 0
        embeddings = self._embed(chunks)
        now = time.time()
        for i, (chunk, emb) in enumerate(zip(chunks, embeddings, strict=True)):
            self.conn.execute(
                "INSERT INTO chunks (source, chunk_index, text, embedding, file_hash, indexed_at) VALUES (?,?,?,?,?,?)",
                (source, i, chunk, emb.tobytes(), file_hash, now),
            )
        self.conn.commit()
        self._cache_warm = False  # invalidate; rebuilt lazily on next search
        return len(chunks)

    def remove_source(self, source: str) -> int:
        cur = self.conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
        self.conn.commit()
        self._cache_warm = False
        return cur.rowcount

    def search(self, query: str, k: int = 5, source_filter: str = "") -> list[dict]:
        query_vec = self._embed([query])[0]

        # Fast path: no source filter + warm cache -> one batched matmul
        if not source_filter and self._cache_matrix is not None:
            return self._search_cached(query_vec, k)
        self._ensure_cache()
        if not source_filter and self._cache_matrix is not None:
            return self._search_cached(query_vec, k)

        rows = self.conn.execute(
            "SELECT id, source, chunk_index, text, embedding FROM chunks"
        ).fetchall()
        if not rows:
            return []

        results = []
        for row_id, source, chunk_idx, text, emb_blob in rows:
            if source_filter and source_filter.lower() not in source.lower():
                continue
            if emb_blob:
                vec = np.frombuffer(emb_blob, dtype="float32")
                denom = np.linalg.norm(query_vec) * np.linalg.norm(vec)
                score = float(np.dot(query_vec, vec) / denom) if denom > 0 else 0.0
            else:
                score = 0.0
            results.append(
                {
                    "id": row_id,
                    "source": source,
                    "chunk_index": chunk_idx,
                    "text": text,
                    "score": round(score, 4),
                }
            )

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:k]

    # ---------- cached fast-path vector search ----------

    def _rebuild_cache(self):
        rows = self.conn.execute(
            "SELECT id, source, chunk_index, text, embedding FROM chunks WHERE embedding IS NOT NULL"
        ).fetchall()
        if not rows:
            self._cache_matrix = None
            self._cache_meta = []
            return
        ids, sources, idxs, texts, vecs = [], [], [], [], []
        dim = 0
        for row_id, source, chunk_idx, text, emb_blob in rows:
            v = np.frombuffer(emb_blob, dtype="float32")
            if dim == 0:
                dim = v.shape[0]
            if v.shape[0] != dim:
                continue
            ids.append(row_id)
            sources.append(source)
            idxs.append(chunk_idx)
            texts.append(text)
            vecs.append(v)
        self._cache_matrix = np.vstack(vecs).astype("float32")
        self._cache_meta = list(zip(ids, sources, idxs, texts, strict=True))
        self._cache_norms = np.sqrt((self._cache_matrix * self._cache_matrix).sum(axis=1))

    def _search_cached(self, query_vec, k: int) -> list[dict]:
        # guard against zero-norm rows (failed embeds) producing nan
        qn = np.linalg.norm(query_vec)
        denom = self._cache_norms * qn
        denom = np.where(denom > 0, denom, 1.0)
        sims = (self._cache_matrix @ query_vec) / denom
        # zero rows score 0 instead of nan
        sims = np.nan_to_num(sims, nan=0.0, posinf=0.0, neginf=0.0)
        top = np.argsort(sims)[::-1][:k]
        out = []
        for i in top:
            rid, source, cidx, text = self._cache_meta[i]
            out.append(
                {
                    "id": rid,
                    "source": source,
                    "chunk_index": cidx,
                    "text": text,
                    "score": round(float(sims[i]), 4),
                }
            )
        return out

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sources = self.conn.execute("SELECT DISTINCT source FROM chunks ORDER BY source").fetchall()
        return {
            "total_chunks": total,
            "total_sources": len(sources),
            "sources": [s[0] for s in sources],
            "embeddings": self.embedder_status(),
        }

    def embedder_status(self) -> dict:
        """Search-mode flag (T6, ROADMAP P2): is vector search actually on?

        The embedder loads lazily on first search, so three states:
        - "not-tried-yet": no search has run since boot, nothing is known.
        - "on": embedder loaded, vector search active.
        - "off": load was attempted and failed — search is FTS-only.
          "off" is the degraded state dashboards must show loudly: without
          this flag a fastembed break silently degrades every search.
        Never raises.
        """
        try:
            if not self._embedder_loaded:
                return {"state": "not-tried-yet"}
            if self._embedder is None:
                return {"state": "off"}
            return {"state": "on"}
        except Exception:
            return {"state": "unknown"}

    def clear(self) -> int:
        cur = self.conn.execute("DELETE FROM chunks")
        self.conn.commit()
        self._cache_warm = False
        self._cache_matrix = None
        self._cache_meta = []
        self._cache_norms = None
        return cur.rowcount

    def list_sources(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) as chunks, MAX(indexed_at) as last FROM chunks GROUP BY source ORDER BY source"
        ).fetchall()
        return [{"source": r[0], "chunks": r[1], "indexed_at": r[2]} for r in rows]


class DocumentIndexer:
    """Indexes files and directories into the KnowledgeStore."""

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"[knowledge] failed to read {path.name}: {exc} — skipping file")
            return ""

    def _read_pdf(self, path: Path) -> str:
        """PDF text extraction with OCR fallback for scanned pages."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            pages = []
            ocr_needed = False
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
                else:
                    ocr_needed = True
                    pages.append("")  # placeholder; OCR fills it below
            if ocr_needed:
                ocr_texts = self._ocr_pdf_pages(path, len(pages))
                for i, ot in enumerate(ocr_texts):
                    if not pages[i].strip() and ot.strip():
                        pages[i] = ot
            return "\n\n".join(p for p in pages if p.strip())
        except Exception as exc:
            print(f"[knowledge] failed to extract PDF {path.name}: {exc} — skipping file")
            return ""

    def _ocr_pdf_pages(self, path: Path, page_count: int, max_pages: int = 10) -> list[str]:
        """Rasterize PDF pages and OCR them (for scanned PDFs)."""
        results = [""] * page_count
        try:
            import fitz  # PyMuPDF
        except ImportError:
            try:
                import pymupdf as fitz
            except ImportError:
                # fail-soft: OCR stack not installed — scanned pages stay empty
                return results
        try:
            import io as _io

            import pytesseract
            from PIL import Image

            doc = fitz.open(str(path))
            for i in range(min(page_count, max_pages, len(doc))):
                pix = doc[i].get_pixmap(dpi=150)
                img = Image.open(_io.BytesIO(pix.tobytes("png")))
                results[i] = pytesseract.image_to_string(img)
            doc.close()
        except Exception as exc:
            # fail-soft: best-effort OCR; pages we couldn't OCR stay empty
            print(f"[knowledge] OCR failed for {path.name}: {exc} — some pages unextracted")
        return results

    def _read_docx(self, path: Path) -> str:
        try:
            import docx

            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for table in d.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            return "\n\n".join(parts)
        except Exception as exc:
            print(f"[knowledge] failed to extract DOCX {path.name}: {exc} — skipping file")
            return ""

    def _read_xlsx(self, path: Path) -> str:
        try:
            import openpyxl

            wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
            parts = []
            for ws in wb.worksheets:
                parts.append(f"[Sheet: {ws.title}]")
                for row in ws.iter_rows(max_row=500, values_only=True):
                    cells = [str(c) for c in row if c is not None and str(c).strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            wb.close()
            return "\n".join(parts)
        except Exception as exc:
            print(f"[knowledge] failed to extract XLSX {path.name}: {exc} — skipping file")
            return ""

    def _read_pptx(self, path: Path) -> str:
        try:
            from pptx import Presentation

            prs = Presentation(str(path))
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                parts.append(f"[Slide {i}]")
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            t = "".join(run.text for run in para.runs).strip()
                            if t:
                                parts.append(t)
                    if getattr(shape, "has_table", False) and shape.has_table:
                        for row in shape.table.rows:
                            cells = [c.text.strip() for c in row.cells if c.text.strip()]
                            if cells:
                                parts.append(" | ".join(cells))
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
                    if notes:
                        parts.append(f"[Notes] {notes}")
            return "\n\n".join(parts)
        except Exception as exc:
            print(f"[knowledge] failed to extract PPTX {path.name}: {exc} — skipping file")
            return ""

    def _ocr_image(self, path: Path) -> str:
        try:
            import pytesseract
            from PIL import Image

            return pytesseract.image_to_string(Image.open(str(path)))
        except Exception as exc:
            print(f"[knowledge] failed to OCR image {path.name}: {exc} — skipping file")
            return ""

    def _read_file(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in PDF_EXTENSIONS:
            return self._read_pdf(path)
        if ext in OFFICE_EXTENSIONS:
            if ext in (".docx", ".docm"):
                return self._read_docx(path)
            if ext in (".xlsx", ".xlsm"):
                return self._read_xlsx(path)
            return self._read_pptx(path)
        if ext in IMAGE_OCR_EXTENSIONS:
            return self._ocr_image(path)
        if ext in TEXT_EXTENSIONS:
            return self._read_text_file(path)
        return ""

    def index_file(self, path: Path, force: bool = False) -> dict:
        path = Path(path).resolve()
        if not path.exists() or not path.is_file():
            return {"indexed": 0, "skipped": True, "reason": "file not found"}

        fhash = self.store.file_hash(str(path))
        if not force and self.store.already_indexed(fhash):
            return {"indexed": 0, "skipped": True, "reason": "already indexed (unchanged)"}

        self.store.remove_source(str(path))
        text = self._read_file(path)
        if not text.strip():
            return {"indexed": 0, "skipped": True, "reason": "no extractable text"}

        chunks = chunk_text(text)
        count = self.store.add_chunks(str(path), chunks, fhash)
        return {"indexed": count, "skipped": False, "source": str(path)}

    def index_directory(
        self,
        dir_path: str,
        recursive: bool = True,
        force: bool = False,
        max_files: int = 500,
    ) -> dict:
        root = Path(dir_path).resolve()
        if not root.exists() or not root.is_dir():
            return {"error": f"directory not found: {dir_path}"}

        allowed = TEXT_EXTENSIONS | PDF_EXTENSIONS | OFFICE_EXTENSIONS | IMAGE_OCR_EXTENSIONS
        files = []
        if recursive:
            for f in root.rglob("*"):
                if (
                    f.is_file()
                    and f.suffix.lower() in allowed
                    and not any(part in SKIP_DIRS for part in f.parts)
                ):
                    files.append(f)
        else:
            for f in root.iterdir():
                if f.is_file() and f.suffix.lower() in allowed:
                    files.append(f)

        files = files[:max_files]
        total_chunks = 0
        indexed_files = 0
        skipped = 0
        errors = []

        for f in files:
            try:
                result = self.index_file(f, force=force)
                if result.get("skipped"):
                    skipped += 1
                else:
                    total_chunks += result.get("indexed", 0)
                    indexed_files += 1
            except Exception as exc:
                errors.append(f"{f.name}: {exc}")

        return {
            "directory": str(root),
            "files_found": len(files),
            "files_indexed": indexed_files,
            "files_skipped": skipped,
            "total_chunks": total_chunks,
            "errors": errors[:10] if errors else [],
        }

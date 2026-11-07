"""Wave 7 failure-path tests for core.knowledge.

Contract: per-file extraction failures are LOUD (printed, naming the file)
and the file is skipped — never silent, never aborting a directory index.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core import knowledge
from core.knowledge import DocumentIndexer, KnowledgeStore


@pytest.fixture
def store(tmp_path):
    s = KnowledgeStore(tmp_path / "kb.db")
    yield s
    s.conn.close()


def test_embedder_failure_is_loud_and_fts_only(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)  # import fails
    assert knowledge._get_embedder() is None
    out = capsys.readouterr().out
    assert "embedder unavailable" in out and "FTS-only" in out


def test_unreadable_text_file_is_loud_and_skipped(store, tmp_path, capsys, monkeypatch):
    f = tmp_path / "doc.txt"
    f.write_text("hello world", encoding="utf-8")
    idx = DocumentIndexer(store)

    real_read = Path.read_text

    def boom(self, *a, **k):
        if self.name == "doc.txt":
            raise OSError("permission denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    result = idx.index_file(f)
    assert result["skipped"] is True
    assert result["reason"] == "no extractable text"
    out = capsys.readouterr().out
    assert "doc.txt" in out and "permission denied" in out


def test_corrupt_pdf_is_loud_and_skipped(store, tmp_path, capsys):
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.4 garbage not a real pdf")
    idx = DocumentIndexer(store)
    result = idx.index_file(f)
    assert result["skipped"] is True
    out = capsys.readouterr().out
    assert "broken.pdf" in out


def test_directory_index_survives_bad_files(store, tmp_path, capsys):
    (tmp_path / "good.md").write_text("# Title\n\nSome useful content here.", encoding="utf-8")
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf")
    idx = DocumentIndexer(store)
    result = idx.index_directory(str(tmp_path), recursive=False)
    assert result["files_indexed"] == 1
    assert result["files_skipped"] >= 1
    assert result["total_chunks"] >= 1


def test_index_missing_file_returns_reason(store, tmp_path):
    idx = DocumentIndexer(store)
    result = idx.index_file(tmp_path / "ghost.txt")
    assert result == {"indexed": 0, "skipped": True, "reason": "file not found"}

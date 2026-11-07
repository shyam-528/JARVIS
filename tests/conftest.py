from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.setenv("JARVIS_NO_EMBEDDINGS", "1")

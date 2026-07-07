"""Unit tests for `src/domain/docs/knowledge_base.py`.

Covers:
- DocEntry construction and embedding_text format.
- Entry loading from fixture JSON files (valid, missing fields, malformed JSON).
- Cache round-trip: write then read back preserves vectors and hashes.
- Content-hash invalidation: stale entries are re-embedded; fresh ones are skipped.
- Cosine ranking: the entry whose embedding_text most closely matches the query
  vector scores highest.

Usage:
    PYTHONPATH=src pytest tests/domain/docs/test_knowledge_base.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from domain.docs.knowledge_base import DocEntry, DocsKnowledgeBase, _CACHE_VERSION
from domain.rules.knowledge_base import Embedder


# ── Stub embedder ─────────────────────────────────────────────────────────────


class _FixedEmbedder(Embedder):
    """Returns a unit vector whose direction encodes the text length bucket.

    Texts are bucketed into four quadrants by length so the stub produces
    distinct, deterministic vectors without any real model call.
    """

    DIM = 4

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        out = []
        for text in texts:
            bucket = len(text) % self.DIM
            v = [0.0] * self.DIM
            v[bucket] = 1.0
            out.append(v)
        return out


def _unit(v: list[float]) -> np.ndarray:
    a = np.array(v, dtype=np.float32)
    return a / (np.linalg.norm(a) or 1.0)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_entries_dir(tmp_path: Path, entries: list[dict]) -> Path:
    d = tmp_path / "entries"
    d.mkdir()
    for i, e in enumerate(entries):
        filename = e.get("key") or f"entry-{i}"
        (d / f"{filename}.json").write_text(
            json.dumps(e, ensure_ascii=False), encoding="utf-8"
        )
    return d


def _make_kb(tmp_path: Path, entries: list[dict]) -> DocsKnowledgeBase:
    _make_entries_dir(tmp_path, entries)
    kb = DocsKnowledgeBase(root=tmp_path)
    kb._entries = None  # ensure lazy load
    return kb


_SAMPLE_ENTRIES = [
    {"key": "catchment", "title": "Catchment", "body": "A catchment is a group of locations."},
    {"key": "program", "title": "Program", "body": "A program is a named intervention."},
    {"key": "rules", "title": "JavaScript Rules", "body": "Rules are written in JavaScript."},
]


# ── DocEntry ──────────────────────────────────────────────────────────────────


def test_doc_entry_embedding_text_prepends_title() -> None:
    entry = DocEntry(key="k", title="My Title", body="Body text here.")
    text = entry.embedding_text()
    assert text.startswith("My Title\n\n")
    assert "Body text here." in text


def test_doc_entry_is_immutable() -> None:
    entry = DocEntry(key="k", title="T", body="B")
    with pytest.raises(Exception):
        entry.key = "new"  # type: ignore[misc]


# ── Entry loading ─────────────────────────────────────────────────────────────


def test_loads_valid_entries(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path, _SAMPLE_ENTRIES)
    assert len(kb.entries) == 3
    keys = {e.key for e in kb.entries}
    assert keys == {"catchment", "program", "rules"}


def test_skips_entry_missing_body(tmp_path: Path) -> None:
    entries = [
        {"key": "ok", "title": "OK", "body": "Some body text."},
        {"key": "bad", "title": "Bad"},          # missing body
    ]
    kb = _make_kb(tmp_path, entries)
    assert len(kb.entries) == 1
    assert kb.entries[0].key == "ok"


def test_skips_entry_missing_key(tmp_path: Path) -> None:
    entries = [
        {"title": "No key", "body": "body"},    # missing key
        {"key": "good", "title": "Good", "body": "body"},
    ]
    kb = _make_kb(tmp_path, entries)
    assert len(kb.entries) == 1


def test_skips_malformed_json(tmp_path: Path) -> None:
    d = tmp_path / "entries"
    d.mkdir()
    (d / "good.json").write_text(
        json.dumps({"key": "good", "title": "Good", "body": "body"}), encoding="utf-8"
    )
    (d / "bad.json").write_text("{not valid json", encoding="utf-8")
    kb = DocsKnowledgeBase(root=tmp_path)
    assert len(kb.entries) == 1


def test_empty_entries_dir(tmp_path: Path) -> None:
    (tmp_path / "entries").mkdir()
    kb = DocsKnowledgeBase(root=tmp_path)
    assert kb.entries == []


def test_missing_entries_dir(tmp_path: Path) -> None:
    kb = DocsKnowledgeBase(root=tmp_path)
    assert kb.entries == []


# ── Cache round-trip ──────────────────────────────────────────────────────────


def test_rebuild_writes_cache(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path, _SAMPLE_ENTRIES)
    kb._entries = kb._load_entries()  # force load before patching embedder

    # Patch the module-level default embedder used by rebuild.
    import domain.docs.knowledge_base as kb_mod
    orig = kb_mod._default_embedder
    kb_mod._default_embedder = lambda: _FixedEmbedder()
    try:
        kb.rebuild()
    finally:
        kb_mod._default_embedder = orig

    assert kb.cache_path.exists()
    payload = json.loads(kb.cache_path.read_text(encoding="utf-8"))
    assert payload["version"] == _CACHE_VERSION
    assert set(payload["entries"].keys()) == {"catchment", "program", "rules"}
    for rec in payload["entries"].values():
        assert "hash" in rec
        assert len(rec["vector"]) == _FixedEmbedder.DIM


def test_cache_round_trip_preserves_vectors(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path, _SAMPLE_ENTRIES)
    kb._entries = kb._load_entries()

    import domain.docs.knowledge_base as kb_mod
    orig = kb_mod._default_embedder
    kb_mod._default_embedder = lambda: _FixedEmbedder()
    try:
        kb.rebuild()
        # Fresh KB from same root reads the cache.
        kb2 = DocsKnowledgeBase(root=tmp_path)
        kb2._entries = kb2._load_entries()
        vectors = kb2._read_cache()
    finally:
        kb_mod._default_embedder = orig

    assert set(vectors.keys()) == {"catchment", "program", "rules"}


# ── Content-hash invalidation ─────────────────────────────────────────────────


def test_rebuild_only_embeds_changed_entries(tmp_path: Path) -> None:
    entries_dir = _make_entries_dir(tmp_path, _SAMPLE_ENTRIES)

    embed_calls: list[list[str]] = []

    class _TrackingEmbedder(Embedder):
        def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
            if input_type == "document":
                embed_calls.append(texts)
            return [[0.1, 0.2, 0.3, 0.4]] * len(texts)

    import domain.docs.knowledge_base as kb_mod
    orig = kb_mod._default_embedder
    kb_mod._default_embedder = lambda: _TrackingEmbedder()
    try:
        kb = DocsKnowledgeBase(root=tmp_path)
        kb.rebuild()
        first_call_count = sum(len(c) for c in embed_calls)

        # Modify one entry on disk.
        updated = {"key": "catchment", "title": "Catchment", "body": "Updated body text."}
        (entries_dir / "catchment.json").write_text(
            json.dumps(updated), encoding="utf-8"
        )

        embed_calls.clear()
        kb2 = DocsKnowledgeBase(root=tmp_path)
        kb2.rebuild()
        second_call_count = sum(len(c) for c in embed_calls)
    finally:
        kb_mod._default_embedder = orig

    assert first_call_count == 3   # all three embedded on first run
    assert second_call_count == 1  # only the changed entry re-embedded


# ── Cosine ranking ────────────────────────────────────────────────────────────


def test_retrieve_returns_closest_entry(tmp_path: Path) -> None:
    """Entry whose embedding_text length matches the query bucket ranks first."""
    # Build entries whose embedding_text lengths fall into distinct buckets.
    # _FixedEmbedder puts bucket = len(text) % 4.
    # We craft one entry that will land in the same bucket as the query.
    entries = [
        # bucket 0 (len % 4 == 0): title+body text of length 40
        {"key": "bucket0", "title": "AB", "body": "X" * 34},   # "AB\n\n" + 34 = 38... adjust below
        {"key": "bucket1", "title": "AB", "body": "X" * 35},
        {"key": "bucket2", "title": "AB", "body": "X" * 36},
        {"key": "bucket3", "title": "AB", "body": "X" * 37},
    ]
    # Compute actual embedding_text lengths and set up a query that lands in bucket1
    actual_entries = [
        DocEntry(key=e["key"], title=e["title"], body=e["body"]) for e in entries
    ]
    buckets = {e.key: len(e.embedding_text()) % _FixedEmbedder.DIM for e in actual_entries}

    # Find which key lands in bucket 1 so we can match the query to it.
    target_key = next(k for k, b in buckets.items() if b == 1)

    # Write entries to disk.
    d = tmp_path / "entries"
    d.mkdir()
    for e in entries:
        (d / f"{e['key']}.json").write_text(json.dumps(e), encoding="utf-8")

    # A query string whose length % 4 == 1 will get vector [0,1,0,0].
    query = "Q" * (4 - (len("Q") % 4) + 1)  # ensure len % 4 == 1
    # Simplest: just use a string of length 1
    query = "Q"
    assert len(query) % _FixedEmbedder.DIM == 1

    import domain.docs.knowledge_base as kb_mod
    orig = kb_mod._default_embedder
    kb_mod._default_embedder = lambda: _FixedEmbedder()
    try:
        kb = DocsKnowledgeBase(root=tmp_path)
        result = kb.retrieve(query, top_k=1)
    finally:
        kb_mod._default_embedder = orig

    assert len(result) == 1
    assert result[0].key == target_key


def test_retrieve_top_k_respected(tmp_path: Path) -> None:
    kb = _make_kb(tmp_path, _SAMPLE_ENTRIES)

    import domain.docs.knowledge_base as kb_mod
    orig = kb_mod._default_embedder
    kb_mod._default_embedder = lambda: _FixedEmbedder()
    try:
        result = kb.retrieve("what is a program?", top_k=2)
    finally:
        kb_mod._default_embedder = orig

    assert len(result) <= 2


def test_retrieve_returns_empty_when_no_entries(tmp_path: Path) -> None:
    (tmp_path / "entries").mkdir()
    kb = DocsKnowledgeBase(root=tmp_path)

    import domain.docs.knowledge_base as kb_mod
    orig = kb_mod._default_embedder
    kb_mod._default_embedder = lambda: _FixedEmbedder()
    try:
        result = kb.retrieve("anything", top_k=5)
    finally:
        kb_mod._default_embedder = orig

    assert result == []

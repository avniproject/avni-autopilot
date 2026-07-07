"""Documentation entry catalog with Voyage embeddings + cosine retrieval.

The knowledge base owns two things:

  1. The on-disk catalog under `resources/docs/entries/`:
       *.json   — one entry per topic (key, title, body)

  2. The embedding cache at `resources/docs/.embeddings.json`. Entry vectors
     are computed once by `avni-docs-kb rebuild`; query vectors are computed
     live at retrieve() time.

See specs/AVNI_DOCS_KB_SDD.md.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from domain.rules.knowledge_base import (
    VoyageEmbedder,
    _content_hash,
    _default_embedder,
    _top_k,
)

log = logging.getLogger(__name__)

_DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "resources" / "docs"
_CACHE_VERSION = 1
_TOP_K_ENTRIES = 5


class DocEntry(BaseModel):
    """One topic entry in the documentation knowledge base."""

    model_config = ConfigDict(frozen=True)

    key: str    # filename stem, e.g. "javascript-rules-visit-schedule"
    title: str  # short label surfaced in answer sources
    body: str   # full section text

    def embedding_text(self) -> str:
        """Text the embedder sees — title prepended for topic context."""
        return f"{self.title}\n\n{self.body}"


class DocsKnowledgeBase:
    """Loader + retriever for the documentation entry catalog.

    Construct once per process; the catalog is loaded lazily on first access
    and cached in memory. retrieve() is safe to call repeatedly.

    Usage:
        kb = DocsKnowledgeBase()
        entries = kb.retrieve("what is a catchment?")
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _DEFAULT_ROOT
        self.entries_dir = self.root / "entries"
        self.cache_path = self.root / ".embeddings.json"
        self._entries: list[DocEntry] | None = None
        self._catalog_vectors: dict[str, np.ndarray] | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = _TOP_K_ENTRIES) -> list[DocEntry]:
        """Embed the query and return top-K entries by cosine similarity."""
        vectors = self._ensure_catalog_vectors()
        emb = _default_embedder()
        query_vecs = emb.embed([query], input_type="query")
        query_vec = np.asarray(query_vecs[0], dtype=np.float32)
        return _top_k(query_vec, self.entries, vectors, top_k)

    def rebuild(self) -> None:
        """Re-embed changed entries and write the embedding cache."""
        entries = self.entries
        if not entries:
            log.warning(f"No entries found under {self.entries_dir}")
            return

        cache = self._read_cache()
        missing = [
            e for e in entries
            if cache.get(e.key, {}).get("hash") != _content_hash(e.embedding_text())
        ]

        if missing:
            log.info(f"Docs KB rebuild: {len(missing)} of {len(entries)} entries to embed")
            emb = _default_embedder()
            new_vectors = emb.embed([e.embedding_text() for e in missing], input_type="document")
            for entry, vec in zip(missing, new_vectors):
                cache[entry.key] = {
                    "hash": _content_hash(entry.embedding_text()),
                    "vector": vec,
                }
            self._write_cache(cache)
            log.info("Docs KB rebuild complete")
        else:
            log.info("Docs KB cache up to date — nothing to embed")

        self._catalog_vectors = None  # invalidate in-memory cache

    # ── Catalog loading ───────────────────────────────────────────────────────

    @property
    def entries(self) -> list[DocEntry]:
        if self._entries is None:
            self._entries = self._load_entries()
        return self._entries

    def _load_entries(self) -> list[DocEntry]:
        out: list[DocEntry] = []
        if not self.entries_dir.exists():
            log.warning(f"Docs entries dir not found: {self.entries_dir}")
            return out
        for path in sorted(self.entries_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning(f"Skipped docs entry {path.name}: {exc}")
                continue
            key = data.get("key", "").strip()
            title = data.get("title", "").strip()
            body = data.get("body", "").strip()
            if not key or not title or not body:
                log.warning(f"Skipped docs entry {path.name}: missing key/title/body")
                continue
            out.append(DocEntry(key=key, title=title, body=body))
        log.info(f"Docs KB loaded {len(out)} entries from {self.entries_dir}")
        return out

    # ── Embedding cache ───────────────────────────────────────────────────────

    def _ensure_catalog_vectors(self) -> dict[str, np.ndarray]:
        if self._catalog_vectors is not None:
            return self._catalog_vectors

        cache = self._read_cache()
        entries = self.entries
        missing = [
            e for e in entries
            if cache.get(e.key, {}).get("hash") != _content_hash(e.embedding_text())
        ]
        if missing:
            log.info(f"Docs KB auto-rebuild: {len(missing)} stale entries")
            emb = _default_embedder()
            new_vectors = emb.embed([e.embedding_text() for e in missing], input_type="document")
            for entry, vec in zip(missing, new_vectors):
                cache[entry.key] = {
                    "hash": _content_hash(entry.embedding_text()),
                    "vector": vec,
                }
            self._write_cache(cache)

        self._catalog_vectors = {
            key: np.asarray(rec["vector"], dtype=np.float32)
            for key, rec in cache.items()
        }
        return self._catalog_vectors

    def _read_cache(self) -> dict[str, dict]:
        if not self.cache_path.exists():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"Docs KB cache unreadable, ignoring: {exc}")
            return {}
        if payload.get("version") != _CACHE_VERSION:
            log.info(f"Docs KB cache version mismatch, discarding: {payload.get('version')!r}")
            return {}
        return payload.get("entries") or {}

    def _write_cache(self, entries: dict[str, dict]) -> None:
        serializable = {
            key: {
                "hash": rec["hash"],
                "vector": [float(x) for x in rec["vector"]],
            }
            for key, rec in entries.items()
        }
        payload = {"version": _CACHE_VERSION, "entries": serializable}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cache_path)

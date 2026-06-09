"""Helper + example catalog with Voyage embeddings + cosine retrieval.

The knowledge base owns three things:

  1. The on-disk catalog under `resources/rules/`:
       helpers/entities/*.json   — per-avni-models-class helper entries
       helpers/imports/*.json    — `imports.*` namespace helper entries
       examples/<rule_kind>/*.md — curated few-shot examples

  2. The embedding cache at `resources/rules/.embeddings.json`. Catalog vectors
     are computed once by `kb_cli rebuild`; query vectors are computed live at
     retrieve() time.

  3. The retrieve() entry point — embeds the query, scores it against cached
     catalog vectors with brute-force cosine similarity in numpy, and returns
     top-K helpers + top-K examples.

If the cache is missing or any entry's content hash has drifted, retrieve()
auto-rebuilds before scoring. The cost is gated by actual rule work (no rule
intents → no embedding API calls). See specs/VISIT_SCHEDULE_RULE_SDD.md §6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from domain.rules.rule_spec import RuleKind, RuleSpec

log = logging.getLogger(__name__)


# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "resources" / "rules"
_EMBEDDING_MODEL = "voyage-3"
_TOP_K_HELPERS = 5
_TOP_K_EXAMPLES = 3
_CACHE_VERSION = 1

# Free-tier-safe defaults for Voyage AI. With a payment method on file the
# standard limits are ~2000 RPM / 3M TPM; override via env vars if you've
# raised them.
#
#   VOYAGE_BATCH_TOKEN_BUDGET — max tokens per embed request (default 8000).
#   VOYAGE_MIN_REQUEST_INTERVAL_SEC — minimum seconds between requests
#                                     (default 21 — three RPM with margin).
#   VOYAGE_MAX_RETRIES — retries on RateLimitError (default 5).
_DEFAULT_BATCH_TOKEN_BUDGET = int(os.environ.get("VOYAGE_BATCH_TOKEN_BUDGET", "8000"))
_DEFAULT_MIN_REQUEST_INTERVAL = float(os.environ.get("VOYAGE_MIN_REQUEST_INTERVAL_SEC", "21"))
_DEFAULT_MAX_RETRIES = int(os.environ.get("VOYAGE_MAX_RETRIES", "5"))


# ── Catalog entry shapes ──────────────────────────────────────────────────────


class HelperEntry(BaseModel):
    """One row of the helper catalog (per-entity or per-imports-namespace)."""

    model_config = ConfigDict(frozen=True)

    key: str                # stable id used in the embedding cache
    name: str               # e.g. "Individual.findLatestObservationFromPreviousEncounters"
    signature: str
    applies_to: tuple[str, ...]
    use_when: str
    example_snippet: str
    source_file: str        # relative path, for debugging

    def embedding_text(self) -> str:
        """The text the embedder sees when vectorising this entry."""
        return "\n".join(
            part for part in (self.name, self.signature, self.use_when, self.example_snippet)
            if part
        )


class ExampleEntry(BaseModel):
    """One few-shot example pulled from the curated rule corpus."""

    model_config = ConfigDict(frozen=True)

    key: str
    rule_kind: str
    intent: str
    entity_param: str
    encounter_types: tuple[str, ...]
    concepts: tuple[str, ...]
    source_org: str
    js: str
    source_file: str

    def embedding_text(self) -> str:
        return self.intent


@dataclass
class RetrievedContext:
    """Output of `KnowledgeBase.retrieve` — top-K helpers + examples."""

    helpers: list[HelperEntry] = field(default_factory=list)
    examples: list[ExampleEntry] = field(default_factory=list)


# ── Knowledge base ────────────────────────────────────────────────────────────


class KnowledgeBase:
    """Loader + retriever for the helper + example catalog.

    Construct once per process; the catalog is loaded lazily on first access
    and cached in memory. `retrieve()` is safe to call repeatedly.

    Usage:
        kb = KnowledgeBase()
        ctx = kb.retrieve(rule_spec)
        helpers_text = kb.render_helpers(ctx.helpers)
        examples_text = kb.render_examples(ctx.examples)
    """

    def __init__(self, root: Path | str | None = None,
                 embedder: "Embedder | None" = None) -> None:
        self.root = Path(root) if root else _DEFAULT_ROOT
        self.helpers_dir = self.root / "helpers"
        self.examples_dir = self.root / "examples"
        self.cache_path = self.root / ".embeddings.json"
        self._embedder = embedder
        self._helpers: list[HelperEntry] | None = None
        self._examples: list[ExampleEntry] | None = None
        self._catalog_vectors: dict[str, np.ndarray] | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def helpers(self) -> list[HelperEntry]:
        if self._helpers is None:
            self._helpers = self._load_helpers()
        return self._helpers

    @property
    def examples(self) -> list[ExampleEntry]:
        if self._examples is None:
            self._examples = self._load_examples()
        return self._examples

    def retrieve(
        self,
        spec: RuleSpec,
        top_k_helpers: int = _TOP_K_HELPERS,
        top_k_examples: int = _TOP_K_EXAMPLES,
    ) -> RetrievedContext:
        """Embed the query and return top-K helpers + examples for this rule kind."""
        helpers_scope = [h for h in self.helpers if spec.rule_kind.value in h.applies_to]
        examples_scope = [e for e in self.examples if e.rule_kind == spec.rule_kind.value]

        if not helpers_scope and not examples_scope:
            log.warning(f"KB empty for rule_kind={spec.rule_kind.value!r}")
            return RetrievedContext()

        vectors = self._ensure_catalog_vectors()
        query_vec = self._embed_query(_query_text(spec))

        helpers_ranked = _top_k(query_vec, helpers_scope, vectors, top_k_helpers)
        examples_ranked = _top_k(query_vec, examples_scope, vectors, top_k_examples)
        return RetrievedContext(helpers=helpers_ranked, examples=examples_ranked)

    def render_helpers(self, helpers: list[HelperEntry]) -> str:
        """Format helper entries for injection into the user prompt."""
        if not helpers:
            return "(none retrieved)"
        chunks = []
        for h in helpers:
            block = [f"- {h.name}", f"  signature: {h.signature}"]
            if h.use_when:
                block.append(f"  use when: {h.use_when}")
            if h.example_snippet:
                block.append(f"  snippet: {h.example_snippet}")
            chunks.append("\n".join(block))
        return "\n".join(chunks)

    def render_examples(self, examples: list[ExampleEntry]) -> str:
        """Format example entries for injection into the user prompt."""
        if not examples:
            return "(none retrieved)"
        chunks = []
        for e in examples:
            chunks.append(
                f"### Example: {e.source_org} — {e.intent}\n"
                f"entity_param: {e.entity_param}\n"
                f"```js\n{e.js}\n```"
            )
        return "\n\n".join(chunks)

    # ── Catalog loading ───────────────────────────────────────────────────────

    def _load_helpers(self) -> list[HelperEntry]:
        out: list[HelperEntry] = []
        if not self.helpers_dir.exists():
            return out
        for path in sorted(self.helpers_dir.rglob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning(f"Skipped helper file {path}: {exc}")
                continue
            entries = data if isinstance(data, list) else data.get("entries", [])
            rel = str(path.relative_to(self.root))
            for entry in entries:
                name = entry.get("name", "").strip()
                if not name:
                    continue
                out.append(HelperEntry(
                    key=f"helper:{rel}:{name}",
                    name=name,
                    signature=entry.get("signature", ""),
                    applies_to=tuple(entry.get("applies_to") or []),
                    use_when=entry.get("use_when", ""),
                    example_snippet=entry.get("example_snippet", ""),
                    source_file=rel,
                ))
        return out

    def _load_examples(self) -> list[ExampleEntry]:
        out: list[ExampleEntry] = []
        if not self.examples_dir.exists():
            return out
        for path in sorted(self.examples_dir.rglob("*.md")):
            try:
                meta, body = _parse_example(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                log.warning(f"Skipped example {path}: {exc}")
                continue
            rule_kind = meta.get("rule_kind", "").strip()
            intent = meta.get("intent", "").strip()
            if not rule_kind or not intent or not body:
                log.warning(f"Skipped example {path}: missing rule_kind / intent / body")
                continue
            rel = str(path.relative_to(self.root))
            out.append(ExampleEntry(
                key=f"example:{rel}",
                rule_kind=rule_kind,
                intent=intent,
                entity_param=meta.get("entity_param", "").strip(),
                encounter_types=tuple(_coerce_list(meta.get("encounter_types"))),
                concepts=tuple(_coerce_list(meta.get("concepts"))),
                source_org=meta.get("source_org", "").strip(),
                js=body,
                source_file=rel,
            ))
        return out

    # ── Embedding cache ───────────────────────────────────────────────────────

    def _ensure_catalog_vectors(self) -> dict[str, np.ndarray]:
        """Return catalog vectors keyed by entry key, rebuilding the cache if stale."""
        if self._catalog_vectors is not None:
            return self._catalog_vectors

        cache = self._read_cache()
        all_entries: list[HelperEntry | ExampleEntry] = [
            *self.helpers, *self.examples,
        ]
        missing = [e for e in all_entries
                   if cache.get(e.key, {}).get("hash") != _content_hash(e.embedding_text())]

        if missing:
            log.info(f"KB cache rebuild: {len(missing)} of {len(all_entries)} entries")
            new_vectors = self._embedder_for_documents()(
                [e.embedding_text() for e in missing]
            )
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
            log.warning(f"KB cache unreadable, ignoring: {exc}")
            return {}
        if payload.get("version") != _CACHE_VERSION:
            log.info(f"KB cache version mismatch, discarding: {payload.get('version')!r}")
            return {}
        return payload.get("entries") or {}

    def _write_cache(self, entries: dict[str, dict]) -> None:
        # Coerce vectors to plain Python floats — embedder backends sometimes
        # return numpy types that `json.dumps` won't serialize directly.
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

    # ── Embedder plumbing ─────────────────────────────────────────────────────

    def _embedder_for_documents(self):
        emb = self._embedder or _default_embedder()
        return lambda texts: emb.embed(texts, input_type="document")

    def _embed_query(self, text: str) -> np.ndarray:
        emb = self._embedder or _default_embedder()
        vecs = emb.embed([text], input_type="query")
        return np.asarray(vecs[0], dtype=np.float32)


# ── Embedder protocol + default impl ──────────────────────────────────────────


class Embedder:
    """Interface for vectorising a batch of texts.

    The default implementation calls Voyage; tests can pass a stub.
    """

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        raise NotImplementedError


class VoyageEmbedder(Embedder):
    """Voyage AI embedding client with token-aware batching + RPM throttling.

    Reads `VOYAGE_API_KEY` lazily on first call so importing this module
    does not require credentials. Defaults are sized for the free tier
    (3 RPM / 10K TPM); override with `VOYAGE_BATCH_TOKEN_BUDGET`,
    `VOYAGE_MIN_REQUEST_INTERVAL_SEC`, and `VOYAGE_MAX_RETRIES` once you
    add a payment method (standard rates are ~2000 RPM / 3M TPM).
    """

    def __init__(
        self,
        model: str = _EMBEDDING_MODEL,
        batch_token_budget: int = _DEFAULT_BATCH_TOKEN_BUDGET,
        min_request_interval: float = _DEFAULT_MIN_REQUEST_INTERVAL,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self.model = model
        self.batch_token_budget = batch_token_budget
        self.min_request_interval = min_request_interval
        self.max_retries = max_retries
        self._client = None
        self._last_request_at: float = 0.0

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_client()

        batches = self._chunk_by_tokens(texts)
        if len(batches) > 1:
            log.info(
                f"Voyage embed: {len(texts)} texts split into {len(batches)} batch(es) "
                f"to stay under {self.batch_token_budget}-token budget"
            )

        vectors: list[list[float]] = []
        for idx, batch in enumerate(batches):
            vectors.extend(self._embed_one_batch(batch, input_type, batch_index=idx))
        return vectors

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        import voyageai
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise RuntimeError(
                "VOYAGE_API_KEY is not set. Embedding cannot proceed without it."
            )
        self._client = voyageai.Client(api_key=key)

    def _embed_one_batch(
        self, batch: list[str], input_type: str, batch_index: int,
    ) -> list[list[float]]:
        """Embed a single batch with RPM throttling + retry on rate-limit errors."""
        import voyageai

        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            self._wait_for_rpm_slot()
            try:
                result = self._client.embed(
                    batch, model=self.model, input_type=input_type,
                )
                self._last_request_at = time.monotonic()
                return result.embeddings
            except voyageai.error.RateLimitError as exc:
                wait = max(self.min_request_interval, backoff)
                log.warning(
                    f"Voyage rate-limit on batch {batch_index} "
                    f"(attempt {attempt}/{self.max_retries}): sleeping {wait:.0f}s. "
                    f"Detail: {exc}"
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
            except Exception:
                # Non-retryable — surface to the caller. KnowledgeBase.retrieve
                # catches generic exceptions and turns them into warnings.
                raise

        raise RuntimeError(
            f"Voyage embed failed after {self.max_retries} rate-limit retries. "
            f"Add a payment method at https://dashboard.voyageai.com/ to lift "
            f"the free-tier 3 RPM / 10K TPM ceiling."
        )

    def _wait_for_rpm_slot(self) -> None:
        """Sleep just enough to satisfy `min_request_interval` between requests."""
        if self._last_request_at == 0.0:
            return
        elapsed = time.monotonic() - self._last_request_at
        gap = self.min_request_interval - elapsed
        if gap > 0:
            time.sleep(gap)

    def _chunk_by_tokens(self, texts: list[str]) -> list[list[str]]:
        """Split `texts` into batches that fit within `batch_token_budget`.

        Token count is estimated by character length / 3.5 — Voyage uses a
        tokenizer close enough to OpenAI's cl100k that this is a safe ceiling
        for English + JS-flavoured payloads. A single text exceeding the
        budget is sent on its own; the server will reject it if it exceeds
        the per-request hard limit.
        """
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for text in texts:
            tokens = _estimate_tokens(text)
            if current and current_tokens + tokens > self.batch_token_budget:
                batches.append(current)
                current = [text]
                current_tokens = tokens
            else:
                current.append(text)
                current_tokens += tokens
        if current:
            batches.append(current)
        return batches


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate — used only for batch packing decisions."""
    if not text:
        return 1
    # ~3.5 chars per token is roughly cl100k_base for prose + code mix.
    return max(1, len(text) // 3 + 1)


def _default_embedder() -> Embedder:
    return VoyageEmbedder()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _query_text(spec: RuleSpec) -> str:
    """The string passed to the query embedder.

    Combines the intent with a short context line so similarity scoring sees
    both the user's ask and the form's structural shape.
    """
    bits = [f"formType={spec.form_type}"]
    if spec.subject_type:
        bits.append(f"subject={spec.subject_type}")
    if spec.program:
        bits.append(f"program={spec.program}")
    if spec.encounter_type:
        bits.append(f"encounterType={spec.encounter_type}")
    context = " ".join(bits)
    return f"{spec.intent}\n[{context}]"


def _top_k(
    query: np.ndarray,
    entries: list,
    vectors: dict[str, np.ndarray],
    k: int,
) -> list:
    """Cosine-similarity top-K. Entries missing from the cache are skipped."""
    if not entries:
        return []
    scored: list[tuple[float, object]] = []
    q_norm = float(np.linalg.norm(query)) or 1.0
    for entry in entries:
        vec = vectors.get(entry.key)
        if vec is None:
            continue
        v_norm = float(np.linalg.norm(vec)) or 1.0
        score = float(np.dot(query, vec) / (q_norm * v_norm))
        scored.append((score, entry))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [entry for _score, entry in scored[:k]]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coerce_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if x is not None]
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:js|javascript)?\n(.*?)\n```", re.DOTALL)


def _parse_example(text: str) -> tuple[dict, str]:
    """Split a Markdown example file into its frontmatter dict and JS body."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("missing frontmatter")
    meta_block, rest = match.group(1), match.group(2)

    meta: dict = {}
    for line in meta_block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = _parse_yaml_scalar(val.strip())

    fence = _FENCE_RE.search(rest)
    body = fence.group(1).strip() if fence else rest.strip()
    return meta, body


def _parse_yaml_scalar(raw: str):
    """Tiny YAML-ish scalar parser for the example frontmatter.

    Handles quoted strings and inline JSON arrays. Anything more exotic is
    returned verbatim — the frontmatter we write is deliberately narrow.
    """
    if not raw:
        return ""
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return json.loads(raw.replace("'", '"'))
        except json.JSONDecodeError:
            return raw
    return raw

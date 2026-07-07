# Avni Documentation Knowledge Base — Software Design Document

## 1. Objective

`resources/faqs/merged_kb.md` is an 11K-line implementer-focused knowledge base
covering JavaScript rules, form configuration, concept management, organisation
setup, troubleshooting patterns, and Q&A derived from 90K+ support tickets.
Today the chat agent has no knowledge of it.

This SDD adds:

1. A **documentation knowledge base** backed by hand-maintained JSON entry files
   under `resources/docs/entries/` — one file per topic, mirroring the helper
   catalog pattern used by the rules KB.
2. A one-time **conversion script** (`scripts/convert_docs_kb.py`) that splits
   `resources/faqs/merged_kb.md` into the initial set of JSON entry files.
3. A `DocsKnowledgeBase` class that loads the JSON entries, embeds them via
   Voyage AI, and retrieves top-K entries by cosine similarity.
4. An `answer_avni_question(question)` chat tool that retrieves relevant entries
   and answers product/implementation questions grounded in the knowledge base.
5. An `avni-docs-kb rebuild` CLI command that re-embeds changed entries and
   writes the embedding cache — matching the shape of `avni-rules-kb rebuild`.

---

## 2. Scope

### In scope

- JSON entry files under `resources/docs/entries/` as the **source of truth**.
  Each file holds one topic with `key`, `title`, and `body` fields. Files are
  hand-maintained after the initial conversion.
- A one-time conversion script `scripts/convert_docs_kb.py` that reads
  `resources/faqs/merged_kb.md`, splits it at H2 headings (H3 content merged
  inline into the parent H2), and writes one JSON file per section. Run once;
  output committed; `merged_kb.md` is thereafter reference-only.
- A `DocsKnowledgeBase` class in `src/domain/docs/knowledge_base.py` that loads
  entries from `resources/docs/entries/`, embeds queries via the existing
  `VoyageEmbedder`, and returns top-K entries by cosine similarity.
- An `answer_avni_question(question)` tool added to `src/chat/tools.py` and
  declared in the system prompt.
- An `avni-docs-kb` CLI (`src/domain/docs/kb_cli.py`) with a single `rebuild`
  sub-command: reads all `*.json` under `resources/docs/entries/`, re-embeds
  entries whose content hash has changed, writes `resources/docs/.embeddings.json`.

### Out of scope

- Fetching fresh docs from `avni.readme.io` (deferred). When that is added,
  only `scripts/convert_docs_kb.py` changes — it fetches from the API instead
  of reading `merged_kb.md`. The KB, CLI, and chat tool are unaffected.
- A runtime chunker — splitting is done once by the conversion script, not at
  query time.
- `chunk` and `refresh` CLI sub-commands — `rebuild` is the only command.
- Multi-turn doc chat (one question per invocation).
- Hybrid search (BM25 + vector). Cosine-only retrieval is sufficient.

---

## 3. Architecture

### 3.1 Directory layout

```
resources/
  faqs/
    merged_kb.md               ← reference only after initial conversion
  docs/
    entries/                   ← source of truth (committed, hand-maintained)
      avni-overview.json
      javascript-rules-visit-schedule.json
      troubleshooting-form-fields.json
      …
    .embeddings.json           ← Voyage vector cache (content-hash gated, generated)
scripts/
  convert_docs_kb.py           ← one-time conversion script (run once, not part of CLI)
```

`resources/docs/.embeddings.json` is generated output — not committed. Run
`avni-docs-kb rebuild` after checkout to regenerate it.

### 3.2 Module layout

```
src/
  domain/
    docs/
      __init__.py
      knowledge_base.py        ← DocEntry, DocsKnowledgeBase
      kb_cli.py                ← avni-docs-kb entry point (rebuild only)
  chat/
    tools.py                   ← +answer_avni_question tool
    prompts.py                 ← +tool description
```

---

## 4. Component designs

### 4.1 Entry format (`resources/docs/entries/*.json`)

Each JSON file holds one topic entry:

```json
{
  "key": "javascript-rules-visit-schedule",
  "title": "JavaScript Rules — Visit Schedule Rules",
  "body": "Visit schedule rules determine when the next visit should be scheduled…"
}
```

`key` is the filename stem and the stable cache key. `title` is a short label
surfaced in `sources` when the tool responds. `body` is the full section text —
plain prose, no Markdown headings, no navigation links.

The `DocEntry` model:

```python
class DocEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str       # matches filename stem, e.g. "javascript-rules-visit-schedule"
    title: str     # short label, e.g. "JavaScript Rules — Visit Schedule Rules"
    body: str      # full section text used for embedding

    def embedding_text(self) -> str:
        return f"{self.title}\n\n{self.body}"
```

`embedding_text()` prepends the title so the embedding carries topic context —
the same pattern used by `HelperEntry` (name + signature + use_when).

### 4.2 Conversion script (`scripts/convert_docs_kb.py`)

Run **once** to bootstrap the JSON entries from `merged_kb.md`. Not part of the
runtime CLI.

**Algorithm:**

1. Read `resources/faqs/merged_kb.md` line by line.
2. Split at every H2 heading (`## …`). H1 headings are tracked as the current
   section label but do not create a new entry on their own — they are prefixed
   to the H2 title: `"{H1} — {H2}"`.
3. H3 content is merged inline into the parent H2's body (no separate entry per H3).
4. Strip readme.io JSX/MDX wrapper tags (`<Image …/>`, `<Anchor …/>`, `<br />`,
   `<CallOut …>…</CallOut>`) and image-only lines (`![](url)`).
5. Drop entries whose body is under 60 whitespace-split tokens (navigation stubs,
   empty sections, pure link-lists).
6. Slugify the combined title to produce `key` (lowercase, spaces → hyphens,
   strip non-alphanumeric except hyphens).
7. Write one `{key}.json` per entry under `resources/docs/entries/`.

After the script runs, the JSON files are reviewed, hand-edited as needed, and
committed. `merged_kb.md` plays no further role.

### 4.3 Knowledge base (`domain/docs/knowledge_base.py`)

`DocsKnowledgeBase` mirrors `KnowledgeBase` from `domain/rules/knowledge_base.py`
— one catalog type, no `applies_to` filtering.

```python
class DocsKnowledgeBase:
    def __init__(self, root: Path | None = None) -> None:
        # root defaults to resources/docs/
        ...

    def retrieve(self, query: str, top_k: int = 5) -> list[DocEntry]:
        # embed query, cosine-score against cached vectors, return top-k
        ...

    def rebuild(self) -> None:
        # load entries, embed changed ones, write .embeddings.json
        ...
```

Embedding uses the shared `VoyageEmbedder` and helper functions imported from
`domain.rules.knowledge_base` (`VoyageEmbedder`, `_content_hash`, `_top_k`,
`_estimate_tokens`). Cache format is identical:

```json
{
  "version": 1,
  "entries": {
    "<key>": { "hash": "sha256...", "vector": [...] }
  }
}
```

Content-hash gating: only entries whose `hash` differs from the cache are
re-embedded on `rebuild`.

When `VOYAGE_API_KEY` is absent, `retrieve()` raises `RuntimeError` — the caller
(`answer_avni_question`) catches it and returns a graceful fallback message.

### 4.4 Chat tool (`chat/tools.py`)

```python
_docs_kb = DocsKnowledgeBase()

@tool
def answer_avni_question(question: str) -> dict:
    """Answer a question about Avni implementation using the knowledge base."""
    if not os.environ.get("VOYAGE_API_KEY"):
        return {
            "answer": "VOYAGE_API_KEY is not configured — knowledge base search unavailable.",
            "sources": [],
        }
    entries = _docs_kb.retrieve(question, top_k=5)
    if not entries:
        return {"answer": "No relevant documentation found.", "sources": []}
    context = "\n\n---\n\n".join(
        f"[{e.title}]\n{e.body}" for e in entries
    )
    prompt = (
        "You are an Avni implementation expert. Answer the question below "
        "using only the knowledge base excerpts provided. If the answer is "
        "not in the excerpts, say so — do not guess.\n\n"
        f"Knowledge base excerpts:\n{context}\n\n"
        f"Question: {question}"
    )
    response = _llm.invoke(prompt)
    return {"answer": response.content, "sources": [e.title for e in entries]}
```

`_docs_kb` is a module-level singleton constructed alongside `_rule_generator`.
Lazy — entries and vectors are loaded on first `retrieve()` call.

**System prompt addition (`chat/prompts.py`):**

```
  - answer_avni_question(question) — answer a question about Avni using
    the implementer knowledge base. Use when the user asks about Avni
    concepts, features, form configuration, rules, troubleshooting, or
    how the platform works. Examples: "what is a catchment?", "how does
    program enrolment work?", "why are my form fields not showing?".
```

### 4.5 CLI (`domain/docs/kb_cli.py`)

Entry point registered as `avni-docs-kb` in `pyproject.toml`.

Single sub-command:

| Sub-command | What it does |
|---|---|
| `rebuild` | Load `resources/docs/entries/*.json`, re-embed changed entries, write `resources/docs/.embeddings.json`. Requires `VOYAGE_API_KEY`. |

```bash
avni-docs-kb rebuild          # after editing any entry JSON file
avni-docs-kb rebuild --docs-root <path>    # override resources/docs/
```

---

## 5. Data flow

```
resources/faqs/merged_kb.md
      │  scripts/convert_docs_kb.py  (run once)
      ▼
resources/docs/entries/*.json  (committed, hand-maintained)
      │  avni-docs-kb rebuild  (Voyage)
      ▼
resources/docs/.embeddings.json
      │  retrieve (cosine sim, live at query time)
      ▼
DocsKnowledgeBase.retrieve(query) → top-5 DocEntry
      │
      ▼
answer_avni_question tool → Claude Sonnet 4.6 → answer + sources
```

---

## 6. Embedding cost and performance

| Operation | Volume | Voyage calls | Notes |
|---|---|---|---|
| `rebuild` (first run) | ~80–120 entries | ~2–3 batch calls | One-time; run after checkout |
| `rebuild` (incremental) | only changed entries | 0–1 typical | Content-hash gated |
| `retrieve` (per question) | 1 query | 1 call | Single embed, brute-force cosine |

Env-var knobs (`VOYAGE_BATCH_TOKEN_BUDGET`, `VOYAGE_MIN_REQUEST_INTERVAL_SEC`,
`VOYAGE_MAX_RETRIES`) govern the shared `VoyageEmbedder` and apply to doc
embedding as well as rule embedding.

---

## 7. Testing

- `tests/domain/docs/test_knowledge_base.py` — entry loading from fixture JSON
  files, cache load/save, content-hash invalidation, cosine ranking with a mock
  `VoyageEmbedder` returning fixed vectors.

---

## 8. Configuration reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `VOYAGE_API_KEY` | For embed/retrieve | — | Voyage AI key (shared with rules KB) |
| `AVNI_DOCS_ROOT` | No | `resources/docs/` | Override docs KB root |

---

## 9. Setup

After checkout or whenever entry JSON files are edited:

```bash
avni-docs-kb rebuild
```

To bootstrap entry files from scratch:

```bash
python scripts/convert_docs_kb.py   # run once; review output; commit entries
avni-docs-kb rebuild
```

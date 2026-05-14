# LangGraph Refactor — Software Design Document

## 1. Objective

Reshape `src/` to follow the standard LangGraph project layout — state, nodes, graph, tools, prompts, and agent each in their own file — while taking the opportunity to fix structural debt (oversized `parser.py`, manual-merge state plumbing, hidden side effects at import time, mixed concerns in `pipeline.py` and `chat.py`).

The goal is a codebase that is:
- **Maintainable** — each file owns one responsibility; adding a node or a tool is a localised change.
- **Extensible** — new nodes, tools, prompts, or graphs can be added without rewriting existing modules.
- **Production-ready** — durable state, observable execution, testable in isolation, configurable without code edits.

This refactor is **behaviour-preserving**. The pipeline and editor produce identical output; the chat REPL behaves the same way from the user's perspective. Only the internal layout changes.

---

## 2. Current State and Pain Points

| File | LOC | Responsibility today |
|---|---|---|
| `parser.py` | 1841 | xlsx → `EntitySpec`. Mixes helpers, sheet classifier, entity parsers, W3H parser, form parser, file loaders, post-processing. |
| `pipeline.py` | 623 | State schema **+** node implementations **+** apply-change domain logic **+** graph wiring **+** runtime helpers. |
| `bundle_editor.py` | 605 | Field editor on bundle ZIP. |
| `generators.py` | 483 | `EntitySpec` → JSON. |
| `chat.py` | 461 | Tool definitions **+** agent build **+** system prompt **+** REPL streaming **+** slash commands **+** env loading. |
| `models.py` | 366 | Shared Pydantic types (specs, change kinds, edit ops). |
| `enricher.py` | 196 | Per-form LLM enrichment. |
| `llm_helper.py` | 189 | Anthropic client wrapper. |

### Pain points

1. **Mixed concerns in `pipeline.py` and `chat.py`.** Two of the larger files each do five different things; adding a node or a tool means navigating around unrelated code.
2. **`parser.py` is too big.** Already organised into clear `# ──` sections; would split cleanly along those seams.
3. **Redundant `{**state, ...}` spread.** Every node returns `{**state, "x": new_x, ...}` — the spread is dead code, since LangGraph already merges per-key. Dropping it doesn't change behaviour but does shorten every node.
4. **Side effects at import time.** `chat.py` reads `.env` and compiles `_pipeline_graph` as a side effect of importing the module; tests can't import `chat` without those running.
5. **`sys.path.insert(0, _HERE)` repeated in three files.** Works, but indicates the lack of an actual package root. The subpackage layout makes `src/` a proper package; this hack disappears.
6. **No config layer.** Model name, token budget, paths, log levels are all hard-coded in the file that uses them.
7. **No tests.** `tests/` directory is empty. Pure node functions are easy to test but the current node implementations mix I/O with state transitions, making unit tests awkward.
8. **`MemorySaver` only.** Pipeline state evaporates when the Python process exits — fine for development, but a production deployment that survives restarts needs a durable checkpointer.

---

## 3. Target File Layout

Four subpackages — each one is a single architectural role. Inside each, role-separated files keep LangGraph idioms intact.

```
src/
  __init__.py
  config.py                       # Settings reading env / .env
  logging_setup.py                # setup_logging + log_node context manager
  models.py                       # Shared Pydantic types (FieldSpec, EntitySpec,
                                  # Change, EditOperation, ...)

  # ── pipeline/ : the LangGraph generation pipeline ──────────────────────────
  pipeline/
    __init__.py                   # Re-exports: build_graph, initial_state, run
    state.py                      # BundleState TypedDict + initial_state factory
    nodes.py                      # discover_files, parse_documents, enrich_with_llm,
                                  # apply_user_decisions, generate_*, package_zip
    graph.py                      # build_graph, _can_proceed, run

  # ── chat/ : the ReAct agent surface ────────────────────────────────────────
  chat/
    __init__.py                   # Re-exports: run_chat
    agent.py                      # build_agent
    prompts.py                    # SYSTEM_PROMPT
    tools.py                      # @tool defs; module-level pipeline graph instance
    repl.py                       # _stream_turn, _handle_slash, run_chat, __main__

  # ── domain/ : business modules the pipeline + chat call into ──────────────
  domain/
    __init__.py
    generators.py                 # EntitySpec → JSON dicts
    enricher.py                   # Per-form LLM enrichment (produces `Change`s)
    changes.py                    # apply_resolutions, _apply_one, _parse_edit,
                                  # _find_field (applies confirmed `Change`s to FormSpecs)
    llm.py                        # Was llm_helper.py (Anthropic client wrapper)
    bundle_editor.py              # Bundle-ZIP field editor
    parser/                       # xlsx → EntitySpec (was the 1841-line parser.py)
      __init__.py                 # Re-exports: parse_scoping_docs, consolidate_and_audit,
                                  # _fuzzy_match (public surface)
      helpers.py                  # _clean, _fuzzy_match, _map_data_type, _find_col
      classifier.py               # _classify_sheet
      entities.py                 # parse_subject_types, parse_programs, parse_encounters,
                                  # parse_location_hierarchy
      w3h.py                      # parse_w3h, _match_sheet_to_w3h
      forms.py                    # parse_form_df, parse_unified_modelling,
                                  # _detect_skip_logic_patterns
      loaders.py                  # _load_xlsx / _csv / _txt / _markdown_tables / _load_file
      post.py                     # _resolve_form_subject_types
      audit.py                    # consolidate_and_audit
```

### Top-level inventory

Only 6 entries directly under `src/`:

| Entry | Purpose |
|---|---|
| `config.py` | env-driven settings |
| `logging_setup.py` | logging config + per-node timing context manager |
| `models.py` | shared Pydantic types used everywhere |
| `pipeline/` | the LangGraph pipeline (state + nodes + graph) |
| `chat/` | the chat agent (tools + prompts + repl) |
| `domain/` | all business modules the pipeline and chat call into — including `parser/` as a sub-subpackage |

### Why this shape

- **`pipeline/` is the LangGraph machine.** Adding a node? Touch `pipeline/nodes.py`. Changing topology? Touch `pipeline/graph.py`. Adjusting state? Touch `pipeline/state.py`. Nothing else.
- **`chat/` is the agent surface.** Could be replaced with a web UI or a Slack adapter without touching `pipeline/` or `domain/`.
- **`domain/` is all business code.** No LangGraph imports inside; pure functions taking and returning Pydantic models or plain dicts. Reusable from a CLI, REST API, or a different graph entirely. Parser is the largest tenant — big enough to deserve its own subdirectory inside `domain/`, but it's still domain code, not a separate architectural layer.
- **Cross-cutting (`config`, `logging_setup`, `models`) stays at the top** — these are imported by every subpackage; nesting them would force everyone into deeper relative-import paths.

### Import paths after the move

```python
# pipeline-internal
from pipeline.state import BundleState
from pipeline.nodes import discover_files
from pipeline.graph import build_graph

# chat-internal
from chat.tools import TOOLS
from chat.agent import build_agent

# cross-package
from pipeline.graph import build_graph                # chat/tools.py uses this
from domain.parser import parse_scoping_docs          # pipeline/nodes.py uses this
from domain.generators import make_uuid               # domain/bundle_editor.py uses this
from domain.enricher import enrich_forms              # pipeline/nodes.py uses this
from domain.changes import apply_resolutions          # pipeline/nodes.py uses this
from models import EntitySpec, Change                 # everyone uses this
from config import settings                           # everyone uses this
```

All imports become absolute against the `src` root. The `sys.path.insert(0, _HERE)` hack disappears: `src/` becomes a real package (every dir has `__init__.py`), and the project is launched with `python -m chat.repl` (or via a console-script entry in `pyproject.toml` that points at `chat.repl:run_chat`).

---

## 4. State (`src/state.py`)

`BundleState` keeps its current shape — every field declared as a plain type, default overwrite reducer per key. No `Annotated[...]` reducers.

The one cleanup: **drop the redundant `{**state, ...}` spread** in every node's return. LangGraph already merges per-key, so the spread is dead code. Returning only the keys the node sets is shorter and behaves identically.

Before:
```python
def discover_files(state: BundleState) -> BundleState:
    errors = list(state.get("errors", []))
    if not os.path.isdir(state["input_dir"]):
        errors.append(f"Input directory not found: {state['input_dir']}")
    return {**state, "file_paths": files, "errors": errors}
```

After:
```python
def discover_files(state: BundleState) -> dict:
    errors = list(state.get("errors", []))
    if not os.path.isdir(state["input_dir"]):
        errors.append(f"Input directory not found: {state['input_dir']}")
    return {"file_paths": files, "errors": errors}
```

The manual `errors = list(state.get("errors", [])); errors.append(...)` pattern stays for now — only 3-4 fields actually accumulate, and the existing code is uniform and readable. Reducers can be revisited if/when the pipeline gains parallel branches or many more accumulator writers (see §14).

`initial_state(...)` lives in `state.py`, same factory as today.

---

## 5. Nodes (`src/nodes.py`)

Each node is a function `state → partial state`. I/O happens (file reads, LLM calls), but the return is only the keys the node sets — no `{**state, ...}` spread.

```python
def discover_files(state: BundleState) -> dict: ...
def parse_documents(state: BundleState) -> dict: ...
def enrich_with_llm(state: BundleState) -> dict: ...
def apply_user_decisions(state: BundleState) -> dict: ...
def generate_entities(state: BundleState) -> dict: ...
def generate_forms(state: BundleState) -> dict: ...
def generate_form_mappings(state: BundleState) -> dict: ...
def package_zip(state: BundleState) -> dict: ...
```

### 5.1 No domain logic inside nodes

The current `pipeline.py` hosts `_apply_resolutions`, `_apply_one`, `_parse_edit`, `_find_field`, `_all_fields_in` — pure mutation logic over `FormSpec`/`Change`, with no LangGraph imports. These move to **`domain/changes.py`** alongside the enricher (which produces `Change`s — `changes.py` consumes them). The `apply_user_decisions` node becomes a thin orchestrator:

```python
from domain.changes import apply_resolutions
from langgraph.types import interrupt
from models import Change

def apply_user_decisions(state: BundleState) -> dict:
    pending = state.get("pending_changes") or []
    if not pending:
        return {}
    resolutions = interrupt({"kind": "confirm_changes", "org": state["org_name"], "changes": pending})

    spec = state["entity_spec"]
    applied, post_warnings = apply_resolutions(
        spec.forms,
        [Change(**d) for d in pending],
        resolutions or {},
    )
    return {
        "entity_spec": spec,
        "pending_changes": [],
        "applied_changes": list(state.get("applied_changes") or []) + [c.model_dump() for c in applied],
        "enrich_warnings": list(state.get("enrich_warnings") or []) + post_warnings,
    }
```

The node only deals with state shape and the `interrupt()` mechanic; the mutation logic lives in `domain/changes.py` and is independently testable.

### 5.2 No deferred imports

Today's `pipeline.py` does `from parser import ...` and `from enricher import ...` inside node bodies to "avoid circulars". After the refactor those imports move to module top — there's no circular dependency in the new layout because nodes don't import from each other.

---

## 6. Graph (`src/pipeline/graph.py`)

```python
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from pipeline.state import BundleState, initial_state
from pipeline.nodes import (
    discover_files, parse_documents, enrich_with_llm, apply_user_decisions,
    generate_entities, generate_forms, generate_form_mappings, package_zip,
)


def _can_proceed(state: BundleState) -> str:
    if state.get("errors") and not state.get("file_paths"):
        return "abort"
    if state.get("errors") and state.get("entity_spec") is None:
        return "abort"
    return "continue"


def build_graph(checkpointer=None):
    g = StateGraph(BundleState)
    g.add_node("discover_files", discover_files)
    g.add_node("parse_documents", parse_documents)
    g.add_node("enrich_with_llm", enrich_with_llm)
    g.add_node("apply_user_decisions", apply_user_decisions)
    g.add_node("generate_entities", generate_entities)
    g.add_node("generate_forms", generate_forms)
    g.add_node("generate_form_mappings", generate_form_mappings)
    g.add_node("package_zip", package_zip)

    g.set_entry_point("discover_files")
    g.add_conditional_edges("discover_files", _can_proceed,
                            {"continue": "parse_documents", "abort": END})
    g.add_conditional_edges("parse_documents", _can_proceed,
                            {"continue": "enrich_with_llm", "abort": END})
    g.add_edge("enrich_with_llm", "apply_user_decisions")
    g.add_edge("apply_user_decisions", "generate_entities")
    g.add_edge("generate_entities", "generate_forms")
    g.add_edge("generate_forms", "generate_form_mappings")
    g.add_edge("generate_form_mappings", "package_zip")
    g.add_edge("package_zip", END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
```

A graph file should be skimmable. After the refactor, it is — every node imported by name, every edge spelled out, no helpers inline.

---

## 7. Agent Surface (`src/chat/`)

### 7.1 `chat/prompts.py`

```python
SYSTEM_PROMPT = """You are an Avni bundle generator assistant..."""
```

Prompts live as module-level constants. Easy to A/B test, easy to diff in PRs, easy to grep.

### 7.2 `chat/tools.py`

All `@tool` functions plus the module-level compiled pipeline graph instance:

```python
from pipeline.graph import build_graph
from pipeline.state import initial_state
from domain.bundle_editor import apply_field_edits, list_bundle_fields as _list_bundle_fields

_pipeline_graph = build_graph()       # compiled once, shared by all tool calls

@tool
def generate_bundle(org: str, user_instructions: str | None = None) -> dict: ...
@tool
def resume_bundle(thread_id: str, resolutions: dict[str, str]) -> dict: ...
@tool
def list_bundle_fields(bundle_path: str) -> dict: ...
@tool
def edit_bundle_fields(bundle_path: str, operations: list[dict]) -> dict: ...

TOOLS = [generate_bundle, resume_bundle, list_bundle_fields, edit_bundle_fields]
```

Compiling the graph at module load is intentional: subsequent tool calls reuse the same checkpointer-backed graph, which is what makes `resume_bundle` work.

### 7.3 `chat/agent.py`

```python
from langchain_anthropic import ChatAnthropic
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from chat.prompts import SYSTEM_PROMPT
from chat.tools import TOOLS
from config import settings


def build_agent():
    model = ChatAnthropic(model=settings.model, max_tokens=settings.max_tokens,
                          thinking={"type": "adaptive"})
    return create_react_agent(
        model=model, tools=TOOLS, prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )
```

### 7.4 `chat/repl.py`

REPL only — stream events, parse slash commands, run the conversation loop, host the `__main__` entry point. Imports `build_agent` from `chat.agent`. Launched via `python -m chat.repl` or a console-script entry.

---

## 8. Configuration (`src/config.py`)

A Pydantic `BaseSettings` (or a plain dataclass) read once from environment variables / `.env`:

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8192
    input_root: str = "resources/input"
    output_root: str = "resources/output"
    log_level: str = "INFO"
    checkpointer_kind: str = "memory"     # memory | sqlite | postgres
    sqlite_path: str = ".langgraph.sqlite"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
```

Centralising settings means:
- The hard-coded `MODEL`, `MAX_TOKENS`, `INPUT_ROOT`, `OUTPUT_ROOT` in `chat.py` and `_pipeline_graph` boilerplate go away.
- A deployment can override anything via env vars without code changes.
- Tests can construct alternative `Settings()` instances cheaply.

The `.env` loading in `chat.py` (currently a hand-rolled parser) is replaced by Pydantic's `env_file` support.

---

## 9. Logging & Observability (`src/logging_setup.py`)

A single `setup_logging(level: str)` function called once at startup (from `repl.py` or any other entry point). Configures the root logger and per-module levels (`pipeline`, `enricher`, `llm`) consistently.

Per-node structured logs already happen (`log.info("[%s] Parsed — subjects=%d ...", ...)`). The refactor adds **structured per-node start/end events** so a future log aggregator can extract timing easily:

```python
def discover_files(state: BundleState) -> dict:
    with log_node("discover_files", org=state["org_name"]):
        ...
```

`log_node` is a context manager in `logging_setup.py` that emits `node_start` and `node_end` events with elapsed time. Cheap, no external dependency.

For production deployments, swap the formatter for JSON output and pipe to whatever the deployment uses (CloudWatch, Datadog, Loki). The structure is already there.

---

## 10. Error Handling

Today, several nodes catch broad exceptions, append to `errors`, and return early — most of that machinery is sound. The refactor formalises one rule:

> **Nodes never raise.** A node either returns a delta (success or partial success with `errors`) or returns `{"errors": [...]}` and lets the conditional edge route to `END`.

This means:
- The framework's automatic retry (if ever configured) doesn't fire on programmer errors.
- The state always reflects the run's truth — if there's an error, it's in `errors`, never silently swallowed.
- Tests can verify error paths by inspecting state, not by catching exceptions.

The editor (`bundle_editor.py`) follows the same rule already — failures land in `EditResult.rejected`.

---

## 11. Persistence: Production Checkpointer

`MemorySaver` is fine for development and tests but loses state across process restarts. Production needs durability.

`config.checkpointer_kind` selects:

| Value | Use |
|---|---|
| `memory` | Dev / tests. The default. |
| `sqlite` | Local persistence — survives restarts on a single machine. Path from `settings.sqlite_path`. |
| `postgres` | Multi-process / multi-machine production. Connection from env. |

`graph.build_graph()` and `agent.build_agent()` accept a checkpointer argument; a small factory in `config.py` returns the right one based on settings. No node code changes.

Note: the chat agent and the pipeline have **separate** checkpointers (two graphs, two stores). Both should use the same kind in production.

---

## 12. Testing Strategy

The refactor unblocks unit testing:

| Module | Test approach |
|---|---|
| `state.py` | Snapshot test of `initial_state()` shape; verify reducer behaviour with a tiny synthetic state. |
| `nodes.py` | Each node is pure (state in → delta out). Hand-construct a minimal `BundleState`, call the node, assert on the delta. No graph compilation needed. |
| `apply_changes.py` | Pass synthetic `FormSpec` lists + Changes, assert mutations. Already pure. |
| `graph.py` | One integration test that runs the whole graph on a known-good org and snapshots key counts. |
| `parser/`, `generators.py`, `enricher.py`, `bundle_editor.py` | Domain logic — already pure functions; straightforward unit tests. |
| `tools.py` | Mock the pipeline graph; assert the tool returns the expected `done` / `needs_confirmation` shape. |

Target: every node has at least one happy-path test. Domain modules (parser submodules, generators, editor) each have ≥3 tests covering the typical, edge, and error cases.

---

## 13. Migration Plan

Done in five small commits, each of which keeps the project running. The branch passes `python -m chat.repl` (the new entry point) at every step.

### Step 1: Move domain files into `domain/`

- Create `src/domain/` with `__init__.py`.
- Move `generators.py`, `enricher.py`, `bundle_editor.py`, `parser.py` into `src/domain/`. (Parser stays a single file for now; it gets split in Step 4.)
- Rename `llm_helper.py` → `domain/llm.py`.
- Update imports in callers (`pipeline.py`, `chat.py`, `enricher.py`, `bundle_editor.py`, `parser.py`).

After this commit, behaviour is unchanged; only domain modules have moved.

### Step 2: Split `pipeline.py` into `pipeline/` (and lift change-application to `domain/`)

- Create `src/pipeline/` with `__init__.py` re-exporting `build_graph`, `initial_state`, `run`.
- Extract `BundleState` + `initial_state` → `pipeline/state.py`.
- Extract change-application helpers (`_apply_resolutions`, `_apply_one`, `_parse_edit`, `_find_field`, `_all_fields_in`) → **`domain/changes.py`** (pure mutation on `FormSpec`/`Change`, no LangGraph). Rename `_apply_resolutions` → `apply_resolutions` since it's now a public domain function.
- Move all node functions → `pipeline/nodes.py`. `apply_user_decisions` now imports from `domain.changes`.
- Move `_can_proceed`, `build_graph`, `run` → `pipeline/graph.py`.
- Delete the old `src/pipeline.py`.

While moving each node, **drop the redundant `{**state, ...}` spread** from its return. Behaviour identical; node bodies shorter. The manual accumulator pattern (`errors = list(state.get(...)); errors.append(...)`) stays as-is.

### Step 3: Split `chat.py` into `chat/`

- Create `src/chat/` with `__init__.py` re-exporting `run_chat`.
- Extract `SYSTEM_PROMPT` → `chat/prompts.py`.
- Extract `@tool` definitions + the module-level `_pipeline_graph` instance + `_summarize` + `_run_with_interrupt_handling` → `chat/tools.py`.
- Extract `build_agent` + model constants → `chat/agent.py`.
- Extract `_stream_turn`, `_extract_text`, `_handle_slash`, `run_chat`, `__main__` block → `chat/repl.py`.
- Delete the old `src/chat.py`.
- Update `pyproject.toml` console-script entry to `chat.repl:run_chat` (or whatever the project's entry-point convention is).

### Step 4: Split `parser.py` into `domain/parser/`

- Move each `# ──` section into its own file under `domain/parser/`.
- `domain/parser/__init__.py` re-exports `parse_scoping_docs`, `consolidate_and_audit`, `_fuzzy_match` (the public surface).
- Update the one import in `pipeline/nodes.py` (`from domain.parser import parse_scoping_docs`).

### Step 5: Introduce `config.py` and `logging_setup.py`

- Centralise settings.
- Replace `MODEL`, `MAX_TOKENS`, `INPUT_ROOT`, `OUTPUT_ROOT`, `.env` loading.
- Add `setup_logging` called once from `chat/repl.py`.

Steps are intentionally small. Step 1 (file moves into `domain/`) is the safest starting point; everything after builds on it.

### Deferred: Pluggable checkpointer

Originally proposed as Step 6, deferred for now. `MemorySaver` is used everywhere — fine for dev, in-process, and the current chat REPL flow. Revisit only when a production-grade deployment (multi-process, restart-resilient) is on the table; until then, the extra abstraction is dead weight.

---

## 14. Out of Scope

- **Behaviour changes** to parsing, enrichment, generation, or editing logic. The refactor is structural only.
- **State reducers** (`Annotated[list[T], operator.add]`). The current manual-merge pattern (`errors = list(state.get(...)); errors.append(...)`) stays. With 8 nodes and 3-4 accumulator fields the win is modest, and the migration adds behavioural risk to an otherwise structural change. Revisit if/when the pipeline grows parallel branches (where reducers become mandatory) or many more accumulator writers.
- **Adding new pipeline features** (parallel form enrichment, retry loops, multi-pass enrichment) — possible follow-ups, not part of this work.
- **Switching off LangGraph** for the editor. The editor stays a plain function; this refactor doesn't alter that boundary.
- **Web UI / API surface.** The repl stays the only entry point. The structure makes a future REST or Studio adapter trivial, but adding one is its own SDD.
- **Authentication / multi-tenancy / deployment infrastructure.** Production-ready in the sense of "the code is shaped for it"; the operational pieces (containers, secrets, deployment) are deployment concerns.

# Intelligent Form Parsing — Software Design Document

## 1. Objective

Today's parser (`src/parser.py`) is fully deterministic — fixed separator priority for options, hard-slice for long names, no awareness when a question name is reused inside a single form, and a fixed-table data-type lookup. Several real input documents (Durga India, Ekam) hit edge cases where the deterministic path produces incorrect or unusable output, and new edge cases keep appearing (predict min/max, infer MultiSelect from question phrasing, accept user-driven additions, …).

This SDD describes a **hybrid** architecture:

1. **Deterministic baseline** — the existing parser keeps producing a best-effort `FormSpec` from each form sheet. Free, offline, idempotent.
2. **LLM enrichment pass** — Claude receives the raw DataFrame **plus** the parser's `FormSpec` and returns a refined `FormSpec` validated against the Pydantic schema. Confidence flags accompany every refinement.
3. **Validation + confirmation** — refinements are cross-checked against the source sheet (no inventing fields/options); high-confidence ones auto-apply, low-confidence and lossy ones are confirmed with the user.

The LLM does the work that's hard to enumerate in code: option splitting when delimiters are mixed, data-type guessing from field names, MultiSelect detection from "select all that apply" phrasing, min/max prediction (e.g. age 0–120, weight 0–500), long-name shortening, duplicate-field disambiguation, and applying user-supplied additions like *"also add a Sponsor field to Pregnancy Enrolment."*

Concerns covered:

| # | Concern | Confirmation                                                                                                                                                 |
|---|---|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1 | Option splitting when delimiters are mixed or appear inside an option | Auto-apply if validation passes(validation with deterministic parser output); confirm only when the LLM and the deterministic split disagree on options list |
| 2 | Field/option names > 255 characters | **Always** — semantic shortening is a judgment call                                                                                                          |
| 3 | Same question repeated within one form | **Always** — context-dependent                                                                                                                               |
| 4 | Missing data type, missing MultiSelect flag, missing unit | Auto-apply if confidence ≥ threshold; confirm otherwise                                                                                                      |
| 5 | min/max bounds for Numeric fields when not provided | Auto-apply if confidence ≥ threshold; confirm otherwise                                                                                                      |
| 6 | User-driven add/remove field via chat | **Always** — natural side-effect of the chat round-trip                                                                                                      |

---

## 2. Status of Current Code

### 2.1 Option parsing (`parser.py:148`)

```python
def _parse_options(val):
    raw = _clean(val)
    if "\n" in raw:   parts = raw.split("\n")
    elif ";" in raw:  parts = raw.split(";")
    else:             parts = raw.split(",")
```

Fails when a single option contains a comma and the chosen separator is also a comma. Real example from Durga India:

> `To prevent and address sexual harassment of women at the workplace including those in the unorganised sector (ex domestic workers, beauty parlors)`

Currently split into two on the inner comma.

### 2.2 Name truncation (`generators.py:33`)

```python
def truncate(value, limit=255):
    return value[:limit] if len(value) > limit else value
```

Pure byte-slice. Two distinct long questions can truncate to the same 255-char prefix and collide on UUID seed `concept:{name}`.

### 2.3 Duplicate-field bug (`generators.py:219`)

If the same field name appears twice in one form, both `formElement` blocks get the same UUID seed `formElement:{form}:{name}` → identical UUIDs → upload-time uniqueness violation. The concept registry silently dedupes by lowercase name, so one occurrence is also lost from `concepts.json`.

### 2.4 Data-type inference (`parser.py:272`)

```python
def _map_data_type(raw):
    return _DATA_TYPE_MAP.get(raw.strip().lower(), "Text")
```

Static dict. Blank or unrecognized → `Text`. No content inspection. `SingleSelect`/`MultiSelect` derived only from an explicit "Selection Type" column.

### 2.5 No min/max prediction

`_parse_min_max` extracts a range only when the source cell explicitly contains one. `Numeric` fields with no range cell stay unconstrained.

### 2.6 No path for user-driven changes

Once parsing finishes, there is no way to say *"also add a Sponsor field"* without editing the source sheet and re-running.

---

## 3. Pipeline Changes

A new node `enrich_with_llm` is inserted between `parse_documents` and `generate_entities`:

```
discover_files → parse_documents → enrich_with_llm → generate_entities
                                       ↑
                                   sends each form sheet
                                   + parser's FormSpec
                                   to Claude → refined FormSpec
                                   → validate → confirm → apply
```

The node is a pure function over `EntitySpec` plus an `LLMHelper` injected by the host. When confirmation is needed, the node calls LangGraph's `interrupt()` — the graph pauses, the caller resumes it with `Command(resume=user_resolutions)`. The graph never knows whether the caller is the chat agent, a FastAPI endpoint, or a future web UI.

```python
from langgraph.types import interrupt

def enrich_with_llm(state):
    # ...build pending_changes...
    if pending_changes_needing_confirmation:
        resolutions = interrupt({
            "kind": "confirm_changes",
            "changes": pending_changes_needing_confirmation,
        })
        # resolutions: dict[change_id, "yes" | "no" | "edit:<value>"]
        # ...apply...
```

| Caller | How it handles the interrupt |
|---|---|
| `chat.py` | `agent.stream()` yields the interrupt event; agent presents the changes to the user; user replies; agent calls `agent.invoke(Command(resume=...), config)` |
| Future FastAPI | First endpoint runs the graph, returns the interrupt payload + `thread_id`; second endpoint receives the user's resolutions + `thread_id`, calls `Command(resume=...)`, returns the final result |

This replaces the previous "two-call generate_bundle with `confirm_changes` argument" pattern. The pause is a proper graph checkpoint, so it survives process restarts and is serialisable (no runtime callable in `BundleState`).

Each refinement is a structured `Change`:

```python
class Change(TypedDict):
    change_id: str            # "form-2/field-7/options"
    form: str
    field: str                # "" for form-level (e.g. "add a new field")
    kind: Literal[
        "options", "data_type", "selection_type", "min_max",
        "long_name", "duplicate_field", "add_field", "remove_field"
    ]
    before: dict              # current value(s)
    after: dict               # proposed value(s)
    reason: str               # plain-English why
    confidence: float         # 0.0 – 1.0, set by Claude
    auto_apply: bool          # derived: True iff confidence ≥ threshold AND change is non-lossy
```

Auto-apply rules:

| Kind | Auto-apply when |
|---|---|
| `options` | LLM confirms ≥ deterministic split count AND validates against the source cell text |
| `data_type` | confidence ≥ 0.9 AND the inferred type is one of `Date / PhoneNumber / ImageV2 / Numeric` based on a clear name keyword |
| `selection_type` | "select all that apply" or "(multi)" appears literally in the question text |
| `min_max` | **Never auto-apply** — always confirm |
| `long_name` | **Never auto-apply** — always confirm |
| `duplicate_field` | **Never auto-apply** — always confirm |
| `add_field` / `remove_field` | **Never auto-apply** — only ever requested by the user |

---

## 4. The LLM Enrichment Call

### 4.1 Input

For each form sheet, build a single Claude call with:

- **System prompt** (cached, identical across forms): role + JSON-schema description + auto-apply rules + "do not invent fields/options that aren't in the sheet."
- **User content**:
  - The raw DataFrame, serialized as Markdown table (≈ 5–30 KB for typical forms).
  - The deterministic parser's `FormSpec` for that form, serialized as JSON.
  - Optional `user_instructions` — text from the chat user (e.g. *"also add a Sponsor field"*) when present.

### 4.2 Output schema

A Pydantic model that Claude must return exactly. Use `client.messages.parse(output_format=EnrichedFormSpec)`:

```python
class EnrichedField(FieldSpec):
    confidence: float
    inferred_fields: list[str] = []   # e.g. ["dataType", "min", "max"]
    notes: str = ""

class EnrichedFormSpec(BaseModel):
    form: FormSpec                    # the refined form (LLM output)
    changes: list[Change]             # one entry per refinement vs. parser output
```

Claude returns the entire refined `FormSpec` plus a `changes` list. If Claude has no refinements, `changes` is empty and the form passes through unchanged.

### 4.3 Validation (anti-hallucination)

Before any `Change` is applied, `enrich_with_llm` validates:

| Check | Action on failure |
|---|---|
| Every `field.name` in `EnrichedFormSpec.form` either matches the parser's output **or** fuzzy-matches the source DataFrame at threshold ≥ 85 | Drop that field; log warning |
| Every Coded `field.options` entry fuzzy-matches the source cell text at threshold ≥ 85 | Drop that option; log warning |
| `field.dataType` is one of the supported Avni types | Coerce to `Text`; log warning |
| Every `Change.change_id` is unique within the form | Renumber |
| `add_field` Changes only fire when the user explicitly requested an addition | Drop; log warning |

**Fuzzy match** uses [rapidfuzz](https://github.com/rapidfuzz/rapidfuzz)'s `partial_ratio`, not `ratio`. The reason matters here:

- `fuzz.ratio(option, source_cell)` scores the **whole strings** against each other. Validating an option like `"Yes"` against a 200-char source cell containing `"Yes\nNo\nMaybe\n…"` scores low — the option is a tiny fraction of the cell, so most of the cell looks like a "mismatch" even though `"Yes"` is clearly there.
- `fuzz.partial_ratio(option, source_cell)` slides the shorter string across the longer one and returns the best-aligned-window score. A genuine option in the cell scores ~100; an invented option scores ~50 or below. This is the only score that gives a meaningful threshold for our use case.

Why fuzzy at all (vs strict substring):

- Unicode form differences (e.g. `"Kachaa"` vs `"Kachā"`) — fail substring, pass `partial_ratio` ≥ 95
- Whitespace differences from LLM reformatting (`"don't know"` vs `" don't know "`) — same
- Case differences (`"Yes"` vs `"yes"`) — `partial_ratio` is case-insensitive when both inputs are lower()'d

Threshold 85 is the sweet spot for `partial_ratio` — high enough to reject genuinely invented options (`"Always"` when the cell has only `"Yes"` / `"No"` scores ~30), low enough to accept normalisation-only differences. Add `rapidfuzz>=3.0` to `pyproject.toml` (small C-extension dep).

### 4.4 When to skip the LLM call

For cost control, skip enrichment for forms where the deterministic parser has high confidence already:

- All fields have an explicit `dataType` from the source cell.
- All Coded fields have non-empty `options` and an explicit `selectionType`.
- All numeric fields have min and max values
- No field name exceeds 255 characters.
- No duplicate field names within the form.
- No user instructions are pending.

Forms that pass all six gates are passed through unchanged. Most clean forms will skip the LLM entirely.

---

## 5. User-driven Changes (Concern 6)

The chat agent can pass `user_instructions` straight through to the enrichment call. Three patterns supported:

| User says | LLM emits |
|---|---|
| *"Add a 'Sponsor' Text field to the Pregnancy Enrolment form"* | `Change(kind="add_field", form="Pregnancy Enrolment", after={"name": "Sponsor", "dataType": "Text"})` |
| *"Remove the 'Aadhaar Number' field everywhere"* | One `Change(kind="remove_field", ...)` per form that has it |
| *"Make 'Age' Numeric with min 0 max 120"* | `Change(kind="min_max", form=..., field="Age", after={"min": 0, "max": 120})` |

Every user-driven change goes through the same confirmation flow (`auto_apply=False`), so the user always sees the diff before it lands in the bundle.

---

## 6. Cost Model

Per form, in the worst case (every form goes through enrichment):

| Component | Tokens | Haiku cost |
|---|---|---|
| System prompt + schema (cached after first call) | ~2,000 in (10× cached at 0.1× rate) | ~$0.0002 / form |
| DataFrame + parser FormSpec | ~5,000–30,000 in | ~$0.005–0.03 / form |
| Output (refined FormSpec + changes) | ~1,000–3,000 out | ~$0.005–0.015 / form |
| **Total per form** | | **~$0.01–0.05** |  

For an org like Ekam (25 forms): worst-case **~$1.25**. With the skip-when-clean rule (§4.4), a clean modelling document costs zero (no API calls). Prompt-cache reads bring repeat-org cost down further.

If `ANTHROPIC_API_KEY` is unset or rate-limited, `enrich_with_llm` degrades to the previous deterministic-only behaviour and surfaces a warning in the run summary. The pipeline never hard-fails on missing LLM access.

---

## 7. Confirmation UX (chat)

The chat agent uses LangGraph's native interrupt/resume rather than re-invoking the tool. The `generate_bundle` tool wraps a streamed graph run:

```python
@tool
def generate_bundle(
    org: str,
    user_instructions: str | None = None,
) -> dict:
    config = {"configurable": {"thread_id": _thread_for(org)}}
    initial = {"org_name": org, "user_instructions": user_instructions, ...}
    return _run_until_done(pipeline, initial, config)
```

`_run_until_done` streams the graph; on `interrupt`, it surfaces the pending changes to the agent (as the tool result for that turn), the agent converses with the user to gather resolutions, then a follow-up tool call resumes the same `thread_id` with `Command(resume=resolutions)`.

Conversation example:

```
you> generate astita, also add a Sponsor field to Pregnancy Enrolment
  ⚙ generate_bundle({"org": "astitva", "user_instructions": "add a Sponsor field to Pregnancy Enrolment"})
agent> Pipeline ran with 14 auto-applied refinements. 3 changes need your confirmation:
        1. Add 'Sponsor' (Text) to Pregnancy Enrolment   ← from your instruction
        2. Long name in Baseline for Women (306 → 89 chars)
        3. Duplicate 'Date' in Pregnancy Enrolment → "LMP — Date" / "EDD — Date"
       Confirm 1, 2, 3?
you> 1 yes, 2 yes, 3 use "Period — Date" and "Delivery — Date"
  ⚙ resume_bundle({"thread_id": "...", "resolutions": {"...add_field...": "yes", ...}})
agent> Bundle generated. resources/output/astitva/Astitva.zip (3 user-confirmed + 14 auto-applied changes).
```

The tool surface is split into `generate_bundle` (start) and `resume_bundle` (continue) so each tool call is structurally simple — no overloaded "second call" semantics. Both share the `thread_id` and target the same in-flight graph state.

---

## 8. State Schema Additions

`BundleState` (in `pipeline.py`) — every field is JSON-serialisable, so the checkpointer can capture state across an `interrupt()`:

```python
class BundleState(TypedDict):
    ...
    user_instructions: str | None         # passed through from chat tool
    pending_changes: list[Change]         # surfaced via interrupt(); empty after resume
    applied_changes: list[Change]         # for the run summary + audit log
```

The previous `llm_helper` and `confirmer` fields are gone. `LLMHelper` is constructed lazily inside `enrich_with_llm` from environment (or skipped if `ANTHROPIC_API_KEY` is unset). Confirmation is handled by `interrupt()` + `Command(resume=...)` — no callable needs to live in state.

`FieldSpec` (in `models.py`) gets a single set of inference flags. Typed with `Literal` so a typo fails Pydantic validation rather than silently never matching:

```python
from typing import Literal

InferredField = Literal["dataType", "options", "min", "max", "selectionType", "unit"]

class FieldSpec(BaseModel):
    ...
    inferred_fields: set[InferredField] = set()
```

Usage: `if "dataType" in field.inferred_fields:` — extensible without schema changes when new inference targets are added.

---

## 9. Files to Modify / Create

| File | Change |
|---|---|
| `src/parser.py` | Populate `field.inferred_fields` when source cells are blank/unmapped; keep current logic otherwise |
| `src/models.py` | Add `inferred_fields: set[InferredField]` + `EnrichedField`, `EnrichedFormSpec`, `Change` types |
| `src/pipeline.py` | Add `enrich_with_llm` node; new `BundleState` fields; call `interrupt()` when pending changes need confirmation |
| `src/llm_helper.py` | **New** — wraps `ChatAnthropic` (Haiku); builds the enrichment prompt; uses `messages.parse` with the Pydantic schema; cache-controls the system prompt |
| `src/enricher.py` | **New** — orchestrates: skip-gates check → call `LLMHelper` → fuzzy-validate response (rapidfuzz) → split into auto-apply vs pending → apply auto-apply set |
| `src/chat.py` | Add `user_instructions` to `generate_bundle`; new `resume_bundle(thread_id, resolutions)` tool; both stream the graph and translate `interrupt` events into tool results |
| `src/generators.py` | Remove `truncate()` and all its call sites in `_build_form` (concept name, answer name, form-element-group name). Remove the `if a_uuid in seen: continue` dedup loop in the Coded-answers builder. Long names and duplicates are now resolved by `enrich_with_llm` upstream. *(Behaviour when the LLM is unavailable is deferred — see §11.)* |
| `pyproject.toml` | Add `rapidfuzz>=3.0` |

---

## 10. Roll-out Order

1. **Skeleton + auto-apply path** — `enrich_with_llm` node, `LLMHelper`, `Change` types, validation. No confirmation UX yet; only auto-apply changes flow through. The chat host becomes LLM-aware.
2. **Concerns 1, 4, 5** — option splitting, data-type inference, min/max prediction. These exercise the auto-apply path most. Validates the cost model on real orgs.
3. **Concerns 2, 3** — long-name shortening and duplicate disambiguation. Adds the always-confirm path; needs `Confirmer` complete.
4. **Concern 6** — user-driven add/remove. Adds `user_instructions` to the chat tool; tests round-tripping.

After step 1, the pipeline behaves identically to today on clean inputs (LLM skipped) and starts auto-fixing common issues on messy inputs. Each subsequent step expands coverage without breaking the prior behaviour.

---

## 11. Out of Scope

- Skip-logic / visit-schedule / validation rule generation (already deferred in `BUNDLE_GENERATOR_SDD.md`).
- Translating fields to other languages.
- Auto-suggesting field-grouping into sections (assumes the source sheet's `Page Name` column is the source of truth).
- Auto-creating concepts that aren't referenced from any form (registry stays form-driven).
- Cross-form refactors (e.g. *"merge Pregnancy Enrolment and Pregnancy Exit"*) — single-form scope only for v1.
- **Behaviour when LLM enrichment is unavailable** (no API key, rate-limited, or call fails) — deferred. After the changes in §9, a dirty input (long name, duplicate field) reaching `generators.py` without prior LLM enrichment will fail or produce a broken bundle. A future iteration will decide whether to hard-fail with a clear error, fall back to the previous deterministic truncation/dedup, or require the user to set `ANTHROPIC_API_KEY`.

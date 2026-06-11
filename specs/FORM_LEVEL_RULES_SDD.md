# Form-Level Rule Generation — Software Design Document

Extends the visit-schedule rule machinery (`specs/VISIT_SCHEDULE_RULE_SDD.md`) to three additional form-level rule kinds — **validation**, **edit-form**, **decision** — using the curated example corpora now in `requirements/rules_ai_automation.xlsx`.

---

## 1. Objective

Generate JS for three more rule fields on each form JSON:

| Rule kind | JSON field on form | Avni return shape |
|---|---|---|
| Validation | `validationRule` | `[ validationError, ... ]` (`imports.common.createValidationError(...)`) |
| Edit-form | `editFormRule` | `{ eligible: { value: boolean, message?: string } }` (preferred). Legacy `{ editable: boolean }` is accepted but not emitted. |
| Decision | `decisionRule` | `{ encounterDecisions, registrationDecisions, enrolmentDecisions }` (one or more) |

Inputs are the same two as visit-schedule:

1. A new `Form Rules` sheet in the scoping document with one column per rule kind (per discussion 2026-06-03).
2. A chat instruction routed through a single `set_form_rule(form_name, rule_kind, intent)` tool.

Output is JS dropped into the form JSON's matching field. Empty when generation can't ground every reference.

This SDD does **not** introduce new architecture — the visit-schedule pipeline node, generator, validator, knowledge base, parser, prompt scaffold, and chat-tool write path all already exist. What's new is per-kind metadata (prompt return rubric + return-shape validator) and three more example corpora.

---

## 2. Scope

### In scope

- Add `RuleKind.VALIDATION`, `RuleKind.EDIT_FORM`, `RuleKind.DECISION` to `src/models.py` (per the C5 / B2 trim, `RuleKind` currently has only `VISIT_SCHEDULE`).
- Per-kind entries in `_KIND_META` and `_RETURN_EXPRESSION_BY_KIND` in `prompts.py` (return-type description + return-expression hint).
- Per-kind **return-shape** AST checks in `validator.py` (the grounding checks for concepts / encounter types / coded-concept answers are kind-agnostic and already work).
- Parser column aliases for the three kinds in `parser.py` (already present in `_COLUMN_ALIASES_BY_FIELD` — wire-through only).
- Ingest the three curated tabs from `rules_ai_automation.xlsx`:
  - `Validation rule (curated)` → `resources/rules/examples/validationRule/*.md`
  - `Edit form rule (curated)` → `resources/rules/examples/editFormRule/*.md`
  - `Decision rule (curated)` → `resources/rules/examples/decisionRule/*.md`
- One generic chat tool `set_form_rule(form_name, rule_kind, intent)` replacing `set_visit_schedule_rule`.
- Pipeline node `generate_rules` already iterates every kind on `FormSpec.rule_intents`; just need the new kinds to flow.
- Generator writes the JS into the matching JSON field per `RuleKind.value`.

### Out of scope

- Other Avni rule kinds (`encounterEligibilityCheckRule`, `enrolmentEligibilityCheckRule`, `subjectSummaryRule`, `enrolmentSummaryRule`, form-element-level skip-logic rules). Same extension pattern — just no curated corpus for them yet.
- Running generated JS in a sandbox to verify behaviour. Same posture as the visit-schedule SDD §2: syntax + grounding only.
- Editing source `.xlsx` files.
- A separate vector store. See §3 — embeddings stay shared.
- New context for `params.user.*` (roles, user groups) beyond mentioning it in the edit-form prompt. The bundle parser doesn't surface roles today; for the MVP, role-name strings inside edit-form intents are treated as untyped strings and not validated against an allowlist.

### Precondition

- The visit-schedule rule machinery (`VISIT_SCHEDULE_RULE_SDD.md` + the post-refactor commits A2 / B1 / C / D) is in place and working end-to-end.
- The curated tabs in `rules_ai_automation.xlsx` have been normalised by the most recent `/curate-rules` workflow (no `//SAMPLE` markers, no `use strict';` typos).
- `requirements/rules_ai_automation.xlsx` is the single source for all four rule-kind corpora (`VS rule (curated)`, `Validation rule (curated)`, `Edit form rule (curated)`, `Decision rule (curated)`).

---

## 3. Embeddings — shared cache, kind-tagged retrieval

**Decision: keep one `.embeddings.json` cache; tag entries by rule kind; filter at retrieval time.**

### Why shared

1. **Helpers are 100% shared.** Every helper (`Individual.findLatestObservationFromPreviousEncounters`, `imports.moment.add`, `RuleCondition`, …) is callable from any rule kind. Splitting them per kind duplicates ~370 vectors × N kinds for zero benefit.

2. **Retrieval already filters by kind.** `KnowledgeBase.retrieve(spec)` scopes helpers via `spec.rule_kind.value in h.applies_to` and examples via `ex.rule_kind == spec.rule_kind.value`. Cosine similarity runs only over the kind-scoped subset. Sharing the *cache* does not pollute *retrieval results*.

3. **Cost is irrelevant.** ~370 helpers (~30 K tokens) + ~70 examples across all four kinds (~6 K tokens) = ~36 K tokens per full rebuild. At Voyage / OpenAI embedding prices that's < $0.001 per rebuild. The split-vs-share decision can't be justified on cost.

4. **One rebuild, one cache file, one in-memory `KnowledgeBase`.** Simpler operations, simpler debugging.

5. **Cross-kind retrieval is a feature, not a bug.** If a validation rule's "schedule date guard" idiom is best illustrated by a visit-schedule example, retrieval can find it — but the kind-filter on examples prevents this leak from happening by default. (We can promote a few cross-kind helpers explicitly via `applies_to: [...]`.)

### What changes vs. the current state

- `applies_to: list[str]` on `HelperEntry` carries every kind the helper is relevant to. Default is **all four kinds** (the bulk of avni-models methods apply to every rule kind). Per-kind override only where a helper is genuinely kind-specific (e.g. `VisitScheduleBuilder.add` is `["visitScheduleRule"]`).
- `rule_kind: str` on `ExampleEntry` continues to identify which kind an example belongs to. Examples are not cross-kind.
- The single `.embeddings.json` file holds vectors for all helpers + all examples across all four kinds.

### What is *not* changing

- No second cache file.
- No per-kind `KnowledgeBase` instance.
- No new retrieval entry point.

---

## 4. Architecture changes

### 4.1 Reuse

| Component | Reuse? | Notes |
|---|---|---|
| `RuleGenerator` | Yes | Kind-agnostic — picks prompt fragments from `_KIND_META[spec.rule_kind]`. |
| `KnowledgeBase` + `.embeddings.json` | Yes | Kind-tagged retrieval; see §3. |
| `validator.check` symbol grounding | Yes | Concept / encounter / answer grounding is kind-agnostic. |
| `validator.validate_and_decide` gate | Yes | Single shared write gate. |
| Pipeline node `generate_rules` | Yes | Already iterates `FormSpec.rule_intents` per kind. |
| `parser.py` `_COLUMN_ALIASES_BY_FIELD` | Mostly | `validationRule` and `decisionRule` aliases present; add `editFormRule`. |
| `bundle_editor.write_form_rule` | Yes | Writes JS into `form_json[rule_field]`. |
| Chat tool write path | Yes | Same `_open_bundle` / re-zip flow. |
| `_render_example` (kb_ingest) + the markdown frontmatter shape | Yes | Same per-kind subdirectory; `--rule-kind` arg already supports any kind value. |

### 4.2 Additions

```
src/models.py
  └─ RuleKind enum: + VALIDATION, EDIT_FORM, DECISION
src/domain/rules/prompts.py
  └─ _KIND_META: + 3 entries with per-kind return_type_description
  └─ _RETURN_EXPRESSION_BY_KIND: + 3 entries
src/domain/rules/validator.py
  └─ per-kind return-shape AST check (post-grounding)
src/domain/rules/parser.py
  └─ _COLUMN_ALIASES_BY_FIELD: + "edit form rule", "edit form" aliases
                                   for editFormRule
src/chat/tools.py
  └─ set_form_rule(form_name, rule_kind, intent) — generic; replaces
     set_visit_schedule_rule outright. Agent prompt updated to reach for
     set_form_rule with the right rule_kind.

resources/rules/examples/
  ├─ visitScheduleRule/*.md        (existing)
  ├─ validationRule/*.md           (new — 24 files)
  ├─ editFormRule/*.md             (new — 8 files)
  └─ decisionRule/*.md             (new — 14 files)
```

---

## 5. Per-kind metadata

### 5.1 `_KIND_META` entries (`prompts.py`)

```python
_KIND_META[RuleKind.VALIDATION] = RuleKindMeta(
    return_type_description=(
        "an array of validation error objects produced by "
        "`imports.common.createValidationError(messageKey)` — single arg. "
        "`messageKey` is either a literal error message (prototype use) or "
        "an i18n key registered in avni-webapp's translations. Return an "
        "empty array when nothing is wrong."
    ),
)
_KIND_META[RuleKind.EDIT_FORM] = RuleKindMeta(
    return_type_description=(
        "an object `{ eligible: { value: boolean, message?: string } }`. "
        "`eligible.value: false` blocks editing; `message` is the reason "
        "shown to the user. A legacy `{ editable: boolean }` form is still "
        "accepted by Avni but should NOT be emitted — always produce the "
        "nested `eligible` object."
    ),
)
_KIND_META[RuleKind.DECISION] = RuleKindMeta(
    return_type_description=(
        "an object `{ encounterDecisions, registrationDecisions, "
        "enrolmentDecisions }`, each a (possibly empty) array of "
        "`{ name, value }` decision entries. Most rules only touch one of "
        "the three arrays — leave the others empty."
    ),
)
```

### 5.2 `_RETURN_EXPRESSION_BY_KIND` entries

```python
_RETURN_EXPRESSION_BY_KIND[RuleKind.VALIDATION]  = "validationResults"
_RETURN_EXPRESSION_BY_KIND[RuleKind.EDIT_FORM]   = "{ eligible: { value: true } }"
_RETURN_EXPRESSION_BY_KIND[RuleKind.DECISION]    = "decisions"
```

These show up in the system-prompt skeleton (`{return_expression}`) as the canonical placeholder body. The real body is whatever the LLM generates between the `// ... rule body ...` placeholder and the `return`.

### 5.3 Per-kind return-shape validator checks

After the existing grounding checks (concept / encounter / coded-answer) pass, `validator.check` runs one kind-specific AST check:

| Kind | Check |
|---|---|
| `visitScheduleRule` | (unchanged) — relies on system-prompt rubric. |
| `validationRule` | Top-level `return` resolves to an array expression (literal, identifier, or call result). Every reachable `imports.common.createValidationError(...)` call has a string-literal first argument. |
| `editFormRule` | Top-level `return` resolves to an `ObjectExpression` with an `eligible` property whose value is itself an `ObjectExpression` carrying a `value` boolean (preferred). A legacy `Literal` boolean directly under `eligible`, or an `ObjectExpression` keyed by `editable`, is accepted with a soft warning ("legacy edit-form return shape — prefer `{ eligible: { value, message } }`"). |
| `decisionRule` | Top-level `return` resolves to an `ObjectExpression` (the decisions container) or an `Identifier` (a name that's been mutated to hold one). An array literal at top-level is rejected — Avni expects the outer container, not a flat list. The rule body must assign or initialise at least one of `encounterDecisions / registrationDecisions / enrolmentDecisions` on the returned container. |

Soft failures only — these surface as warnings, not as a write-block. The grounding checks remain the only hard gate.

---

## 6. Parser additions

`_COLUMN_ALIASES_BY_FIELD` in `src/domain/rules/parser.py` already has `validationRule` and `decisionRule`. Add:

```python
"editFormRule": ("edit form rule", "edit form"),
```

The `Form Rules` sheet convention (one row per form, one column per rule field; column headers case-insensitive) is unchanged.

---

## 7. Knowledge base — example ingest

The current ingester (`kb_cli.py ingest-examples`) takes `--xlsx`, `--tab`, `--rule-kind`. Run it four times:

```
avni-rules-kb examples --rule-kind visitScheduleRule    # existing
avni-rules-kb examples --xlsx requirements/rules_ai_automation.xlsx \
                       --tab "Validation rule (curated)" \
                       --rule-kind validationRule
avni-rules-kb examples --xlsx requirements/rules_ai_automation.xlsx \
                       --tab "Edit form rule (curated)" \
                       --rule-kind editFormRule
avni-rules-kb examples --xlsx requirements/rules_ai_automation.xlsx \
                       --tab "Decision rule (curated)" \
                       --rule-kind decisionRule
```

After the four runs, a single `rebuild` repopulates the shared `.embeddings.json`.

### Optional convenience — `examples-all`

A small porcelain command:

```python
def cmd_examples_all() -> int:
    """Ingest curated tabs for every wired RuleKind and rebuild once."""
```

Reads a static manifest:

```python
_INGEST_MANIFEST: dict[RuleKind, tuple[Path, str]] = {
    RuleKind.VISIT_SCHEDULE: (_RAA_XLSX, "VS rule (curated)"),
    RuleKind.VALIDATION:     (_RAA_XLSX, "Validation rule (curated)"),
    RuleKind.EDIT_FORM:      (_RAA_XLSX, "Edit form rule (curated)"),
    RuleKind.DECISION:       (_RAA_XLSX, "Decision rule (curated)"),
}
```

Optional — convenient for CI / first-time setup. Not strictly required if humans run the four-step sequence by hand.

---

## 8. Chat tool

Replace `set_visit_schedule_rule(form_name, intent)` with a single generic tool:

```python
@tool
def set_form_rule(
    form_name: str,
    rule_kind: Literal[
        "visitScheduleRule",
        "validationRule",
        "editFormRule",
        "decisionRule",
    ],
    intent: str,
) -> dict:
    """Generate JS for a form-level rule and write it into the bundle.

    `rule_kind` picks which Avni rule field on the form JSON is updated.
    `intent` is natural-language — what the rule should DO, not how.
    Returns the standard {status, ...} envelope on success / rejection.
    """
```

Internally it calls the existing `RuleGenerator` + `validate_and_decide` + `write_form_rule` path, parameterised by the new `rule_kind`.

The agent prompt (`src/chat/prompts.py`) gains a one-paragraph rubric explaining when each kind applies; the agent picks the kind from the user's instruction. The pipeline node `generate_rules` already loops over `FormSpec.rule_intents` regardless of kind.

---

## 9. Inputs

### 9.1 Scoping doc — `Form Rules` sheet

Same shape as in the visit-schedule SDD §9.1 / earlier 2026-06-03 discussion. One row per form, with columns:

| Form Name | Visit Schedule Rule | Decision Rule | Validation Rule | Edit Form Rule |

Per-cell content is natural-language intent ("block saving when weight < 24 or > 90 kg"). Blank = no rule. Parser populates `FormSpec.rule_intents[kind_value] = cell_text`.

### 9.2 Chat instruction

User says: "validate that age must be between 18 and 60 on the Adult Registration form". The agent calls `set_form_rule(form_name="Adult Registration", rule_kind="validationRule", intent="block saving when age is < 18 or > 60")`.

---

## 10. Data flow

```
parse_documents
  └─ Form Rules sheet → FormSpec.rule_intents[kind_value]
generate_entities → generate_forms → generate_form_mappings
generate_rules (existing node)
  └─ for each form with rule_intents:
       for each (kind_value, intent) pair:
         spec = RuleSpec(rule_kind=RuleKind(kind_value), intent=..., ...)
         result = RuleGenerator().generate(spec)            # kind-aware prompt
         ok, warnings = validate_and_decide(result, spec)   # kind-aware checks
         if ok:
           form_json[kind_value] = result.js
package_zip (existing)
```

The pipeline node already supports this. The only reason it doesn't generate validation / edit-form / decision rules today is that the corresponding `RuleKind` enum values don't exist yet.

---

## 11. Files to create / change

| File | Status | Description |
|---|---|---|
| `src/models.py` | edit | Re-introduce `VALIDATION`, `EDIT_FORM`, `DECISION` in `RuleKind`. |
| `src/domain/rules/prompts.py` | edit | Add 3 entries to `_KIND_META` and `_RETURN_EXPRESSION_BY_KIND`. |
| `src/domain/rules/validator.py` | edit | New `_check_return_shape(tree, kind)` dispatch; called from `check()` after grounding. Soft warnings only. |
| `src/domain/rules/parser.py` | edit | Add `editFormRule` aliases. |
| `src/domain/rules/kb_cli.py` | edit | Optional `examples-all` porcelain command + `_INGEST_MANIFEST`. |
| `src/chat/tools.py` | edit | Replace `set_visit_schedule_rule` with `set_form_rule(form_name, rule_kind, intent)`. Update the agent prompt to reach for the new tool with the right `rule_kind`. |
| `src/chat/prompts.py` | edit | Agent prompt picks up a short rubric on when each rule kind applies. |
| `resources/rules/examples/validationRule/*.md` | generated | 24 files, one per curated row. |
| `resources/rules/examples/editFormRule/*.md` | generated | 8 files. |
| `resources/rules/examples/decisionRule/*.md` | generated | 14 files. |
| `resources/rules/.embeddings.json` | regenerated | Single cache, all kinds. |

No new runtime dependencies. No new modules.

---

## 12. Verification

1. **Unit-level**: per-kind `_KIND_META` lookup returns the expected return-type description; `validator._check_return_shape` accepts the canonical return shapes for each kind on hand-written examples and rejects obvious mismatches.
2. **Corpus round-trip**: `examples-all` ingests four corpora; `KnowledgeBase()` loads them; `retrieve(spec)` with `rule_kind=VALIDATION` returns only validation examples (and helpers tagged for validation).
3. **End-to-end (offline)**: run `generate_bundles.py --org <test>` with a stub `Form Rules` sheet populated with one intent per kind; expect four non-empty rule fields on the target form's JSON.
4. **Chat round-trip**: `set_form_rule("Adult Registration", "validationRule", "block save when age < 18")` writes a parseable, grounding-passing `validationRule` into the form JSON.

---

## 13. Out of scope (recap)

- Other rule kinds beyond the four wired here.
- A separate vector store / embedding cache per kind.
- Runtime-equivalence testing of generated JS.
- Surfacing user roles / user groups from the bundle as grounded symbols for edit-form rule generation (treated as untyped strings inside the intent).
- Re-curation of the `(curated)` tabs — that's the `/curate-rules` workflow's job; this SDD only consumes the curated output.
- Editing source xlsx tabs.

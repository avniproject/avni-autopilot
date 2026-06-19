# Field-Rule Generation — Software Design Document

Extends the form-level rule machinery (`specs/FORM_LEVEL_RULES_SDD.md`) to
one more Avni rule attachment point — `formElement.rule` (field rule) —
using the curated `Field rules (curated)` corpus in
`resources/rules/rules_ai_automation.xlsx` (15 rows).

Field rules can combine multiple behaviours in a single JS body —
visibility, auto-fill / auto-compute value, validation, and coded-answer
filtering — because Avni gives every field exactly **one** `rule`
slot whose return value carries all of `visibility`, `value`,
`validationErrors`, `answersToSkip`, and `answersToShow`. The curated
corpus reflects this with a `kind` column on each curated row tagging
the behaviour it emphasises
(`visibility | value | validation | answerFilter`).

The SDD covers **all five behaviour columns** on the modelling doc
in a single phase — visibility, default value, computed value,
validation, and answer filter — because the curated corpus already
contains representatives for each (clusters 1–15 of
`Field rules (curated)`) and the architecture (one `RuleKind`, one
parser, one generator, one writer) does not change between behaviours.
**Page rules (`formElementGroup.rule`) are out of scope for this SDD**
— deferred to a follow-up that reuses the same architecture for the
page attachment point (§15).

---

## 1. Objective

Generate JS for two more `rule` fields on the form JSON:

| Rule kind | JSON write location | Avni return shape |
|---|---|---|
| Field rule | `form.formElementGroups[i].formElements[j].rule` | a single `FormElementStatus(uuid, visibility, value, answersToSkip, validationErrors, answersToShow)` — any subset of behaviours populated |

Inputs are read from the existing modelling doc:

1. The per-field **"When to show"** column already harvested by
   `src/domain/parser.py:988` (aliases: `when to show`, `skip logic`,
   `condition`, `visibility`). Currently stored as a stub
   `SkipLogicSpec` and never consumed downstream. Re-plumb it as a
   per-field rule intent.
2. Four additional per-field columns covering the remaining
   behaviours (default value, validation, answer filter, and the
   negative-form "when not to show"). See §6.1 for the alias table
   and the connector phrases.

Output is JS dropped into the matching `rule` field on the form JSON.
Empty when the generator can't ground every reference (same gate as
the form-level rules — see `specs/VISIT_SCHEDULE_RULE_SDD.md` §7.3).

This SDD does **not** introduce a new generation pipeline or new
runtime. Like `FORM_LEVEL_RULES_SDD.md`, it adds per-kind metadata, a
return-shape validator check, parser plumbing, and example ingest —
everything else is reuse.

---

## 2. Scope

### In scope

- Add `RuleKind.FORM_ELEMENT = "formElementRule"` to
  `src/domain/rules/rule_spec.py`.
- One entry in `_KIND_META` and `_RETURN_EXPRESSION_BY_KIND` in
  `src/domain/rules/prompts.py` for `FORM_ELEMENT`.
- Return-shape AST check for `FORM_ELEMENT` in
  `src/domain/rules/validator.py` (single `FormElementStatus`).
- Parser changes (§6):
  - Promote the existing per-field "When to show" cell from
    `SkipLogicSpec` stub to a real rule intent on the field.
  - Read the additional behaviour columns (value, validation,
    answer-filter) into the same `rule_intent`.
- `FieldSpec` gains `rule_intent: str | None`.
  `FormSpec.rule_intents` keeps its existing dict shape for form-level
  rules and is unaffected.
- **Per-field generation.** For `FORM_ELEMENT` the pipeline calls
  `RuleGenerator().generate(spec)` once per field with a non-empty
  intent — reusing the existing single-rule generator path verbatim.
  Repeating-QG awareness is delegated to retrieval: cluster #15 of
  `Field rules (curated)` (`questionGroupValueInEncounter`) sits in
  the corpus and surfaces via cosine similarity when an intent
  matches its prompt ("…inside a repeating section…"). No spec-level
  flag, no `_KIND_META` extension, no per-spec context injection.
  Batch generation can be added later as an optimisation if
  generation time becomes a bottleneck — the per-field path doesn't
  foreclose it.
- Generator writes into `form.formElementGroups[i].formElements[j].rule`
  (existing `generators.py:290` site) by populating the `rule` key
  when a generated JS body is produced.
- Pipeline node `generate_rules` extended to iterate per-field
  intents in addition to the existing per-form intents.
- Knowledge base example ingest (§7):
  - `formElementRule` corpus from **all 15 rows** of
    `Field rules (curated)`. The `kind` column rides along on each
    ingested example for retrieval-time boost (§7).
- A `kind` column is already present on `Field rules (curated)`
  (added by `scripts/add_kind_column_and_qg_cluster.py`); it tags
  each row with `visibility | value | validation | answerFilter`.
- New chat tool `set_form_element_rule(form_name, page_name,
  field_name, intent)` for ad-hoc field-rule edits from the agent.
  `page_name` is required so the tool can locate the exact field
  inside the form's structure.

### Out of scope
- **Page rules (`formElementGroup.rule`).** Deferred to a follow-up
  SDD. The architecture in this SDD generalises naturally — add
  `RuleKind.FORM_ELEMENT_GROUP`, one `_KIND_META` entry, an
  array-return validator check, and a per-page parser pass — but
  shipping field rules first keeps the surface area smaller. The
  curated `Page rules (curated)` tab and its 8 rows stay in the
  workbook untouched.
- **`.mandatory()` chain** (doc-documented capability, 0 uses in the
  18 k-row Field rules corpus). Not curated, not generated.
- **`resetValueIfNull`** (1 use in corpus). Same reasoning.
- **Role / user-group conditional visibility** (`params.user`,
  `myUserGroups`). The corpus does not exercise it; the bundle parser
  does not surface roles. Treated as out of scope until role symbols
  are first-class in `EntitySpec`.
- **Runtime equivalence testing** of generated JS. Same posture as
  the existing rule SDDs — grounding + return-shape only.
- **Nested question groups (QG inside QG).** Only first-level
  repeating groups are recognised. A repeating group whose parent is
  itself a (repeating or non-repeating) question group is treated as
  a flat repeating group; the `params.questionGroupIndex` is the
  innermost group's index. Multi-level threading is deferred.

### Precondition

- `FORM_LEVEL_RULES_SDD.md` is implemented end-to-end; the
  `RuleGenerator` + `KnowledgeBase` + `validator` + `bundle_editor`
  paths are all kind-parametric today.
- `Field rules (curated)` exists in
  `resources/rules/rules_ai_automation.xlsx` with 15 rows (one per
  semantic cluster), an intent prompt per row, and a `kind` column.
- The parser already reads the per-field "When to show" cell into
  `FieldSpec.skipLogic`. The string is preserved verbatim today.

---

## 3. Why one `RuleKind` — not per behaviour

A field rule can simultaneously control visibility, set a default
value, push validation errors, and filter coded answers. The Avni
runtime exposes all four as fields on the single `FormElementStatus`
object the rule returns; there is no way to attach a "visibility
rule" and a "validation rule" separately to the same field. Three
consequences flow from that:

1. **No `RuleKind` per behaviour.** A `FIELD_VISIBILITY` /
   `FIELD_VALUE` / `FIELD_VALIDATION` / `FIELD_ANSWER_FILTER` split
   would force the pipeline to generate four bodies per multi-behaviour
   field and then merge them by AST — significantly harder than
   generating one combined body. We use a single
   `RuleKind.FORM_ELEMENT` and let the LLM compose behaviours inside
   the body.
2. **One generation call per field.** The parser concatenates the
   modelling-doc columns into one structured `rule_intent` string
   ("Show this field only when X. When visible, pre-fill with Y.
   Block save when Z."). The pipeline calls the existing
   single-rule `RuleGenerator().generate(spec)` once per field with a
   non-empty intent. Reusing the existing single-rule generator
   verbatim is the cheapest path to first ship; batch-per-form
   generation is a viable later optimisation if generation time
   becomes a bottleneck.
3. **Retrieval still segregates by behaviour.** The `kind` column on
   `Field rules (curated)` rows tags each example. The intent text
   alone steers retrieval, but the tag is available as a metadata
   boost or filter when an intent obviously targets one behaviour
   ("hide this field unless …" → boost `kind="visibility"`
   examples).

## 4. Architecture changes

### 4.1 Reuse (unchanged)

| Component | Reuse? | Notes |
|---|---|---|
| `RuleGenerator` | Yes | Picks prompt fragments from `_KIND_META[spec.rule_kind]`. |
| `KnowledgeBase` + `.embeddings.json` | Yes | Shared cache, kind-tagged retrieval (`FORM_LEVEL_RULES_SDD.md` §3). |
| Grounding checks (concept / encounter / coded-answer) | Yes | Kind-agnostic. |
| `validate_and_decide` write gate | Yes | Same shared gate. |
| Pipeline node `generate_rules` | Mostly | Extend the loop body to also iterate per-field intents (§8). |
| `bundle_editor.write_form_rule` | Mostly | Extend to also accept a `(page_name, field_name)` target so the same writer reaches the right `formElement.rule` slot (§9). |
| Chat tool write path | Yes | Same `_open_bundle` / re-zip flow. |
| `kb_ingest` (per-kind subdirectory layout, frontmatter shape, `--rule-kind` arg) | Yes | One more `--rule-kind` value. |

### 4.2 Additions

```
src/domain/rules/rule_spec.py
  └─ RuleKind enum: + FORM_ELEMENT
src/domain/rules/prompts.py
  └─ _KIND_META: + 1 entry (all-behaviours return-type description)
  └─ _RETURN_EXPRESSION_BY_KIND: + 1 entry
src/domain/rules/validator.py
  └─ return-shape AST check for FORM_ELEMENT
src/domain/parser.py
  └─ promote per-field cells (visibility / value / validation /
     answer-filter columns) into a structured FieldSpec.rule_intent
     (§6.1)
src/models.py
  └─ FieldSpec.rule_intent: str | None
src/domain/generators.py
  └─ form_elements[j]["rule"] populated from rule_intent → generated JS
src/pipeline/nodes.py (generate_rules)
  └─ iterate per-field intents in addition to per-form
src/chat/tools.py
  └─ set_form_element_rule(form_name, page_name, field_name, intent)
src/chat/prompts.py
  └─ rubric explaining when to call set_form_element_rule

resources/rules/examples/
  └─ formElementRule/*.md            (new — 15 files, one per
                                       curated cluster)

resources/rules/rules_ai_automation.xlsx — already edited:
  └─ `kind` column on Field rules (curated) (added by
     scripts/add_kind_column_and_qg_cluster.py)
```

### 4.3 Return-shape validator dispatch

After the existing concept / encounter / answer grounding checks,
`validator.check` runs the kind-specific AST check:

| Kind | Check |
|---|---|
| `formElementRule` | Top-level `return` resolves to a `NewExpression` whose callee is `imports.rulesConfig.FormElementStatus`, OR to a call to `statusBuilder.build()` on a `FormElementStatusBuilder` instance constructed in the same scope. Anything else is a soft warning. |

Soft failures only — grounding checks remain the hard gate, matching
the posture in `FORM_LEVEL_RULES_SDD.md` §5.3.

---

## 5. Per-kind metadata

### 5.1 `_KIND_META` entries (`src/domain/rules/prompts.py`)

```python
_KIND_META[RuleKind.FORM_ELEMENT] = RuleKindMeta(
    return_type_description=(
        "a single `FormElementStatus` instance controlling this field's "
        "visibility, default / computed value, validation errors, and "
        "coded-answer filtering. Any subset of those behaviours may be "
        "populated in the same body — the intent text dictates which. "
        "Construct via `new imports.rulesConfig.FormElementStatus(uuid, "
        "visibility, value, answersToSkip, validationErrors, "
        "answersToShow, resetValueIfNull)` (last arg defaults to false) "
        "or via `statusBuilder.show().when.<…>.build()` "
        "(StatusBuilder also exposes `.value(...)`, `.validationError(...)`, "
        "`.skipAnswers(...)`, `.showAnswers(...)` for composition). "
        "The first arg is always `params.formElement.uuid`."
    ),
)
```

### 5.2 `_RETURN_EXPRESSION_BY_KIND` entry

```python
_RETURN_EXPRESSION_BY_KIND[RuleKind.FORM_ELEMENT] = "statusBuilder.build()"
```

These power the `{return_expression}` placeholder in the system-prompt
skeleton; the LLM substitutes a real body in between.

---

## 6. Parser changes

### 6.1 Per-field intent — read every behaviour column

`src/domain/parser.py:988` already detects the `When to show` column
under aliases `when to show | skip logic | condition | visibility`.
Today the cell text is wrapped into a stub `SkipLogicSpec` and never
consumed. Change:

- Add `FieldSpec.rule_intent: str | None`.
- The parser reads **all five behaviour columns** (aliases below) and
  composes a single structured `rule_intent` per field. Cells are
  joined in a fixed order with the listed connector phrases so the
  LLM sees a consistent shape.
- `field.skipLogic` is set to `None` once a generated rule lands —
  the stub was a placeholder; keeping it would double-emit
  visibility logic.
- The auto-detected skip-logic patterns (`_detect_skip_logic_patterns`,
  parser.py:835 — Others / Sub-type / Yes/No detail) populate the
  visibility fragment of `rule_intent` with a normalised
  natural-language sentence ("show only when 'X' is Others") instead
  of a structured `SkipLogicSpec`.

| Order | Column aliases | Behaviour fragment | Connector inserted before this fragment | Kind tag |
|---|---|---|---|---|
| 1 | `when to show`, `visibility`, `skip logic`, `condition` | visibility (positive — show when …) | (start of intent) | `visibility` |
| 2 | `when not to show`, `hide when` | visibility (negative — hide when …) | `Hide this field when: ` | `visibility` |
| 3 | `default_value`, `default value`, `pre-fill` | value — covers both straight pre-fill and arithmetic compute; the intent text dictates which | `Pre-fill / compute the value as: ` | `value` |
| 4 | `validation`, `validate when`, `block save when` | validation | `Block save when: ` | `validation` |
| 5 | `option condition`, `show options when`, `filter options` | answer filter | `Restrict the answer options as follows: ` | `answerFilter` |

One column per kind, with two columns for visibility (positive vs
negative phrasing). The `default_value` column carries both pre-fill
("copy 'Mobile number' from registration") and computation
("sum of male + female members") — the LLM disambiguates from the
intent text, with retrieval surfacing cluster 10 vs cluster 11
accordingly.

Example — a single field row with three populated cells produces a
single intent:

```
Show only when 'Outcome of pregnancy' is Abortion.
Pre-fill / compute the value as: the value of 'Last menstrual period'.
Block save when: date is after today.
```

The generator sees this whole string and produces one rule body
returning a `FormElementStatus` whose `visibility`, `value`, and
`validationErrors` are all populated. Retrieval benefits from the
curated rows because the intent contains keywords ("Pre-fill",
"Block save") that the kind-tagged examples emphasise; the kind tag
is consulted only as a tiebreaker.

### 6.2 `kind` column on the curated tab

The `kind` column on `Field rules (curated)` carries one of
`{"visibility", "value", "validation", "answerFilter"}` per row:

| Row | Cluster | Kind |
|---|---|---|
| 2–10 | 1–9 | visibility |
| 11 | 10 (pre-fill) | value |
| 12 | 11 (compute) | value |
| 13 | 12 (date validation) | validation |
| 14 | 13 (cross-field validation) | validation |
| 15 | 14 (answer filter) | answerFilter |
| 16 | 15 (repeating-group visibility — `questionGroupValueInEncounter`) | visibility |

Already populated by `scripts/add_kind_column_and_qg_cluster.py`
(re-runnable idempotently). The /curate-rules workflow's writer
(`scripts/write_curated_*.py`) is updated to emit this column on
subsequent re-runs, so the tag is preserved across re-curations.

---

## 7. Knowledge base — example ingest

One more invocation of the existing `examples` ingest:

```
avni-rules-kb examples \
  --xlsx resources/rules/rules_ai_automation.xlsx \
  --tab "Field rules (curated)" \
  --rule-kind formElementRule
                                 # all 15 rows; kind tag rides along
                                 # on each example for retrieval boost
```

The ingester reads the existing `org_name`, `form_name`,
`field_name`, `rule`, `prompt` columns plus the `kind` column, and
stores `kind` as metadata on each ingested `ExampleEntry`. At
retrieval time the kind tag is available as a small boost when the
intent text obviously targets one behaviour ("hide", "show … only"
→ boost `kind="visibility"`; "pre-fill", "compute" →
`kind="value"`; "block save" → `kind="validation"`; "only allow",
"restrict options" → `kind="answerFilter"`). Boost shape: add a
small constant (say 0.05) to cosine similarity when the kind matches
the dominant verb of the intent; do not gate retrieval on the tag.

A single `rebuild` thereafter repopulates the shared
`.embeddings.json`.

Update `_INGEST_MANIFEST` in `kb_cli.py` (the `examples-all` porcelain
from `FORM_LEVEL_RULES_SDD.md` §7) to include `formElementRule`.

---

## 8. Pipeline data flow

```
parse_documents
  └─ Field sheet → FormSpec.sections[i].fields[j].rule_intent
                    (composed from visibility / value / validation /
                     answer-filter columns)
  └─ Form Rules sheet → FormSpec.rule_intents[kind_value]  (unchanged)
generate_entities → generate_forms → generate_form_mappings
generate_rules (extended)
  └─ for each form:
       # existing per-form pass (unchanged) — one call per kind
       for each (kind_value, intent) in form.rule_intents:
         spec   = RuleSpec(rule_kind=RuleKind(kind_value), intent=...)
         result = RuleGenerator().generate(spec)
         ok, _  = validate_and_decide(result, spec)
         if ok:
           form_json[kind_value] = result.js

       # new per-field pass — one LLM call per field with intent
       for each field in section.fields with rule_intent:
         spec   = RuleSpec(rule_kind=FORM_ELEMENT,
                           intent=field.rule_intent,
                           form_name=..., form_type=..., …)
         result = RuleGenerator().generate(spec)
         ok, _  = validate_and_decide(result, spec)
         if ok:
           form_json.formElementGroups[i].formElements[j].rule = result.js
package_zip (existing)
```

Both passes use the existing single-rule
`RuleGenerator.generate(spec)` and `validate_and_decide(result, spec)`
— no new generator method, no new spec class. The form-level pass
remains one call per kind as today.

**Failure granularity is per body.** A bad field-rule body leaves
that field's `rule` empty; sibling fields and other forms are
unaffected. Same posture as the form-level rule SDDs — empty `rule`
on rejection is the gate.

**Repeating question groups.** Cluster #15 of `Field rules (curated)`
shows the `questionGroupValueInEncounter` body pattern. Retrieval
surfaces it via cosine similarity on the intent text (the cluster's
prompt mentions "inside a repeating section"). No spec-level flag,
no `_KIND_META` extension. If the modelling-doc intent for a
repeating-row field doesn't naturally trip the cluster's embedding,
that's a corpus-prompt problem and the fix is to refine cluster
#15's prompt — not to add architecture.

**Cost / time envelope.** A typical bundle (10 forms × ~10 fields
with intents) ≈ 100 LLM calls × ~10 s each = ~15–20 min and ~$5 in
inference. Acceptable for the rare "build a new bundle" flow. If
this becomes a bottleneck later, batch-per-form generation drops
in behind a flag without touching the parser, validator, writer,
or chat tool — the call surface is fully encapsulated in
`generate_rules`.

---

## 9. Bundle writer changes

`generators.py:290` currently emits an empty `"rule": ""` on every
form element. The generator now overwrites it with the JS body when
a matching `rule_intent` was provided and generation passed
validation. Pseudocode:

```python
# field element
form_elements.append({
    ...,
    "rule": rule_intents.get(("field", section_name, concept_name), ""),
})
```

`form_element_group["rule"]` is left untouched (the existing
generator output at lines 307–313 has no `rule` key; page rules are
out of scope).

`bundle_editor.write_form_rule` gets an optional
`(page_name, field_name | None)` parameter so chat-driven edits write
into the right `rule` slot:

```python
def write_form_rule(
    bundle_path: Path,
    form_name: str,
    rule_kind: RuleKind,
    js: str,
    *,
    page_name: str | None = None,
    field_name: str | None = None,
) -> None:
    ...
```

For `FORM_ELEMENT` the writer locates the target by
`(page_name, field_name)`; for other kinds the writer is unchanged.

---

## 10. Chat tool

```python
@tool
def set_form_element_rule(
    form_name: str,
    page_name: str,
    field_name: str,
    intent: str,
) -> dict:
    """Generate JS for a field-level rule and write it into the bundle.

    Targets `form.formElementGroups[i].formElements[j].rule`. The
    behaviour mix (visibility / value / validation / answer-filter)
    is dictated by the intent text — "show this only when consent
    is Yes", "pre-fill with mobile number", "block save when date
    is in the past", "only allow C-section if place is hospital".
    The validator gates writes the same way it does for form-level
    rules.
    """
```

Agent prompt rubric (in `src/chat/prompts.py`) gains a paragraph:

> When the user asks to show/hide / pre-fill / validate / filter
> options for a specific field — "only show 'Reason for refusal'
> when consent is No", "pre-fill mobile number from registration",
> "block save when date is in the past" — call
> `set_form_element_rule` with the resolved page and field names.

The agent must call `list_bundle_fields` first to resolve exact
`page_name` / `field_name` strings, matching the existing convention.

---

## 11. Inputs (recap)

### 11.1 Modelling doc — per-field columns

Five column groups on the field sheet, all natural-language cells
(no formula syntax). Per §6.1:

| Column | Kind tag | Example cell |
|---|---|---|
| `when to show` | visibility | "consent is Yes" |
| `when not to show` | visibility | "outcome is Abortion" |
| `default_value` | value | "copy from 'Mobile number' in registration" |
| `validation` | validation | "must be between 18 and 60" |
| `option condition` | answerFilter | "only show 'C-section', 'Assisted' when place of delivery is hospital" |

Blank cells contribute nothing to the intent. A field with all
cells blank gets no `rule_intent` and no JS is generated.

### 11.2 Chat instruction

User types: "in Pregnancy ANC, on the Risk Factors page, only show
'Reason for refusal' when consent is No". The agent calls
`set_form_element_rule(form_name="Pregnancy ANC",
page_name="Risk Factors", field_name="Reason for refusal",
intent="show only when 'Consent given' is No")`.

---

## 12. Files to create / change

| File | Status | Description |
|---|---|---|
| `src/domain/rules/rule_spec.py` | edit | Add `FORM_ELEMENT` to `RuleKind`. No new spec class — reuses `RuleSpec`. |
| `src/domain/rules/prompts.py` | edit | 1 entry in `_KIND_META`, 1 in `_RETURN_EXPRESSION_BY_KIND`. |
| `src/domain/rules/validator.py` | edit | Return-shape AST check for `formElementRule`. |
| `src/domain/parser.py` | edit | Read every behaviour column (visibility / value / validation / answer-filter) into a structured `rule_intent`. |
| `src/models.py` | edit | `FieldSpec.rule_intent`. |
| `src/domain/generators.py` | edit | Populate `formElements[j]["rule"]` from generated JS. |
| `src/pipeline/nodes.py` | edit | Extend `generate_rules` with a per-field pass that reuses the existing single-rule `generate(spec)` interface. |
| `src/domain/bundle_editor.py` | edit | `write_form_rule` takes `page_name`, `field_name` to locate the right `formElement.rule` slot. |
| `src/chat/tools.py` | edit | Add `set_form_element_rule`. |
| `src/chat/prompts.py` | edit | Rubric for the new tool. |
| `src/domain/rules/kb_cli.py` | edit | Read the `kind` column at ingest; store as `ExampleEntry` metadata; retrieval uses it as a soft boost. Update `_INGEST_MANIFEST` for `formElementRule`. |
| `resources/rules/rules_ai_automation.xlsx` | already edited | `kind` column on `Field rules (curated)`; cluster #15 appended (done by `scripts/add_kind_column_and_qg_cluster.py`). |
| `resources/rules/examples/formElementRule/*.md` | generated | 15 files, one per Field-rules curated row. |
| `resources/rules/.embeddings.json` | regenerated | Shared cache. |

No new runtime dependencies. No new modules.

---

## 13. Verification

1. **Unit-level.** `_KIND_META[FORM_ELEMENT]` lookup returns the
   expected return-type description.
   `validator._check_return_shape` accepts hand-written canonical
   bodies and emits soft warnings on obvious shape mismatches
   (missing `FormElementStatus` constructor / `statusBuilder.build()`).
2. **Corpus round-trip.** `examples` ingest on
   `Field rules (curated)` ingests all 15 rows with their `kind`
   metadata. `KnowledgeBase().retrieve(spec)` with
   `rule_kind=FORM_ELEMENT` returns examples filtered to that kind;
   the `kind` boost applies to intents whose dominant verb matches.
3. **End-to-end (offline).** Run `generate_bundles.py --org <test>`
   with a stub modelling doc carrying one intent in each of the five
   field-rule columns on distinct fields. Expect non-empty `rule`
   strings on the five matching `formElement` JSON nodes; empty
   everywhere else.
4. **Chat round-trip.** `set_form_element_rule("Pregnancy ANC",
   "Risk Factors", "Reason for refusal",
   "show only when 'Consent given' is No")` writes a parseable,
   grounding-passing `formElementRule` into the right node.
5. **Byte-identical no-op.** With no `rule_intent` populated anywhere,
   the generated bundle JSON is byte-identical to today's output —
   the `form_element["rule"]` key was already emitted as `""`, so
   no structural change is required.

---

## 14. Out of scope (recap)

- **Page rules (`formElementGroup.rule`).** Deferred. The curated
  `Page rules (curated)` tab and `scripts/write_curated_pages.py`
  stay in the workbook untouched; ingestion and pipeline pass for
  `FORM_ELEMENT_GROUP` are picked up by a follow-up SDD.
- Batch-per-form generation (one LLM call per form returning a
  `{field_name → JS body}` map). Documented as a future optimisation
  in §3 and §8; not implemented in this phase. Per-field calls
  reuse the existing single-rule `RuleGenerator.generate(spec)`.
- `.mandatory()` chain (0 corpus uses) and `resetValueIfNull` (1
  use). Not curated, not generated.
- Role / user-group conditional visibility (`params.user`,
  `myUserGroups`). Not surfaced from the bundle parser today.
- Nested question groups (QG inside QG) — only first-level repeating
  groups are recognised by retrieval-driven QG handling.
- Read-only single-select / multi-select stabilisation patterns
  (return the new value only when `previousValue` was null on
  single-select; return only the difference on multi-select).
  Documented in the Avni docs, 0 corpus uses. Niche, deferred.
- `params.entityContext.affiliatedGroups` and
  `params.entityContext.group` — group-member access for household /
  affiliated-group rules. Not surfaced from the parser, 0 corpus
  uses.
- Runtime-equivalence testing of generated JS.
- Editing the source `.xlsx`.

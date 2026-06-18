# Form ↔ Entity Linking — Software Design Document

## 1. Objective

Today's parser links each `FormSpec` to its bundle entities (`subjectType`,
`program`, `encounterType`) by walking a fixed 5-step pipeline in
`_resolve_form_subject_types` (`src/domain/parser.py:1266+`). Step 0 keys off
keyword fragments in the form name (`registration`, `profile`, `details`,
`enrolment`, `enrollment`, `exit`); Steps 1–5 derive scope from whichever
references Step 0 produced. Forms whose name carries no such keyword get a
single shot — a fuzzy-match against encounter type names — and if that misses,
the entire mapping is emitted as `(null, null, null)`. avni-server's import
then either rejects the bundle outright (when the null tuple collides with
another mapping) or accepts a functionally broken row that never resolves
against any subject.

Observed Ekam failures with the current pipeline:

| Form sheet name | Modelling doc says | Bundle output today |
|---|---|---|
| `Awareness` | Subject Type `Awareness`, registration form | `Encounter`, all-null mapping |
| `Stakeholders Meeting` | Subject Type `Stakeholder Meeting` (singular) | `Encounter`, all-null mapping |
| `Save a Child Follow Up` | Program Encounter `Save a Child Followup ` (trailing space + spacing variant) under `Save a child` program | `Encounter`, all-null mapping |
| `Refferal Enrolment Hospital for` | Program `Referral`, encounter `Referral to Hospital Follow up` (31-char Excel truncation + `Refferal` typo) | `Encounter`, all-null mapping |

The shared root cause: the parser walks **bottom-up** — every sheet in the
forms xlsx is enumerated and the parser reverse-engineers which entity it
belongs to from the sheet name alone. The modelling doc, which *already
declares every entity-to-form relationship*, is consulted only for the
entity pools — never for the relationships themselves.

This SDD inverts the flow: the modelling doc becomes the authoritative
declaration of *which forms exist*, a single Claude Haiku pass matches each
declared entity to its sheet in the forms xlsx (handling typos, truncation,
spacing variants, file-vs-sheet-name confusion in one shot), and the
existing validator + HITL infrastructure gates the result.

---

## 2. Scope

### In scope

- An **entity catalog** extracted deterministically from the four modelling-doc
  tabs (`Subject Types`, `Programs`, `Program Encounters`, `Encounters`).
  Each declaration carries the raw form-reference column verbatim — file
  name, sheet name, or the literal word the operator typed.
- A new pipeline node `link_forms_to_entities` (Claude Haiku) that takes
  the entity catalog + the list of sheet names from the forms xlsx and
  returns, for every sheet, either:
  - the entity it belongs to (`{kind, name, [program]}`) and the
    resulting `formType`, plus a self-reported `confidence`, or
  - `entity: null` (junk / unrelated sheet, skip it).
- The deletion of `_resolve_form_subject_types`'s keyword ladder and Steps
  0–5. Form-to-entity binding flows entirely through the new node's output.
- A validator that rejects any LLM-returned entity reference that doesn't
  appear in the bundle's actual pools (defence in depth — the prompt
  already constrains to the allowlist; the validator catches the rare
  hallucination).
- HITL surfacing of any classification where `confidence: low`, or where
  a declared entity ended up with **no** matching sheet (orphan-entity
  warning).
- Junk-sheet detection (`Sheet35`-class entries) via the LLM's
  `entity: null` classification. Those sheets are silently dropped from
  `FormSpec` emission rather than emitted as broken `Encounter` rows.

### Out of scope

- Re-reading the source xlsx beyond what `parse_documents` already loads.
  The catalog is built from DataFrames already in `EntitySpec`; the sheet
  names are already in `xf.sheet_names`.
- Fixing Excel's 31-character sheet-name truncation at source. The LLM
  pass *resolves* the symptom (matching the truncated name to its
  intended entity), but the bundle's form name stays as the truncated
  string. A future ingest-time lint can warn operators (§11).
- Discovery of entities not declared in the modelling doc. If a form
  sheet exists but no Subject Types / Programs / Encounters row
  references it, the LLM may classify it as junk; the operator must add
  the declaration if they wanted it included.
- Cross-form / cross-file relationships (e.g. one form sheet shared across
  two subject types). Each sheet maps to at most one entity in v1.
- Generation of cancellation forms. That logic stays in
  `src/domain/generators.py:`'s `make_forms_and_concepts` (already
  patched in `FORM_LEVEL_RULES_SDD` follow-up to skip auto-generation
  when a manual cancellation exists for the parent's composite key).

---

## 3. Status of current code

### 3.1 Bottom-up enumeration

`src/domain/parser.py:1172` (`_load_xlsx`) returns every sheet in every
xlsx. `_classify_sheet` decides which ones are form sheets and which are
modelling tabs. Each form sheet then becomes a `FormSpec` (via
`_parse_form_sheet`), whose `subjectType / program / encounterType /
formType` start as `None / None / None / "Encounter"` and are filled in
later by `_resolve_form_subject_types`.

### 3.2 The 5-step keyword ladder

`_resolve_form_subject_types` at `src/domain/parser.py:1266-1370`:

- Step 0 — upgrade `formType` from `Encounter` to `IndividualProfile` /
  `ProgramEnrolment` / `ProgramExit` if the form name contains a magic
  keyword.
- Step 1 — fuzzy-match the form name against encounter type names.
- Steps 2–5 — derive subject / program from whatever Step 0 + 1 produced.

The ladder fails the moment a form name diverges from the keyword
convention — bare subject-type names (`Awareness`), bare program-encounter
names (`Save a Child Follow Up`), or Excel-truncated names
(`Refferal Enrolment Hospital for`) all fall through to a null mapping.

### 3.3 Modelling doc declarations are read but not used for linking

`parse_subject_types`, `parse_programs`, `parse_program_encounters`, and
`parse_encounters` already extract the entity declarations. The
`Registration Form` / `Enrolment Form` / `Exit Form` / `Forms Linked` /
`Cancellation Form` columns are read into raw text but **never consulted
for the form-to-entity binding** — only entity names flow forward. The
authoritative declarations sit unused while the parser fuzzy-matches in
the dark.

### 3.4 Fuzzy match scoring tied to a fragile threshold

`_fuzzy_match` runs exact → substring → word-Jaccard → character-overlap
at threshold 0.5. `Save a Child Follow Up` vs `Save a Child Followup `
scores exactly 0.5 on Jaccard — borderline; it doesn't match. Lowering
the threshold opens false positives elsewhere. `Refferal` vs `Referral`
fails on every pass.

---

## 4. Why a single LLM pass

The original draft of this SDD proposed a hybrid: tighten the
deterministic ladder *and* add an LLM fallback for whatever the patched
ladder still missed. After looking at the actual failure shapes, the
hybrid is over-engineered.

**The deterministic patches were paying back complexity for marginal
coverage.** Step −1 (Subject Types tab consultation), Step 6 (program-pool
fallback), fuzzy normalisation, threshold tweaks, plural stemming —
each one catches one or two named cases but leaves the next operator
spreadsheet to discover the next failure shape. The maintenance horizon
is unbounded.

**The matching problem is well within Claude Haiku's range.** ~25 entity
declarations × ~30 sheet names is a tiny matching problem. Total prompt
~2 KB, response ~3 KB, one call per bundle, ~$0.001 with Haiku, ~2 s of
latency. The pipeline already makes multiple Claude calls during field
enrichment (`enrich_with_llm`) and rule generation; this is noise.

**Every failure shape the deterministic ladder struggles with is exactly
what LLMs do well.** Plural/singular, spacing, typos, truncation,
file-vs-sheet-name confusion — handled in one shot by reading the
catalog + sheet list together, with context the deterministic passes
never had access to (which tab the entity was declared in determines its
formType; which program it's nested under determines its scope).

**Cost is bounded by a hard cap.** The validator rejects off-pool
references, the HITL pause surfaces low-confidence resolutions, and the
node short-circuits when the unresolved set is empty (saving the call
and latency on clean bundles).

---

## 5. Design

### 5.1 Entity catalog

`src/domain/parser.py` already produces three entity lists, defined in
`src/models.py`:

- `SubjectTypeSpec` — every Subject Type tab row (Individual, Awareness,
  Stakeholder Meeting, …).
- `ProgramSpec` — every Program tab row (Pregnancy, Child, Referral,
  Save a child).
- `EncounterTypeSpec` — covers **both** Program Encounters tab rows and
  standalone Encounters tab rows. The distinction is carried on two
  existing fields:
  - `is_program_encounter: bool` — `True` for rows from the Program
    Encounters tab.
  - `program_name: str` — the parent program when
    `is_program_encounter`; empty otherwise.

So `ProgramEncounter` is represented as
`EncounterTypeSpec(is_program_encounter=True, program_name="<program>")`
and `Encounter` as
`EncounterTypeSpec(is_program_encounter=False, program_name="")`. The
LLM pass receives them as two distinct sections in the prompt (§7) so
the formType derivation is unambiguous.

Augment each entity spec with the raw form-reference column from its
source row — same field on both EncounterTypeSpec variants because both
tabs have a `Form` and a `Cancellation Form` column:

```python
class SubjectTypeSpec(BaseModel):
    ...
    registration_form_source: str | None = None   # raw value from the xlsx

class ProgramSpec(BaseModel):
    ...
    enrolment_form_source: str | None = None
    exit_form_source: str | None = None

class EncounterTypeSpec(BaseModel):
    ...
    form_source: str | None = None                 # "Forms Linked" / "Form" column
    cancellation_form_source: str | None = None    # "Cancellation Form" column
```

These are surfaced to the LLM as context so it can disambiguate when the
source column happens to name a sheet (e.g. `ANC Cancellation`) rather
than a file (e.g. `Forms EKAM 31st July 2025`).

### 5.2 Pipeline placement

```
discover_files
  → parse_documents          ← entity catalog now carries form_source columns
  → link_forms_to_entities   ← NEW (this SDD)
  → enrich_with_llm          ← unchanged; receives properly-linked FormSpecs
  → resolve_via_hitl
  → generate_entities → ...
```

`link_forms_to_entities` runs after parsing and before field enrichment so
the latter sees correct `formType` / `subjectType` for prompt assembly.
It's a no-op when the bundle has no form sheets to classify (saves the
call on enrich-only runs).

### 5.3 The node — `link_forms_to_entities`

Inputs:

- The deterministically-extracted entity catalog (§5.1).
- The list of sheet names in the forms xlsx — taken from
  `_load_xlsx`'s caller, no second file read.
- The modelling-doc filename(s) and the forms-doc filename, so the LLM
  can distinguish "this column points at a file" from "this column points
  at a sheet within a file".

Outputs (one row per sheet, in input order):

```python
class FormLinkResult(BaseModel):
    sheet_name: str
    form_type: Literal[
        "IndividualProfile",
        "ProgramEnrolment",
        "ProgramExit",
        "ProgramEncounter",
        "ProgramEncounterCancellation",
        "Encounter",
        "IndividualEncounterCancellation",
    ] | None
    subject_type: str | None      # must be in subject_types pool when non-null
    program: str | None           # must be in programs pool when non-null
    encounter_type: str | None    # must be in encounter_types pool when non-null
    confidence: Literal["high", "medium", "low"]
    reasoning: str                # one short sentence; surfaced in HITL prompt
```

`form_type=None` (and all entity fields null) ⇒ the LLM concluded the
sheet is junk / not a form. The pipeline silently drops it (no FormSpec
emitted).

Cancellation form types are part of the schema because the source
spreadsheet routinely contains manually-authored cancellation sheets
(`ANC Cancellation`, `PNC Cancellation`, `Child anthropometry
cancellation`, …) that the LLM must classify with the correct parent
encounter binding. Auto-generation of cancellation forms when no manual
one exists stays in the generator (`FORM_LEVEL_RULES_SDD` follow-up
patch); the LLM only classifies sheets that are present in the forms
xlsx.

### 5.4 LLM call

- Model: `claude-haiku-4-5` (matches existing enrichment node's choice).
- Temperature: 0.
- One call, all sheets at once. Output validated against the
  `FormLinkResult` Pydantic schema. Retries on schema-validation failure
  bounded to 2 attempts; permanent failure leaves the form unresolved
  and surfaces a warning.

### 5.5 Validator (`src/domain/form_links.py`)

For each `FormLinkResult`:

- `subject_type`, `program`, `encounter_type` either `None` or an exact
  (case-insensitive, whitespace-stripped) member of the corresponding
  pool. Off-pool → reject the result, leave the sheet unresolved with a
  warning.
- `form_type` and required-reference combinations consistent:
  - `IndividualProfile` ⇒ `subject_type` non-null.
  - `ProgramEnrolment`, `ProgramExit` ⇒ `subject_type` + `program`
    non-null.
  - `ProgramEncounter`, `ProgramEncounterCancellation` ⇒ `subject_type`
    + `program` + `encounter_type` non-null.
  - `Encounter`, `IndividualEncounterCancellation` ⇒ `subject_type` +
    `encounter_type` non-null, `program` null.
  - All-null ⇒ junk (allowed).
- `confidence ∈ {high, medium, low}`.

Validation failures don't crash the run — they degrade the affected
sheet to the deterministic baseline (today's null mapping), with the
sheet listed in `state.form_link_warnings` for operator review.

### 5.6 HITL surfacing

Re-use the existing enrichment HITL change-card type. Surface a card when:

- `confidence == "low"`.
- The LLM classified a sheet as junk (`entity: null`) but its name
  looks form-like (>15 chars, no `Sheet\d+` pattern) — false-positive
  guard for over-eager skipping.
- Any declared entity has no matching sheet across all classifications
  ("orphan entity"). The card asks the operator: "the `Awareness`
  subject's registration form was declared but no sheet matched. Pick
  from existing sheets / skip / abort?"

`high`-confidence resolutions of well-defined entities auto-apply with
no prompt.

### 5.7 Dropping the keyword ladder

`_resolve_form_subject_types` is deleted in full. Its callers (already a
single call in `parse_scoping_docs`) now invoke the new node's output
through `_apply_form_link_results(forms, results)`:

```python
def _apply_form_link_results(
    forms: list[FormSpec], results: list[FormLinkResult]
) -> list[FormSpec]:
    """Stamp each FormSpec with the LLM-resolved linkage. Drops sheets
    classified as junk (form_type=None). Logs warnings for any sheet
    that couldn't be linked above the validator's confidence floor."""
```

`FormSpec` whose name doesn't appear in `results` (LLM omission) keeps
its parser-default (`formType=Encounter`, all-null) but is flagged for
the HITL pause — the operator decides whether to drop, retry, or
manually map.

---

## 6. Data model changes

`src/models.py` (entity spec module):

```python
class SubjectTypeSpec(BaseModel):
    ...
    registration_form_source: str | None = None

class ProgramSpec(BaseModel):
    ...
    enrolment_form_source: str | None = None
    exit_form_source: str | None = None

class EncounterTypeSpec(BaseModel):
    ...
    # Existing fields already distinguish Program Encounter rows from
    # standalone Encounter rows:
    #   is_program_encounter: bool
    #   program_name: str   (non-empty when is_program_encounter)
    form_source: str | None = None
    cancellation_form_source: str | None = None
```

`src/domain/form_links.py` (new file):

```python
class FormLinkResult(BaseModel):
    sheet_name: str
    form_type: Literal[
        "IndividualProfile",
        "ProgramEnrolment",
        "ProgramExit",
        "ProgramEncounter",
        "ProgramEncounterCancellation",
        "Encounter",
        "IndividualEncounterCancellation",
    ] | None
    subject_type: str | None
    program: str | None
    encounter_type: str | None
    confidence: Literal["high", "medium", "low"]
    reasoning: str
```

`src/pipeline/state.py`:

```python
class BundleState(TypedDict):
    ...
    form_link_warnings: list[str]
```

No other model changes. The `FormSpec`'s existing
`subjectType / program / encounterType / formType` fields are populated
by the new pipeline node instead of `_resolve_form_subject_types`.

---

## 7. Prompt

```
SYSTEM
You match form sheets to entity declarations in an Avni bundle. The
modelling document declares which subjects, programs, and encounters
exist; the forms document contains the sheets to classify. For each
sheet name, decide which declared entity it represents, or `null` if
the sheet is not a form (junk, scratch, metadata).

Rules:
- The entity must appear in the catalog. Never invent entries.
- Determine formType from which tab the entity was declared in:
    Subject Type tab + registration form → IndividualProfile
    Program tab + enrolment form          → ProgramEnrolment
    Program tab + exit form               → ProgramExit
    Program Encounters tab + main form    → ProgramEncounter
    Program Encounters tab + cancellation → ProgramEncounterCancellation
                                            (subject_type + program + encounter_type
                                             all inherited from the parent encounter row)
    Encounters tab + main form            → Encounter
    Encounters tab + cancellation         → IndividualEncounterCancellation
                                            (subject_type + encounter_type inherited from
                                             the parent row; program is null)
  Cancellation sheets are identified by a sheet name ending in
  "Cancellation" / "cancellation" or by matching a non-empty
  "Cancellation Form" column on the parent row.
- Use the form-source column verbatim only as a hint — operators
  sometimes name a file there ("Forms EKAM 31st July 2025") and
  sometimes a specific sheet ("ANC Cancellation"). The sheet name itself
  is the authoritative target.
- Confidence:
    high   — sheet name matches a declared entity unambiguously
    medium — match required handling spacing/typo/truncation, but the
             intent is clear from context
    low    — multiple plausible entities or no clear match

USER
ENTITIES (from modelling doc):

  Subject Types:
    - Individual (registration form source: "Forms EKAM 31st July 2025")
    - Awareness (registration form source: "Forms EKAM 31st July 2025")
    - Stakeholder Meeting (registration form source: "Forms EKAM 31st July 2025")
    - Convergence Action Plan (registration form source: "Empty Registration")
    - Household

  Programs:
    - Pregnancy   target: Individual   enrolment: "Forms EKAM 31st July 2025"
    - Child       target: Individual   enrolment: "Forms EKAM 31st July 2025"  exit: "Forms EKAM 31st July 2025"
    - Referral    target: Individual   enrolment: "Forms EKAM 31st July 2025"
    - Save a child target: Individual  enrolment: "Forms EKAM 31st July 2025"  exit: "Blank exit"

  Program Encounters:
    - ANC                                under Pregnancy    forms: "Forms EKAM..."   cancellation: "ANC Cancellation"
    - Delivery                           under Pregnancy
    - PNC                                under Pregnancy                              cancellation: "PNC Cancellation"
    - Mental Health Screening            under Pregnancy
    - Child Followup and Anthropometry   under Child                                   cancellation: "Child anthropometry cancellation"
    - Referral to Hospital Follow up     under Referral                                cancellation: "Referral Follow Up Cancellation"
    - Save a Child Followup              under Save a child
    - Immunization                       under Child

  Encounters:
    - Convergence Action under Convergence Action Plan
    - General Survey     under Household

SHEET NAMES (from forms doc):
  ["Individual Registration", "Pregnancy Enrolment", "ANC", "ANC Cancellation",
   "PNC", "PNC Cancellation", "Mental Health Screening", "Delivery", "Pregnancy Exit",
   "Child Enrollment", "Child Followup and Anthropometr", "Sheet35", "Child Exit",
   "Immunization", "Refferal Enrolment Hospital for", "Referral to Hospital Follow up",
   "Referral Follow Up Cancellation", "Referral to Hospital Exit",
   "Save a Child Enrolment", "Save a Child Follow Up", "Save a child exit",
   "Stakeholders Meeting", "Convergence Action Plans", "Awareness",
   "Medical and Non-Medical Support", "Training "]

Return a JSON array, one object per sheet, in input order.
```

The model returns an array of `FormLinkResult` objects; the node maps
each entry back to the parser's `FormSpec` by sheet name (case-insensitive
exact match).

---

## 8. Validation

§5.5 in code. Additionally:

- The full catalog of valid `subject_type` / `program` / `encounter_type`
  values is computed once per bundle and passed into the validator. The
  prompt also lists these so the LLM has the allowlist verbatim; the
  validator double-checks defensively.
- Junk classifications (`form_type=None`) are allowed only when every
  entity field is also null. Mixed shapes (e.g.
  `form_type=null, subject_type="Awareness"`) are rejected as schema
  violations.
- Confidence values outside the literal set degrade to `low`.

---

## 9. Files to create / change

| File | Action | Description |
|---|---|---|
| `src/models.py` | edit | Add `registration_form_source` / `enrolment_form_source` / `exit_form_source` / `form_source` / `cancellation_form_source` to the corresponding entity specs. |
| `src/domain/parser.py` | edit | Populate the new `_source` fields from the raw xlsx columns in each `parse_*` function. Delete `_resolve_form_subject_types`. Replace its call site in `parse_scoping_docs` with a hand-off to the new pipeline node's output. |
| `src/domain/form_links.py` | create | `FormLinkResult` Pydantic model, prompt template, Claude Haiku call, validator, `_apply_form_link_results`. Mirrors the shape of `src/domain/enricher.py`. |
| `src/pipeline/nodes.py` | edit | New `link_forms_to_entities` node calling into `form_links.py`. Short-circuits when the form-sheet list is empty. |
| `src/pipeline/graph.py` | edit | Wire the new node between `parse_documents` and `enrich_with_llm`. |
| `src/pipeline/state.py` | edit | Add `form_link_warnings: list[str]`. |
| `tests/domain/test_form_links.py` | create | Unit tests for: validator rejection of off-pool references; junk-sheet classification; round-trip on the four named Ekam failure cases via a stub agent. |

---

## 10. Order of execution

1. **Data model + parser changes** — add `_source` fields to the entity
   specs and populate them. Backward-compatible (defaults are `None`).
   Existing bundles regenerate byte-identical.
2. **`form_links.py` + validator + stub-agent tests** — pure functions,
   no pipeline integration yet. Verify validator behaviour on synthetic
   `FormLinkResult` payloads.
3. **Pipeline wiring** — add `link_forms_to_entities` node + graph edge.
   Stub the agent in dev to skip the LLM call until the prompt is tuned.
4. **Live LLM integration** — point at Claude Haiku, run against Ekam
   and Astitva. The four named failure cases should resolve correctly
   with `medium`/`high` confidence; `Sheet35`-class entries should
   classify as junk.
5. **`_resolve_form_subject_types` deletion** — remove the keyword
   ladder once the new node is producing correct output. Re-run every
   bundle under `resources/output/` and diff `formMappings.json` for
   regressions.
6. **HITL surfacing** — wire low-confidence and orphan-entity cards
   into the existing change-card UI. Auto-apply `high`.

Steps 1–4 are mergeable independently; step 5 is the irreversible
switch-over.

---

## 11. Future extensions (out of scope here)

- **Ingest-time lint for Excel-truncated sheet names.** Detect any sheet
  name at exactly 31 characters and surface a warning at the upload
  step so operators can rename at source. Today the LLM pass papers
  over truncation (matching `Refferal Enrolment Hospital for` →
  `Referral` program) but the form's display name in the bundle stays
  truncated.
- **Reverse direction.** The current pass classifies sheets against
  declared entities. A future pass could also flag entities (programs,
  encounters) with no matching sheet — already partially covered by
  §5.6's orphan-entity card, but a richer surface (per-entity status
  table at end of parse) would help operators audit completeness.
- **Multi-form-per-entity support.** A subject type could in principle
  have multiple registration forms (different demographics). Out of
  scope for v1; the current schema enforces one-to-one.
- **Cancellation-form classification by the same pass.** Today the LLM
  pass classifies main forms; cancellation forms are still auto-generated
  by the generator (per `FORM_LEVEL_RULES_SDD` follow-up). Folding
  cancellation classification into the same call would surface
  inconsistencies (a manually-authored cancellation that doesn't
  match the modelling doc's `Cancellation Form` column) at parse time
  instead of at generate time.
- **Provider abstraction.** If a future deployment can't or shouldn't
  call Anthropic, the validator + HITL infrastructure means a different
  classifier (local model, rules-based, manual) can plug in behind the
  same `FormLinkResult` contract. The LLM call is one swappable function
  inside `form_links.py`.

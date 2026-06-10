# Concept Answer Grounding for Rule Generation — Software Design Document

## 1. Objective

When a user's rule intent references a coded-concept answer informally — e.g.
*"if answer for why do you want to work is supporting family, then schedule in 2
days"* — the rule generator must resolve "supporting family" to the **exact
answer string** that exists in the bundle (e.g. `"can support my family"`),
not pass the user's literal phrasing through to the generated JS.

Today `RuleSpec` exposes available concept *names* and encounter-type names to
the LLM, but **not** the answer options for coded concepts. The model is
forced to invent strings for `containsAnswerConceptName(...)` calls — which at
runtime never match anything in the bundle, silently producing rules that
never fire.

This SDD closes that grounding gap.

---

## 2. Scope

### In scope

- A new `concept_answers: dict[str, list[str]]` field on `RuleSpec` carrying
  every coded-concept's answer list **across every form a rule on the target
  form can legitimately reference** (target form + registration form for the
  same subject type + enrolment form for the same program, when applicable).
  See §5 for the exact resolution rule.
- The pipeline node (`generate_rules`) and chat tool (`set_visit_schedule_rule`)
  both populate it when building the `RuleSpec`.
- A new `CONCEPT_ANSWERS` block in the user prompt rendering the available
  answers per concept.
- System-prompt instruction to map informal phrasings to the exact answer
  string from the allowlist — never the user's wording verbatim.
- Validator extension: every literal passed to `containsAnswerConceptName(...)`
  / `containsAnyAnswerConceptName(...)` must appear in the corresponding
  concept's answer list. Off-list → reject with a warning listing the valid
  answers (so the user can re-try with the right phrasing).

### Out of scope

- **Pre-resolution step** (fuzzy-matching the user's informal phrasing →
  exact answer in Python before prompting). Tracked as a future enhancement
  in §11.
- **Sibling-encounter answer grounding** — answers from *other* encounter
  forms in the same program are not in scope; only the target form, its
  registration, and its enrolment (if any). See §11.
- Answer grounding for non-coded concepts (numeric ranges, date ranges,
  free-text).
- Changes to the catalog or knowledge base — the RAG layer is unchanged.
- Migration of existing failed rules in already-generated bundles.

### Precondition

`EntitySpec` already carries each coded concept's answer options on
`FieldSpec.options` for `dataType == "Coded"`. The pipeline parser populates
this from the modelling sheet. No parser changes are needed.

---

## 3. Failure case that motivates this

From the `durga_india` run:

User intent (form: **Baseline for Women**):
> "Schedule endline assessment based on these conditions: if answer for why do
> you want to work is **supporting family** then schedule in 2 days, if it is
> **gives me financial independence** schedule in 4 days, if it is **feel more
> confident** then schedule in 6 days. Don't schedule on Sundays. Overdue can
> be 2 days after due date."

Bundle's actual answers for the *"Why do you want to work"* concept:
- `"can support my family"`
- `"It gives me financial independence"`
- `"It makes me feel more confident and independent"`

The generated JS used the user's literal phrases (`"supporting family"` etc.)
in `containsAnswerConceptName(...)` calls — so the rule compiled cleanly but
would never match at runtime. Validation passed because no validator checked
answer-string grounding.

---

## 4. Data model change

### `RuleSpec` (`src/domain/rules/rule_spec.py`)

```python
class RuleSpec(BaseModel):
    ...
    available_concepts: list[str]
    available_encounter_types: list[str]
    available_programs: list[str]
    # NEW: for each coded concept on the form (or sibling forms in the
    # enrolment / encounter context), the list of answer concept names.
    # Keys are concept names; values are the answer strings as stored in
    # concepts.json. Non-coded concepts are not included.
    concept_answers: dict[str, list[str]] = Field(default_factory=dict)
```

Backward-compatible — default is an empty dict, so existing call sites that
don't pass it continue to work (rule generation will behave as today).

---

## 5. Population — which forms are in scope

### The resolution rule

For a rule generated against a **target form**, `concept_answers` is the
**merged** answer map across every form the rule can legitimately read from
at runtime:

| Target form's `formType` | Forms in scope for answer grounding |
|---|---|
| `IndividualProfile` (registration) | Target form only |
| `Encounter`, `IndividualEncounterCancellation` | Target form + the registration form for the same `subjectType` |
| `ProgramEnrolment`, `ProgramExit` | Target form + the registration form for the same `subjectType` |
| `ProgramEncounter`, `ProgramEncounterCancellation` | Target form + the registration form for the same `subjectType` + the `ProgramEnrolment` form for the same `program` |

This matches what an Avni rule can actually access via the helper API:
`programEncounter.programEnrolment.individual.findLatestObservationInEntireEnrolment(...)`
walks all three layers. Whatever the rule can reach at runtime should be in
the grounding set at prompt time.

### Merge strategy (collisions)

The result is a flat `dict[concept_name, list[answer_names]]`. If the same
concept name appears on multiple in-scope forms with the **same** answer
list, the merge is a no-op. If two forms reuse a concept name with
**different** answers (rare; an authoring smell), the merged list is the
**union** — both sets of answers are valid for grounding, and the runtime
will check against the concept's actual answers anyway. A warning is logged
when a collision is detected so the authoring drift surfaces.

### Pipeline path: `generate_rules` node (`src/pipeline/nodes.py`)

In `_build_rule_spec`, walk every in-scope form (per the table above) and
collect coded-field answers. A helper handles the form-scope resolution:

```python
def _forms_in_scope_for(target: FormSpec, all_forms: list[FormSpec]) -> list[FormSpec]:
    """Return the list of forms whose coded answers ground the target form's rule."""
    in_scope = [target]
    if target.formType in _PROGRAM_FORM_TYPES:
        enrolment = next(
            (f for f in all_forms
             if f.formType == "ProgramEnrolment" and f.program == target.program),
            None,
        )
        if enrolment and enrolment is not target:
            in_scope.append(enrolment)
    if target.formType != "IndividualProfile":
        registration = next(
            (f for f in all_forms
             if f.formType == "IndividualProfile" and f.subjectType == target.subjectType),
            None,
        )
        if registration and registration is not target:
            in_scope.append(registration)
    return in_scope


def _collect_concept_answers(forms: list[FormSpec]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for form in forms:
        for section in (form.sections or []):
            for field in (section.fields or []):
                if field.dataType != "Coded" or not field.options:
                    continue
                existing = merged.get(field.name)
                if existing is None:
                    merged[field.name] = list(field.options)
                elif set(existing) != set(field.options):
                    log.warning(
                        f"concept '{field.name}' has different answer lists on "
                        f"multiple forms; merging as union"
                    )
                    merged[field.name] = list(dict.fromkeys(existing + list(field.options)))
    return merged
```

`_build_rule_spec` then calls these:

```python
in_scope = _forms_in_scope_for(form_spec, spec.forms)
concept_answers = _collect_concept_answers(in_scope)
```

### Chat-tool path: `bundle_editor.load_form_rule_context`

The chat tool reads from the already-built bundle, not from `EntitySpec`. So
the answer list has to be reconstructed from `formMappings.json` +
`concepts.json` + the form JSONs:

1. Identify the **target form** by `form_name`. Read its
   `formType` / `subjectTypeUUID` / `programUUID` from `formMappings.json`.
2. Identify **in-scope sibling forms** by walking `formMappings.json`:
   - For any non-registration target: find the form with
     `formType == "IndividualProfile"` and matching `subjectTypeUUID`.
   - For `ProgramEncounter*` targets: find the form with
     `formType == "ProgramEnrolment"` and matching `programUUID`.
3. Load each in-scope form's JSON from `forms/`.
4. For every `formElement` whose linked `concept.dataType == "Coded"`, read
   its `answers[*].name`.
5. Merge using the same union-on-collision strategy as the pipeline path.

This logic goes in a new helper inside `bundle_editor.py`:
`_collect_concept_answers_for(bundle_path, target_form_name)`. Called from
`load_form_rule_context` and added to the returned context dict alongside
`available_concepts`.

Constants needed: a `_PROGRAM_FORM_TYPES` frozenset (already exists in
`src/domain/generators.py`) — promote to a shared module or import from
generators.

---

## 6. Prompt change

### System prompt addition (`src/domain/rules/prompts.py`)

Add to the hard-constraints list in `_SYSTEM_PROMPT_TEMPLATE`:

> 7. When using `containsAnswerConceptName(...)` or
>    `containsAnyAnswerConceptName(...)`, the string argument MUST match an
>    exact entry in `CONCEPT_ANSWERS` for the relevant concept. Never pass
>    the user's informal phrasing verbatim. If the user says "supporting
>    family", you must look up the right entry under the relevant concept
>    (e.g. "can support my family") and use that string.

### User prompt addition (`build_user_prompt`)

New `CONCEPT_ANSWERS` block, rendered only when `spec.concept_answers` is
non-empty:

```
CONCEPT_ANSWERS
- Why do you want to work
    * "can support my family"
    * "It gives me financial independence"
    * "It makes me feel more confident and independent"
- Marital status
    * "Married"
    * "Unmarried"
```

Renderer pseudo-code:

```python
def _format_concept_answers(answers: dict[str, list[str]]) -> str:
    if not answers:
        return "(no coded concepts on this form)"
    lines = []
    for concept, options in answers.items():
        lines.append(f"- {concept}")
        for opt in options:
            lines.append(f'    * "{opt}"')
    return "\n".join(lines)
```

---

## 7. Validator change (`src/domain/rules/validator.py`)

Extend the AST walk to track which `containsAnswerConceptName(...)` /
`containsAnyAnswerConceptName(...)` call belongs to which `valueInEncounter`
/ `valueInRegistration` chain — so we can check the answer string against the
right concept's answer list.

Two-step walk:

1. Find each `RuleCondition` chain. Within the chain, identify the
   `valueInEncounter(conceptName)` or `valueInRegistration(conceptName)`
   anchor, then collect every subsequent `containsAnswerConceptName(...)` /
   `containsAnyAnswerConceptName(...)` literal argument.
2. For each `(concept, answer)` pair, check `answer ∈
   spec.concept_answers[concept]` (case-insensitive). Off-list → reject with
   a warning of the form:

```
rules.visitScheduleRule.Baseline for Women: off-bundle answer
  'supporting family' for concept 'Why do you want to work' — expected one
  of: ['can support my family', 'It gives me financial independence',
  'It makes me feel more confident and independent']
```

The verbose warning is deliberate — it tells the user exactly what wording
to retry with.

Backward-compatible: if `spec.concept_answers` is empty (older call sites,
or no coded concepts on the form), the answer-grounding check is skipped.

---

## 8. Files to create / change

| File | Status | Description |
|---|---|---|
| `src/domain/rules/rule_spec.py` | edit | Add `concept_answers: dict[str, list[str]]` to `RuleSpec`. |
| `src/domain/rules/prompts.py` | edit | Add `_format_concept_answers` + a `CONCEPT_ANSWERS` block in `build_user_prompt`; add constraint #7 to the system prompt. |
| `src/domain/rules/validator.py` | edit | Walk `RuleCondition` chains; check `containsAnswerConceptName(...)` literals against `spec.concept_answers`. |
| `src/pipeline/nodes.py` | edit | In `_build_rule_spec`, populate `concept_answers` from `FormSpec.sections[*].fields[*]`. |
| `src/domain/bundle_editor.py` | edit | Add `_extract_form_concept_answers(form_dict)`; include in `load_form_rule_context` output. |
| `src/chat/tools.py` | edit | Pass `concept_answers` from the bundle-editor context into the `RuleSpec` built inside `set_visit_schedule_rule`. |

No new dependencies. No catalog or knowledge-base changes.

---

## 9. Verification

1. **Unit (validator):** craft a `RuleResult` whose JS calls
   `containsAnswerConceptName('supporting family')` against a spec where
   `concept_answers["Why do you want to work"] = ["can support my family", ...]`
   — assert validator rejects with the new warning listing the valid options.
2. **Unit (validator, pass case):** same spec, JS uses
   `containsAnswerConceptName('can support my family')` — assert it passes.
3. **Unit (prompts):** call `build_user_prompt` with a non-empty
   `concept_answers`; assert the rendered prompt contains the
   `CONCEPT_ANSWERS` block with the right indentation.
4. **Integration:** re-run `set_visit_schedule_rule` against the durga_india
   bundle with the original failing intent — assert the generated JS uses
   exact answer strings from the bundle's `concepts.json` (or, on failure,
   that the warning surfaces the valid options).

---

## 10. Order of execution

1. **Data model first** — add `concept_answers` to `RuleSpec` (no-op default,
   backward-compatible).
2. **Validator** — extend to check the new field; verifies safety end-to-end
   regardless of generator behaviour.
3. **Prompt** — inject `CONCEPT_ANSWERS` and the constraint line so the LLM
   has a chance to ground correctly.
4. **Pipeline + chat-tool population** — fill `concept_answers` from both
   paths.
5. **Re-run failing case** — durga_india Baseline-for-Women rule should now
   generate JS that grounds against the right answers (or fail validation
   with a useful warning).

Each step is independently mergeable.

---

## 11. Future extensions (out of scope here)

- **Pre-resolution in Python.** Before prompting, fuzzy-match the user's
  intent against known answers and inject resolved candidates as hints. Moves
  the *"find the exact answer"* work out of the LLM and into deterministic
  code — more reliable, fewer retries.
- **Sibling-encounter answer grounding.** Today's scope is target form +
  registration + enrolment. A rule on `ProgramEncounter A` can technically
  also read answers from `ProgramEncounter B` in the same enrolment via
  `programEnrolment.getEncountersOfType(...)`. Adding sibling encounters
  bloats the prompt (every program-encounter form in scope), so it stays
  out of v1. If a real rule needs it, lift the form-scope helper to include
  `formType in _ENCOUNTER_FORM_TYPES` siblings filtered by `program`.
- **Answer grounding for non-coded concepts.** Numeric ranges
  (`min`/`max`/`unit`), date validation, free-text patterns — each a separate
  grounding contract.
- **Generalising the pattern.** Same problem exists for any
  user-informal-phrasing → bundle-exact-string mapping (form names, encounter
  types, subject types). Today encounter types are fuzzy-matched in the
  parser; the same machinery could resolve answers at prompt-build time.

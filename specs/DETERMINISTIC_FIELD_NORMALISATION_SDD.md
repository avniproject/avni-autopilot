# Deterministic Field Normalisation SDD

## Problem

The current enrichment pass has two responsibilities mixed together:

1. **Detecting** long names (>255 chars) and duplicate field names — both are objective, deterministic conditions.
2. **Suggesting** shorter / disambiguated names — this requires understanding context and intent, so it belongs to the LLM.

The LLM is asked to do both, which is wrong. LLMs cannot count characters reliably, so they hallucinate `long_name` changes for fields that are well within the limit, requiring `_drop_unjustified_changes` as a correction layer. Duplicate detection suffers the same problem — the LLM misses some and invents others.

Downstream, `generators.py` carries two hardcoded stop-gaps that exist only because the enricher doesn't clean up reliably:

- `_truncate_name()` silently hard-truncates any name that slipped through at >255 chars.
- `seen_form_concepts` skips duplicate fields that weren't renamed upstream.

Both are commented as "TEMPORARY" / "proper fix is upstream" — this SDD is that fix.

---

## Goal

- Detection of long names and duplicates: **deterministic, in Python, before the LLM is called**.
- LLM's only job: **suggest** a shorter or disambiguated name for each detected problem. It receives a pre-built problem list and does not scan forms itself.
- **One LLM call** across all forms, not one per dirty form.
- Remove `_truncate_name` from `generators.py` and the `seen_form_concepts` duplicate-skip logic. Both become dead code once normalisation is guaranteed upstream.

---

## Scope

**In scope:**
- Refactor `enricher.py` to detect issues deterministically and pass a combined problem list to one LLM call.
- Refactor `llm.py` prompt + schema so the LLM only fills in name suggestions, not detects problems. Drop the raw sheet from the prompt; pass section-scoped field lists as context instead.
- Remove `_truncate_name` calls and `seen_form_concepts` duplicate-skip from `generators.py`.
- Remove `_drop_unjustified_changes` from `enricher.py` — it exists solely to correct LLM detection errors.

**Out of scope:**
- Changing the `Change` model shape or the downstream `apply_user_decisions` flow.
- Any other enrichment concern (data types, options, skip logic).
- Changing the 255-char limit.

---

## Design

### Step 1 — Deterministic detection (enricher.py)

Replace `_form_is_clean` with a function that returns a structured list of problems across all forms:

```python
@dataclass
class _FieldProblem:
    kind: Literal["long_name", "duplicate_field"]
    form_name: str
    field_name: str
    section_name: str
    occurrence: int          # 1-based; 1 for long_name, 1..N for duplicates
    context_fields: list[str]  # other field names in the relevant section(s)
```

Detection rules (no LLM involved):

- **long_name**: `len(field.name) > 255`. One problem per offending field. `context_fields` = other fields in the same section.
- **duplicate_field**: group fields by `field.name.strip().lower()` within a form. Any group with count > 1 produces one problem per occurrence. `context_fields` = all fields from all sections where the duplicate appears (so the LLM sees the full neighbourhood for disambiguation).

`enrich_forms` collects problems across all forms in one pass. If the combined list is empty, skip the LLM call entirely.

### Step 2 — One LLM call for all problems (llm.py)

A single call replaces the current per-form calls. The user message lists every problem across all forms:

```
FORM: Beneficiary Registration
[1] duplicate_field (1 of 2) — "Remarks"
    Section "School Details" fields: Name, Age, School Name, Standard, Remarks
    Section "Family Details" fields: Guardian Name, Occupation, Contact, Remarks

FORM: HCCM Distribution for Pregnancy
[2] duplicate_field (1 of 2) — "HCCM Distribution Cycle"
    Section "Cycle 1" fields: Date, Quantity, HCCM Distribution Cycle, Remarks
    Section "Cycle 2" fields: Date, Quantity, HCCM Distribution Cycle, Remarks
[3] long_name — "Please describe the issue in detail including all relevant observations"
    Section "Health Notes" fields: HB Level, Health Issue Type, <long field>, Notes

For each problem return: form_name, section_name, field_name (matching exactly as listed above) and suggested_name.
```

No raw sheet, no full FormSpec JSON. Context is limited to the field neighbourhood needed for naming — compact regardless of how many forms or problems there are.

### Step 3 — Response schema (llm.py)

Replace `EnrichedFormSpec` with a leaner schema:

```python
class _NameSuggestion(BaseModel):
    form_name: str
    section_name: str
    field_name: str
    suggested_name: str

class _SuggestionsOutput(BaseModel):
    suggestions: list[_NameSuggestion]
```

Matched back by `(form_name, section_name, field_name)` — unique per occurrence because a duplicate field name appears at most once per section. Robust to the LLM skipping or reordering suggestions; unmatched problems are logged as warnings and skipped.

### Step 4 — Change assembly (enricher.py)

After the single LLM call, `enrich_forms` builds `Change` records by key lookup:

```python
suggestion_map = {
    (s.form_name, s.section_name, s.field_name): s.suggested_name
    for s in output.suggestions
}
for problem in problems:
    suggested = suggestion_map.get(
        (problem.form_name, problem.section_name, problem.field_name)
    )
    if suggested is None:
        log.warning("[enrich] no suggestion returned for %s / %s / %s",
                    problem.form_name, problem.section_name, problem.field_name)
        continue
    change_id = f"{problem.form_name}::{problem.section_name}:{problem.field_name}/{problem.kind}"
    changes.append(Change(
        change_id=change_id,
        form=problem.form_name,
        field=problem.field_name,
        kind=problem.kind,
        before={"name": problem.field_name, "section": problem.section_name},
        after={"name": suggested},
        reason="auto-detected",
    ))
```

`_drop_unjustified_changes` is removed — detection is now guaranteed correct.

`enrich_form` (single-form entry point) is removed. `enrich_forms` is the only public entry point and owns both detection and the single LLM call.

### Step 5 — Remove stop-gaps from generators.py

- Remove `_truncate_name` and all call sites. Field names are guaranteed ≤255 chars before they reach the generator.
- Remove `seen_form_concepts` and the `continue` on duplicate UUID. Fields are guaranteed unique names (and thus unique UUIDs) within a form before they reach the generator.
- Remove the `MAX_NAME_LEN = 255` constant and `TEMPORARY` comments.

---

## Files changed

| File | Change |
|---|---|
| `src/domain/enricher.py` | Replace `_form_is_clean` + per-form loop with `_detect_problems` (all forms, one pass) + single LLM call + Change assembly; remove `_drop_unjustified_changes`; remove `enrich_form` |
| `src/domain/llm.py` | New prompt (problem list + section-scoped context, no raw sheet); new response schema (`_SuggestionsOutput`); remove `EnrichedFormSpec` |
| `src/domain/generators.py` | Remove `_truncate_name`, all call sites, `seen_form_concepts` block, `MAX_NAME_LEN` |
| `src/pipeline/nodes.py` | Remove `_truncate_name` import; update `enrich_with_llm` to call `enrich_forms` only (no `enrich_form`) |

---

## Verification

After implementation:

1. Run the pipeline on Astitva and ekam_org inputs.
2. Confirm exactly one Anthropic API call is made during the enrichment phase (check logs).
3. Confirm the `duplicate_field renamed` log lines still appear for known-duplicate forms (HCCM Distribution, Child Monitoring).
4. Confirm no `_truncate_name` calls remain in the codebase (`grep -r "_truncate_name" src/`).
5. Confirm `seen_form_concepts` is removed from `generators.py`.
6. Confirm `_drop_unjustified_changes` is removed from `enricher.py`.

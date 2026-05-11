# Bundle Editing — Software Design Document

## 1. Objective

Edit an **already-generated bundle ZIP** to add, rename, or remove fields — without re-parsing the source `.xlsx` modelling/scoping documents and without needing the in-memory `EntitySpec` from a prior pipeline run.

Inputs:
- Path to a bundle ZIP (or its unpacked directory).
- A list of field-level edit operations (one or more).

Output:
- A new bundle ZIP at the same path (overwriting the original).
- A summary of changes applied, warnings, and rejections.

---

## 2. Scope

### In scope

- **Field operations only**: add, rename, remove.
- Operates on a bundle ZIP — the *output* of bundle generation, not the input. No source documents required.

### Out of scope

- Editing sections, forms, subject types, programs, encounter types, address levels, organisation config.
- Adding/removing form mappings (no mapping change happens for field edits).
- Rule fields — stay empty strings.
- Rollback / undo. The previous ZIP is overwritten in place; users keep their own copies if they need history.
- Re-running the deterministic parser or LLM enrichment.
- Editing the source `.xlsx` to keep it in sync — the user is responsible for that.
- **Editing existing concepts in `concepts.json`.** Rename and remove never modify `concepts.json`. The one exception is `field.add`, which appends a new concept entry when a fresh concept is created (§4.1).

### Precondition: deterministic UUIDs from seed (status quo)

The bundle generator (`src/generators.py`) derives every UUID deterministically from a `(kind, name)` seed via `make_uuid(seed) → uuid5(NS, seed)`. This is the existing behavior and stays unchanged. Same source `.xlsx` produces identical UUIDs across runs, so regenerations remain idempotent.

Implication for editing: the form-element UUID and the field name are tied together. A rename changes the seed, which changes the UUID. The editor handles this via **void-then-add** for renames (§4.2), keeping the bundle's UUIDs consistent with their names at all times.

---

## 3. The Bundle as the Source of Truth

A bundle ZIP contains everything we need for field-level edits:

```
forms/<FormName>_<uuid>.json   ← form elements (one per field) + concept refs
concepts.json                   ← read-only for rename/remove; appended to by field.add
```

Every other file (`subjectTypes.json`, `programs.json`, `formMappings.json`, …) is unaffected by a field edit. We do not touch them.

### 3.1 What we read out of the bundle

For each form, we know:
- Form name (the `name` top-level key).
- Form UUID (`uuid`).
- Section names and group UUIDs (`formElementGroups[].name`, `…[].uuid`).
- Field names, form-element UUIDs, and concept refs (`formElementGroups[].formElements[].{name, uuid, concept.uuid, concept.name, concept.dataType}`).
- Field properties (`mandatory`, `keyValues`, `concept.answers`, …).

From `concepts.json` we read each concept's `uuid` only — to know whether a concept already exists when we're adding a field. We never modify, rename, or remove a concept entry.

### 3.2 What we don't have

- The original `FormSpec` / `EntitySpec`. We never reconstruct them.
- Skip-logic intent, original field ordering rationale, mapping to source rows. Irrelevant for field edits.

---

## 4. Edit Operation Surface

Three operation kinds, all addressed by `(form, section, field)`:

### 4.1 `field.add`

```jsonc
{
  "op_id": "op-1",
  "kind": "field.add",
  "target": { "form": "ANC", "section": "Vitals" },
  "payload": {
    "name": "Blood Pressure",
    "dataType": "Numeric",         // Text | Numeric | Date | Coded | Notes | …
    "mandatory": false,            // optional, default false
    "unit": "mmHg",                // optional (Numeric)
    "min": 40,                     // optional (Numeric)
    "max": 250,                    // optional (Numeric)
    "options": ["Yes", "No"],      // required for Coded
    "selectionType": "SingleSelect", // optional for Coded
    "position": 3                  // optional 1-based displayOrder; appended if omitted
  }
}
```

Effect:
- Compute the form-element UUID deterministically: `uuid5(NS, f"formElement:{form_name}:{field_name}")`. Look for an existing element with that UUID in the target section.
  - **If found and voided** — reinstate: set `voided` back to `false`, update properties from the payload (`dataType`, `mandatory`, `keyValues`, `concept.answers`), keep its `displayOrder`. UUID is preserved end-to-end, so the original server record reactivates.
  - **If found and live** — duplicate-name conflict; reject with `"duplicate_name"`.
  - **If not found** — append a new form element with the computed UUID, `voided: false`, and `displayOrder = max(existing displayOrder) + 1` (counting voided elements).
- Compute the concept UUID `uuid5(NS, f"concept:{field_name}")`. If not already in `concepts.json`, append a new entry (with answers for Coded). If present, reuse — do not modify.

This is the **only** operation that may write to `concepts.json`, and it only ever appends.

### 4.2 `field.rename`

```jsonc
{
  "op_id": "op-2",
  "kind": "field.rename",
  "target": { "form": "ANC", "section": "Vitals", "field": "BP" },
  "payload": { "new_name": "Blood Pressure" }
}
```

Effect — rename is implemented as **void-then-add**, atomically:

1. Locate the live form element by `(form, section, field)`.
2. Capture its `displayOrder` and field properties (`dataType`, `mandatory`, `keyValues`, `concept.answers`).
3. Set `voided: true` on it. Its UUID, name, and `displayOrder` stay as-is.
4. Run the `field.add` logic for `new_name`:
   - Compute `new_uuid = uuid5(NS, f"formElement:{form_name}:{new_name}")`.
   - If an element with that UUID exists and is voided (the user is renaming back to a previously-removed name), un-void it. The original record reactivates with all its existing observations.
   - Otherwise insert a new form element at the **original element's displayOrder** so the rename appears in place visually. Live elements with `displayOrder ≥ original` shift down by 1.
5. Carry the captured field properties onto the new element.
6. Compute `new_concept_uuid = uuid5(NS, f"concept:{new_name}")`. If not in `concepts.json`, append it; otherwise reuse.

Why void-then-add: deterministic UUIDs tie name → UUID, so the new name must have its own UUID. Keeping name and UUID consistent for every live element means future lookups always resolve cleanly via the same scheme. The server-side cost is that observations against the old form element stay attached to the (now voided) record — structurally a `field.remove` + `field.add`.

### 4.3 `field.remove`

```jsonc
{
  "op_id": "op-3",
  "kind": "field.remove",
  "target": { "form": "ANC", "section": "Vitals", "field": "BP" }
}
```

Effect:
- Locate the form element by `(form, section, field)`.
- Set `formElement.voided = true` in the form file. **The element is not removed from the JSON.**
- `displayOrder` is left as-is (the entry stays in place; the server uses `voided` to suppress it from the UI).
- `concepts.json` is **not touched**.

Why void rather than delete: the bundle has typically already been uploaded, so the form element exists in Avni's database with the same UUID. Deleting it from the bundle would re-upload as a no-op (Avni's upsert wouldn't see the field at all) and the field would remain live on the server. Setting `voided: true` is Avni's soft-delete signal — on re-upload the server marks the corresponding record voided, the mobile clients sync the change, and the field disappears from the form everywhere.

---

## 5. Identification & Matching

All matching is **exact** after a light normalization (case-folded, whitespace-stripped). The matcher does no fuzzy / approximate matching — `"BP"` will not match `"B.P."`. If the user's input doesn't match a stored name exactly, the op is rejected with `"not_found"` and the agent re-prompts the user (using `list_bundle_fields` to ground on real names).

| Lookup | Used for | Key |
|---|---|---|
| Form | all ops | `form.name.strip().lower()` |
| Section | all ops | `formElementGroup.name.strip().lower()` |
| Field (live) | `field.rename`, `field.remove` | `formElement.name.strip().lower()` where `voided == false` |
| Field (reinstate) | `field.add` | element with UUID `= uuid5(NS, f"formElement:{form_name}:{field_name}")` where `voided == true` |

The reinstate lookup uses the deterministic UUID rather than a name match. Under the deterministic-UUID scheme, the UUID derived from a given name is canonical — so re-adding `"BP"` always finds the originally-removed `"BP"` element regardless of what its `name` string currently reads.

If a target string matches more than one entity (e.g. two sections with the same name in the same form — an existing bundle defect), the editor rejects the op with `"ambiguous_target"`. The user disambiguates by passing the UUID directly in `target` (`"form_uuid": "..."` / `"section_uuid": "..."` / `"field_uuid": "..."`).

Fuzzy / approximate matching is out of scope for this iteration. If users find exact match too brittle in practice, revisit by adding `_fuzzy_match` from `src/parser.py` as a fallback pass — already used by `make_form_mappings` — with a strict threshold and resolution warnings.

---

## 6. UUID Strategy

All UUIDs are deterministic — `uuid5(NS, seed)` over the seeds defined in `generators.py`. The editor uses the same scheme; for any field operation, the seed pattern is:

| Thing | Seed |
|---|---|
| Form element | `formElement:<formName>:<fieldName>` |
| Concept (question) | `concept:<fieldName>` |
| Answer concept (Coded option) | `concept:<optionName>` |

### 6.1 Adds

Compute the deterministic UUID from the new field name. If a voided element with that UUID already exists, reinstate it (preserving server-side observation history). Otherwise insert a new element with the computed UUID.

### 6.2 Renames

UUIDs are **not preserved**. A rename voids the old element (UUID derived from the old name) and adds a new element (UUID derived from the new name). Name and UUID stay aligned for every live element at all times, so subsequent lookups resolve via the same deterministic scheme without rename history.

The server-side cost: observations against the old form element stay linked to the (now voided) record; new observations after the rename go against the new record. Structurally identical to `field.remove` + `field.add`.

### 6.3 Removes

The form element stays in the form file with its UUID intact; only `voided` flips to `true`. `concepts.json` is unchanged.

Because the UUID derivation is deterministic, a later `field.add` for the same `(form, field name)` re-derives the same UUID, finds the voided element, and reinstates it — the server reactivates the original record rather than creating a duplicate.

---

## 7. No Cascade for Rename / Remove

Because concepts are never modified or removed by the editor, there is **no cross-form cascade to analyse**:

- A renamed field changes one form's display name only. Other forms that share the same concept UUID are not affected — `concepts.json` is unchanged, so their view of the concept's name is unchanged.
- A removed field drops one form element. The shared concept stays. Other forms that reference it continue to work.
- An added field may append a concept entry to `concepts.json`, but never modifies an existing one. So any other form referencing that concept UUID (if it was already there) still resolves correctly.

The concept usage index, fork-vs-cascade decisions, `scope` flag, and `prune_orphan_concept` flag from earlier drafts are removed.

---

## 8. Architecture

A single module `src/bundle_editor.py` exposing one entry point:

```python
def apply_field_edits(
    bundle_path: str,
    operations: list[dict],
) -> EditResult:
    """Apply field-level edits to a bundle ZIP in place.

    Steps:
      1. Resolve `bundle_path` (ZIP file or already-unpacked directory).
      2. If ZIP: extract to a temp working dir.
      3. Load every form JSON + concepts.json.
      4. Parse and validate operations (Pydantic).
      5. Apply each op against the in-memory dicts; record per-op result.
      6. Validate post-state (no duplicate form-element UUIDs within a
         form; section displayOrders contiguous; field.add concept
         entries either pre-existed or were appended exactly once).
      7. Write modified JSON files back to the working dir.
      8. Re-zip into <bundle_path> using the canonical order from
         pipeline._CANONICAL_ORDER (single source of truth — imported,
         not copied).
      9. Return an EditResult.
    """
```

No LangGraph, no checkpointer — the flow is linear and atomic. If step 6 (validation) fails, no files are written and no ZIP is touched; the caller gets an `EditResult` with rejections.

### 8.1 `EditResult` shape

```python
class EditResult(BaseModel):
    bundle_path: str
    applied: list[str]                  # op_ids applied
    rejected: list[dict]                # [{"op_id":..., "reason":...}]
    warnings: list[str]
    forms_modified: list[str]           # form file names rewritten
    form_elements_added: int            # net-new (no prior voided UUID)
    form_elements_reinstated: int       # field.add that un-voided a prior element
    form_elements_voided: int           # field.remove
    form_elements_renamed: int          # field.rename
    concepts_appended: int              # only non-zero from field.add
```

### 8.2 Concurrency / safety

- All writes happen in a temp working directory; the original ZIP is only overwritten on successful validation. A failed edit leaves the bundle untouched.
- No two operations may target the same `(form, section, field)` in a single batch — the validator rejects the second.

---

## 9. Agent Tool Surface (`src/chat.py`)

One new tool:

```python
@tool
def edit_bundle_fields(bundle_path: str, operations: list[dict]) -> dict:
    """Add, rename, or remove fields in an existing bundle ZIP.

    Returns one of:
      {"status": "done",  "result": <EditResult>}
      {"status": "error", "error": "..."}

    The agent should:
      - Read the user's intent and construct a typed `operations` list.
      - Report back which forms were modified and any warnings.
    """
```

### 9.1 Helper for grounding

So the agent can refer to real form/section/field names rather than guessing:

```python
@tool
def list_bundle_fields(bundle_path: str) -> dict:
    """Return a compact summary of every form in the bundle:
      [{"form": "...", "sections": [{"name": "...", "fields": [...]}]}, ...]
    Use this before constructing edits.
    """
```

### 9.2 System prompt addendum

> If the user asks to add, rename, or remove a field in an existing bundle, ask for the bundle path (or use the most recent one mentioned in this thread). Call `list_bundle_fields(bundle_path)` first so your operations refer to real form/section/field names. Build a typed `operations` list and call `edit_bundle_fields`. Report which forms changed and surface any warnings or rejections verbatim.

---

## 10. Validation

Two layers:

### 10.1 Pre-apply (schema)

- Operation kind is one of the three supported.
- `target` has the required keys for the kind (`field.add` needs `form` + `section`; rename/remove need `form` + `section` + `field`).
- Payload types match (e.g. `dataType` is a known string; `options` is a list when `dataType == "Coded"`).
- Referenced form, section, and (for rename/remove) field resolve exactly via §5. A `"not_found"` or `"ambiguous_target"` resolution rejects the op.
- For `field.add`: a **live** field with the same normalized name does not already exist in the target section. (A voided element with the matching name triggers reinstate, §4.1.)

Per-op failures are recorded in `EditResult.rejected` and **do not abort the batch** — other ops still apply.

### 10.2 Post-apply (integrity)

After all ops mutate the in-memory dicts:
- No duplicate `formElement.uuid` within a form (counting voided elements).
- No duplicate `concept.uuid` within `concepts.json`.
- `displayOrder` values within a section are unique (gaps are allowed because voided elements keep their original `displayOrder`; nothing in the bundle requires 1-based contiguity).

We do **not** check that every form-element concept ref resolves to a `concepts.json` entry, because rename/remove guarantee they continue to resolve (we don't change concept UUIDs), and `field.add` only ever adds concept entries if needed. The only way to break that invariant would be a bug in the editor itself.

If any check fails, the whole batch is aborted (no ZIP overwritten). The failure is reported in `EditResult.rejected` with kind `"integrity"`.

---

## 11. Worked Examples

### 11.1 Add a field

User: *"Add a 'Blood Pressure' Numeric field with units mmHg, min 40 max 250, in ANC → Vitals."*

```jsonc
[
  { "op_id": "op-1", "kind": "field.add",
    "target": { "form": "ANC", "section": "Vitals" },
    "payload": { "name": "Blood Pressure", "dataType": "Numeric",
                 "unit": "mmHg", "min": 40, "max": 250 } }
]
```

- New form element UUID = `uuid5(NS, "formElement:ANC:Blood Pressure")`.
- New concept UUID = `uuid5(NS, "concept:Blood Pressure")`. Appended to `concepts.json` if not already present.
- Appended at the end of `Vitals` with `displayOrder = N+1`.

### 11.2 Rename a field

User: *"Rename 'BP' in ANC → Vitals to 'Blood Pressure'."*

```jsonc
[
  { "op_id": "op-1", "kind": "field.rename",
    "target": { "form": "ANC", "section": "Vitals", "field": "BP" },
    "payload": { "new_name": "Blood Pressure" } }
]
```

- Locates the live `"BP"` form element. Captures its `displayOrder` (say `4`), `dataType`, `mandatory`, `keyValues`.
- Sets `voided: true` on it. UUID = `uuid5(NS, "formElement:ANC:BP")` and `displayOrder=4` stay as-is.
- Computes `new_uuid = uuid5(NS, "formElement:ANC:Blood Pressure")`. No existing element has that UUID, so a new form element is inserted at `displayOrder=4` with the captured properties. Live elements at order ≥ 4 shift down by 1.
- Computes `new_concept_uuid = uuid5(NS, "concept:Blood Pressure")`. Not in `concepts.json`, so appended.
- On re-upload: Avni voids the original form-element record and creates a new one for `"Blood Pressure"`. Past observations stay linked to the voided record.

### 11.3 Remove a field

User: *"Remove 'LMP Date' from Pregnancy Enrolment → Vitals."*

```jsonc
[
  { "op_id": "op-1", "kind": "field.remove",
    "target": { "form": "Pregnancy Enrolment",
                "section": "Vitals", "field": "LMP Date" } }
]
```

- Locates the form element; sets `voided: true` on it. The element stays in the form JSON; its UUID and `displayOrder` are preserved.
- `concepts.json` is unchanged.
- On re-upload, Avni soft-deletes the corresponding form-element record.

---

## 12. Open Questions

- **Observation history during rename.** Under the void-then-add model, observations captured against the original form element stay linked to the (now voided) record on the server; new observations after the rename go against the new record. If a downstream report needs to combine both, it has to join the two form elements — knowing only that one is a rename of the other isn't recorded anywhere. If preserving observation continuity matters, this approach needs to be reconsidered.
- **Same field name in two sections of the same form.** The form-element UUID seed (`formElement:<formName>:<fieldName>`) doesn't include the section, so two adds with the same field name in different sections would produce the same UUID. The integrity check catches this and the second op fails; the user picks a different name (or qualifies it by section, e.g. `"Visit Date (Vitals)"`). Worth flagging in the agent's system prompt.

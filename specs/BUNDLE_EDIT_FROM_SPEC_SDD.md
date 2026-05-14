# Bundle Edit from Spec — Software Design Document

## 1. Objective

When the source `.xlsx` for an org has been edited (fields added, removed, reordered) AND a previously-generated bundle already exists, **update the existing bundle in place** to reflect those changes — preserving the UUIDs of fields that survived the edit and voiding fields that were removed.

The alternative — regenerating the bundle from scratch — produces a fresh bundle with new UUIDs for any renamed or reordered fields, which on re-upload would orphan the corresponding records on the Avni server (Avni upserts by UUID). Edit-from-spec avoids that by treating the existing bundle as the canonical UUID source and applying diffs to it.

In scope:
- **Add** a new field in Excel → append it to the bundle's form file; create a new concept entry if needed.
- **Remove** a field in Excel → set `voided: true` on the corresponding form element; keep its UUID and `displayOrder` intact.
- **Reorder** fields in Excel → update `displayOrder` values to match the new sequence; UUIDs unchanged.

Out of scope:
- Section / form / subject-type / program / encounter-type changes. Sections and forms must exist in both the bundle and the spec; their internal field set may change. Reshaping the entity hierarchy itself uses the editor or a fresh generation.
- Renames in the strict sense (same field, new name). Renames in Excel manifest as `remove + add` after this edit-from-spec run (the renamed field gets a new UUID derived from the new name; the old UUID is voided). If observation continuity matters across a rename, use the chat-driven `field.rename` op in `bundle_editor.py` instead.
- Field property updates (changing `dataType`, `mandatory`, `min`/`max`, etc.) on a surviving field. Edit-from-spec detects identity by UUID; if the UUID stayed the same, the field is treated as unchanged. Property edits go through the existing chat editor.

---

## 2. Trigger

Edit-from-spec is **explicit**, invoked through a new chat tool (`edit_bundle_from_spec`) or a CLI entry point (`avni-edit-from-spec <org>`). Not automatic on `generate_bundle`, because the safe default for a never-uploaded bundle is still a fresh generation.

Decision rule the user faces:

| Situation | Action |
|---|---|
| No bundle yet for this org | `generate_bundle` — fresh build |
| Bundle exists, no source change | (nothing to do) |
| Bundle exists, source has only field-level edits | `edit_bundle_from_spec` — this SDD |
| Bundle exists, source restructured (forms/sections changed) | Manual reconciliation or fresh generation + manual UUID-preservation work |

---

## 3. Architecture

Edit-from-spec **reuses the existing generation pipeline** for its first four nodes — parsing and LLM enrichment with the same human-in-the-loop confirmation flow — then branches into edit-from-spec-specific downstream nodes that diff against the existing bundle and apply edits.

### 3.1 Why the pipeline (not a plain tool call)

Parsing an Excel surfaces the same data-quality issues for edit-from-spec as for generation:
- A field name might exceed 255 characters (DB column limit on the Avni server).
- The same field name might appear twice within a form.

The LLM enrichment pass (`pipeline.nodes.enrich_with_llm`) proposes renames for these; the user confirms each via `apply_user_decisions` → `interrupt()`. Skipping this step for edit-from-spec would mean either silently truncating (data loss) or failing on import (unhappy user). So edit-from-spec must go through the same pause-and-confirm path.

That makes edit-from-spec genuinely a multi-step graph with a HITL pause — exactly the shape LangGraph is for.

### 3.2 Shared pipeline; diff happens on bundle-shaped JSON

A new `mode` field on `BundleState` (`"generate"` | `"edit_from_spec"`) selects which terminal step runs. **Both modes share the full generation chain through `generate_form_mappings`** — they both fully realize the desired bundle in memory. Edit-from-spec then diffs that desired bundle against the existing one on disk; generate writes the desired bundle out as fresh.

```
discover_files → parse_documents → enrich_with_llm → apply_user_decisions
                                                            │
                                                            ▼
                              generate_entities → generate_forms → generate_form_mappings
                                                            │   (shared by both modes;
                                                            │    desired bundle is now
                                                            ▼    in `state`)
                                                  _route_after_generation
                                                   /                  \
                                       mode="generate"             mode="edit_from_spec"
                                              ▼                       ▼
                                       package_zip            diff_against_bundle
                                              ▼                       ▼
                                             END               apply_diff_edits
                                                                      ▼
                                                                     END
```

`apply_diff_edits` is intentionally a single thin node: it calls
`bundle_editor.apply_field_edits(bundle_path, ops)`, which loads the bundle,
applies the ops, runs the integrity check, writes the modified JSONs, and
atomically re-zips. Splitting the apply step from the re-zip would either
duplicate the editor's logic or require restructuring it; collapsing them
into one node keeps the editor as the single source of truth for "modify an
existing bundle on disk".

Why the diff sits this far down: the pipeline's generator nodes are the canonical "EntitySpec → bundle JSON" function. Anything that can affect UUIDs, concept entries, or form structure (deterministic UUID seeding, concept name truncation, auto-generated cancellation forms, the concept registry) happens inside those nodes. By running them in edit-from-spec mode too, the **desired bundle is exactly what `generate_bundle` would have produced** — so the diff is bundle vs. bundle, not EntitySpec vs. bundle.

That keeps the diff function simple: it walks two like-shaped dict structures and matches form elements by UUID. UUID derivation logic lives only in `generators.py`; the diff treats UUIDs as opaque IDs.

### 3.3 New artefacts

| File | Change |
|---|---|
| `domain/diff.py` | **New.** Pure function `diff(desired_bundle, current_bundle) → list[EditOperation]`. Both sides are bundle-shaped dicts: `{"forms": [{file_name, content}], "concepts": [...]}`. No I/O, no LangGraph. |
| `domain/bundle_editor.py` | Add `section.reorder_fields` op kind (§6). Expose a helper `load_bundle_snapshot(bundle_path) → dict` for the diff node to call. |
| `pipeline/state.py` | Add `mode`, `bundle_path`, `diff_ops`, `edit_result` to `BundleState`. |
| `pipeline/nodes.py` | Add two new nodes: `diff_against_bundle` (compute the op list) and `apply_diff_edits` (call `apply_field_edits`, which handles apply + atomic re-zip internally). |
| `pipeline/graph.py` | Add the conditional edge `_route_after_generation` and wire the edit-from-spec branch after `generate_form_mappings`. |
| `chat/tools.py` | Add `@tool edit_bundle_from_spec(org)`. Reuse the existing `resume_bundle` for HITL resume (same graph, same thread). |
| `models.py` | Extend `EditOpKind` with `"section.reorder_fields"`. |
| `chat/prompts.py` | Teach the agent when to offer edit-from-spec vs. fresh generate. |

---

## 4. Identification: "same field" across edit-from-spec

Both sides of the diff are bundle JSON. A form element in the **desired bundle** and a form element in the **current bundle** are considered the same field iff their `uuid` fields are equal.

The desired bundle's UUIDs come from `generators.py`'s deterministic seed:
```
uuid5(NAMESPACE, f"formElement:{form_name}:{field_name}")
```
That's run as part of the pipeline's generation nodes (upstream of the diff node), so by the time the diff runs both sides have real UUIDs already attached. The diff just compares them.

Cases:
- Same UUID, both bundles → matched, treated as surviving.
- UUID only in desired → ADD.
- UUID only in current (and currently live) → REMOVE (void).
- UUID in desired, currently voided in current → REINSTATE (un-void).

Renames in Excel change the field name, which changes the seed, which produces a different UUID — so they show up as `(desired-only) + (current-only-live)` = add + remove. That's intentional — see §1 "Out of scope".

---

## 5. Diff Algorithm (`domain/diff.py`)

Inputs:
- `desired_bundle: dict` — `{"forms": [{file_name, content}], "concepts": [...]}`. Produced upstream by `generate_forms` / `generate_form_mappings` in the pipeline.
- `current_bundle: dict` — same shape, loaded from the existing ZIP via `bundle_editor.load_bundle_snapshot`.

For each form **present in both** (matched by form UUID):

1. Walk both `formElementGroups` and index form elements by UUID, scoped to each section.

   ```
   desired_by_uuid : {form_element_uuid: (formElement_dict, section_name, position)}
   current_by_uuid : {form_element_uuid: (formElement_dict, section_name, voided?, displayOrder)}
   ```

2. Compute three sets per section:

   | Set | Definition | Emits |
   |---|---|---|
   | `desired - current` | UUID in desired bundle, not in current | `field.add` op (payload extracted from the desired formElement dict — name, dataType, answers, keyValues) |
   | `desired ∩ current_voided` | UUID in desired bundle, currently voided in current | `field.add` op (reinstate path in the editor will un-void) |
   | `current_live - desired` | UUID live in current, not in desired | `field.remove` op |
   | `desired ∩ current_live, position changed` | Same UUID both places, but its position in desired's `formElements` differs from its `displayOrder` in current | `section.reorder_fields` op for the whole section |

3. Forms / sections not in both → warning, no ops.

The diff function walks **only dicts on both sides** — no `EntitySpec`, no `FieldSpec`. It treats UUIDs as opaque match keys; UUID derivation lives in `generators.py` and is already done by the time the diff runs.

Output: a typed `list[EditOperation]` consumable by `apply_field_edits`.

---

## 6. New Op Kind: `field.reorder`

The existing editor (§4 of `BUNDLE_EDITING_SDD.md`) supports `field.add` / `field.rename` / `field.remove`. Edit-from-spec needs one more kind so that pure reorderings can be expressed without remove+add (which would change UUIDs).

### 6.1 Payload

```jsonc
{
  "op_id": "op-1",
  "kind": "section.reorder_fields",
  "target": { "form": "ANC", "section": "Vitals" },
  "payload": {
    "order": [
      "Date of Visit",
      "Blood Pressure",
      "Weight",
      "Hemoglobin"
    ]
  }
}
```

Named `section.reorder_fields` rather than `field.reorder` because it's a per-section operation (the order list scopes to one section).

### 6.2 Semantics

- The `order` list contains field names in their desired sequence.
- Each name resolves to a live `formElement` in the target section (exact match, case-folded + whitespace-stripped, as elsewhere).
- The editor assigns `displayOrder = 1..N` to those elements in the listed order. Voided elements are **not** included in `order` and **retain their existing `displayOrder`** (which may now overlap with live ones; that's allowed because the integrity check only enforces uniqueness among live elements).
- Any live field not present in `order` is a validation error (`schema` reject) — every live field must have an assigned position.

### 6.3 Integration with `apply_field_edits`

A new branch in `_apply_one` handles the kind:

```python
elif op.kind == "section.reorder_fields":
    _apply_reorder(op, section, result)
```

`_apply_reorder` validates the order list against the section's live elements, then walks the order assigning `displayOrder` values. No UUIDs touched, no concepts modified.

### 6.4 Integrity check addition

Already covered by the existing rule "displayOrder values within a section are unique among live elements" — a buggy reorder would trip it.

---

## 7. `edit_bundle_from_spec` Tool (`chat/tools.py`)

The tool kicks off the shared pipeline with `mode="edit_from_spec"`. It uses the same `_pipeline_graph` and the same `_run_with_interrupt_handling` helper as `generate_bundle`, so resume goes through the existing `resume_bundle` tool — no second resume entry point.

```python
@tool
def edit_bundle_from_spec(org: str, user_instructions: str | None = None) -> dict:
    """Edit an already-generated bundle from the current source .xlsx (the spec).

    Reads resources/input/<org>/*.xlsx, re-runs the deterministic parser
    and the LLM enrichment pass (which may pause for user confirmation
    of long-name shortenings or duplicate-field disambiguations — same
    as generate_bundle), then diffs the refined EntitySpec against
    resources/output/<org>/<Org>.zip and applies the resulting
    field-level edits (add / remove / reorder).

    Field UUIDs are preserved across the edit. New fields are appended
    with deterministic UUIDs (same scheme as the generator); removed
    fields are voided so Avni soft-deletes the records on re-upload.

    May return:
      {"status": "done", "result": <EditResult>}
      {"status": "needs_confirmation", "thread_id": ..., "payload": {...}}
      {"status": "error", "error": ...}

    On "needs_confirmation", call resume_bundle(thread_id, resolutions)
    — the same tool used to resume a paused generate_bundle.
    """
```

### 7.1 Flow

1. Resolve `input_dir = resources/input/<org>/` and `bundle_path = resources/output/<org>/<Org>.zip`.
2. Reject if either is missing.
3. Build the initial state with `mode="edit_from_spec"`, `bundle_path` set, the usual `org_name`, `input_dir`, `output_dir`, `user_instructions`.
4. Run the pipeline via the existing `_run_with_interrupt_handling` helper. If LLM enrichment proposes any renames, the pipeline pauses at `apply_user_decisions` and the tool returns `needs_confirmation`. Same shape as `generate_bundle`.
5. On resume (via `resume_bundle(thread_id, resolutions)`), the graph applies the confirmed Changes to the `EntitySpec`, runs the shared generation nodes (`generate_entities → generate_forms → generate_form_mappings`), then routes via `_route_after_generation` into the edit-from-spec branch:
   - `diff_against_bundle` — load the existing bundle, compute the op list.
   - `apply_diff_edits` — call `bundle_editor.apply_field_edits`, which handles apply + integrity check + atomic re-zip.
6. Final state's `edit_result` (an `EditResult`) is returned in the `done` payload.

### 7.2 The agent uses one resume tool, not two

`resume_bundle(thread_id, resolutions)` already resumes whatever paused graph corresponds to that thread_id. Edit-from-spec runs on the same pipeline graph with the same checkpointer, so it resumes through the same tool. The agent doesn't need to know whether the original call was `generate_bundle` or `edit_bundle_from_spec` — just feed `thread_id` and resolutions back.

### 7.3 System prompt addition

The `chat.prompts.SYSTEM_PROMPT` gets one paragraph:

> If the user mentions editing the source Excel for an org that already has a bundle (e.g. "I added a field to Srijan's forms.xlsx", "I removed BP from the Ekam scoping doc"), prefer `edit_bundle_from_spec(org)` over `generate_bundle(org)`. Edit-from-spec preserves Avni-server record UUIDs by voiding removed fields rather than dropping them, and only emits diff ops for what actually changed. Generation builds the bundle fresh and would create new records on re-upload for any field whose name changed. After `edit_bundle_from_spec`, the same `resume_bundle` tool handles any LLM-enrichment confirmation pauses.

---

## 8. Edge Cases

### 8.1 Excel section ordering vs displayOrder

The spec's section list order (which sections come first) is **not** synced — only the field order within each section. Reordering whole sections is a section-level op and out of scope here.

### 8.2 New section in Excel

A section that exists in the spec but not in the bundle's form file is reported as a warning. No `section.add` op is emitted because the bundle_editor doesn't support adding sections yet. The user either runs a fresh generation or manually edits the form JSON.

### 8.3 New form in Excel

Same as 8.2 — warning, no op. Adding a new form requires creating a new file under `forms/`, a new entry in `formMappings.json`, and possibly cancellation forms. Out of scope.

### 8.4 Form/section deleted from Excel

Also a warning. The bundle still has the form/section; edit-from-spec does not void entire forms or sections. The user runs `field.remove` per field manually if they want to retire content via this path.

### 8.5 Field that surfaces as both "add" and "remove" because of a rename

This is the rename-in-Excel case. Edit-from-spec emits both ops; the new field gets a new UUID; the old field gets voided. The user is told explicitly in the `EditResult` warnings: *"renamed `BP` → `Blood Pressure`? old form-element will be voided. Use chat editor's field.rename to preserve observation continuity."*

(Detection: if two ops in the same section both arise — one `add` and one `remove` — and the **dataType** matches, the diff annotates them as a likely rename pair via a warning. Doesn't alter the ops themselves — that would couple the two and the user might genuinely have meant "delete X and add unrelated Y".)

### 8.6 Idempotent re-run

Running edit-from-spec twice on an unchanged Excel + bundle yields zero ops. The diff returns an empty list; `apply_field_edits` is a no-op; no files written.

### 8.7 Bundle has voided fields the Excel still references

The voided form element's UUID matches a desired field in the spec → `field.add` reinstates it (existing reinstate path in §4.1 of `BUNDLE_EDITING_SDD.md`). No new concept appended (concept UUID already in `concepts.json`).

---

## 9. UUID & Identity

| Operation | UUID behavior |
|---|---|
| Surviving field (same name in spec + bundle) | Preserved — no change. Same form-element UUID, same concept UUID. |
| Reordered field | Preserved. Only `displayOrder` updates. |
| New field | Deterministic UUID from `formElement:<form>:<name>` and `concept:<name>` — matches what a from-scratch `generators.py` would emit. |
| Removed field | Preserved on the voided element. The record stays in the form JSON. |
| Reinstated (was voided, now in spec) | Reused — the un-void path keeps the original UUID. |

This means re-uploads after edit-from-spec are clean Avni upserts: surviving records are updated in place; new records get created with fresh UUIDs; removed records get soft-deleted via `voided`.

---

## 10. Validation

### 10.1 Pre-apply

- Both `resources/input/<org>/` and the existing bundle ZIP exist; otherwise abort with a clear error.
- The parsed `EntitySpec` has at least one form; otherwise abort.
- Every form in `EntitySpec` matches a form file in the bundle (by name → UUID). Mismatches → warning, not error.

### 10.2 Post-apply

Same integrity checks as `apply_field_edits` (no duplicate form-element UUIDs, displayOrder unique among live). Failure aborts and leaves the bundle untouched.

### 10.3 Diff sanity check

Before writing, log a one-line summary per op so the user sees what's about to happen:

```
[edit-from-spec] op-1 field.add    ANC > Vitals > 'BP Diastolic' (Numeric)
[edit-from-spec] op-2 field.remove ANC > Vitals > 'Old Field'
[edit-from-spec] op-3 section.reorder_fields ANC > Vitals — 14 fields
```

---

## 11. Worked Example

Starting state:
- `Ekam.zip` contains `ANC > Vitals` with fields: `Date of Visit`, `BP`, `Weight`, `Hemoglobin`.
- User edits `Ekam.xlsx`: removes `BP`, adds `Pulse`, reorders to `[Date of Visit, Weight, Hemoglobin, Pulse]`, **and** the new `Pulse` field's name in the spreadsheet accidentally got typed as a 280-character sentence.

`edit_bundle_from_spec("ekam")` runs:

1. **`discover_files` + `parse_documents`** → `EntitySpec` with section `Vitals` = `[Date of Visit, Weight, Hemoglobin, <280-char name>]`.
2. **`enrich_with_llm`** flags the long name; LLM proposes shortening to `"Pulse"`. Stores it as a pending `Change`.
3. **`apply_user_decisions`** calls `interrupt(...)`. The tool returns `needs_confirmation` to the agent, which shows the proposal to the user. **Pause point.**
4. User replies `yes`. Agent calls `resume_bundle(thread_id, {"change-1": "yes"})`.
5. Graph resumes: confirmed Change is applied to the `EntitySpec`. The field's name in the spec is now `"Pulse"`.
6. **`generate_entities` → `generate_forms` → `generate_form_mappings`** run. State now holds the **desired bundle** in memory: `state["forms_json"]` and `state["concepts_json"]` are exactly what a fresh `generate_bundle` would have produced.
7. `_route_after_generation` sees `mode == "edit_from_spec"` and routes into the edit-from-spec branch.
8. **`diff_against_bundle`** loads `Ekam.zip` from disk, then calls `diff(state["forms_json"] + state["concepts_json"], current_bundle)`. Both inputs are bundle-shaped dicts. Output:
   ```
   op-1  field.remove                ANC > Vitals > 'BP'
   op-2  field.add                   ANC > Vitals > 'Pulse' (Numeric)
   op-3  section.reorder_fields      ANC > Vitals
                                     order=[Date of Visit, Weight, Hemoglobin, Pulse]
   ```
9. **`apply_diff_edits`** runs the three ops via `bundle_editor.apply_field_edits`, which handles the full lifecycle:
   - Apply: `BP` form element → `voided: true` (UUID preserved); `Pulse` appended at `displayOrder = 5` (max+1 over live AND voided); reorder assigns `displayOrder = 1, 2, 3, 4` to the four live fields.
   - Integrity check passes (no dup UUIDs, no dup displayOrder among live).
   - Write modified form JSON and updated `concepts.json` into the working directory.
   - Atomically repackage `Ekam.zip` (write `Ekam.zip.tmp`, then `shutil.move`).

Result: `Ekam.zip` is updated in place. On re-upload, Avni voids the `BP` record, creates `Pulse`, and updates the displayOrder of the surviving records. No orphan records.

If there had been no long-name or duplicate issue in the Excel, step 3 would have skipped the pause and the graph would have flowed straight through `apply_user_decisions` to `diff_against_bundle`. The pipeline behaves identically to `generate_bundle` for the first four nodes; only the downstream diverges.

---

## 12. Open Questions

- **Tracking renames as a first-class op.** Today renames look like add+remove and `EditResult.warnings` flags them. If observation continuity across renames is important, the edit-from-spec tool could prompt the user (via an `interrupt()` if running through the chat agent) to confirm each suspected rename pair, then emit a single `field.rename` op instead of the add/remove pair. Adds HITL complexity; defer until users actually hit the problem.
- **Section / form propagation.** If users want to grow the bundle from Excel beyond just fields, the bundle_editor needs `section.add` / `form.add` ops. Doable; out of scope for v1.
- **Snapshot before apply.** Currently `apply_field_edits` overwrites the original ZIP. For an edit-from-spec run — which could touch many fields at once — keeping a `<Org>.zip.bak` before applying might be worth it as a safety net. Cheap; consider adding.

---

## 13. Cross-reference

The `section.reorder_fields` op kind introduced in §6 should also be reflected in `specs/BUNDLE_EDITING_SDD.md` (§4 op surface table). The editor SDD is the canonical reference for the op semantics; this SDD is the consumer.

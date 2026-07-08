"""
Bundle field editor — applies field-level edits (add / rename / remove) to a
generated bundle ZIP in place. See specs/BUNDLE_EDITING_SDD.md.

Entry point: `apply_field_edits(bundle_path, operations) -> EditResult`.

The bundle is the source of truth; the editor never re-parses .xlsx source
documents and never consults an EntitySpec. UUIDs are derived deterministically
from names via the same scheme as src/generators.py, so renames are implemented
as void-then-add (the new name has a different UUID).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from typing import Any

from pydantic import ValidationError

from domain.generators import make_uuid
from models import EditOperation, EditResult, RejectedOp

log = logging.getLogger(__name__)


_FLAT_FILES = [
    "addressLevelTypes.json",
    "subjectTypes.json",
    "operationalSubjectTypes.json",
    "encounterTypes.json",
    "operationalEncounterTypes.json",
    "programs.json",
    "operationalPrograms.json",
    "concepts.json",
    "formMappings.json",
    "organisationConfig.json",
]


# ── Public entry point ────────────────────────────────────────────────────────


def apply_field_edits(bundle_path: str, operations: list[dict]) -> EditResult:
    """Apply field-level edits to a bundle ZIP (or unpacked directory) in place.

    On any failed integrity check the source ZIP is left untouched.
    """
    result = EditResult(bundle_path=bundle_path)

    with _open_bundle(bundle_path) as workdir:
        forms = _load_forms(workdir)            # {file_name: form_dict}
        concepts = _load_concepts(workdir)      # {uuid: concept_dict}

        ops, schema_rejects = _parse_operations(operations)
        result.rejected.extend(schema_rejects)

        modified_forms: set[str] = set()

        for op in ops:
            try:
                _apply_one(op, forms, concepts, result, modified_forms)
            except _OpReject as r:
                result.rejected.append(RejectedOp(op_id=op.op_id, kind=r.kind, reason=r.reason))

        # Post-apply integrity check
        integrity = _check_integrity(forms, concepts)
        if integrity:
            for reason in integrity:
                result.rejected.append(RejectedOp(op_id="*", kind="integrity", reason=reason))
            # Abort: don't write anything.
            log.warning("Bundle edit aborted; integrity check failed: %s", integrity)
            return result

        # Write back only the forms we changed; rewrite concepts.json if any
        # concept was appended.
        for file_name in sorted(modified_forms):
            path = os.path.join(workdir, "forms", file_name)
            _write_json(path, forms[file_name])
            result.forms_modified.append(file_name)

        if result.concepts_appended:
            _write_json(os.path.join(workdir, "concepts.json"), list(concepts.values()))

        # Repackage if the original was a ZIP.
        if zipfile.is_zipfile(bundle_path):
            _repackage_zip(workdir, bundle_path)

    return result


# ── Op application ────────────────────────────────────────────────────────────


class _OpReject(Exception):
    def __init__(self, kind: str, reason: str):
        self.kind = kind
        self.reason = reason


def _apply_one(
    op: EditOperation,
    forms: dict[str, dict],
    concepts: dict[str, dict],
    result: EditResult,
    modified_forms: set[str],
) -> None:
    form_name = op.target.get("form", "").strip()
    if not form_name:
        raise _OpReject("schema", "target.form is required")

    file_name, form = _find_form(forms, form_name, op.target.get("form_uuid"))
    section = _find_section(form, op.target.get("section", ""), op.target.get("section_uuid"))

    if op.kind == "field.add":
        _apply_add(op, form, section, concepts, result, form_name)
    elif op.kind == "field.rename":
        _apply_rename(op, form, section, concepts, result, form_name)
    elif op.kind == "field.remove":
        _apply_remove(op, section, result)
    elif op.kind == "section.reorder_fields":
        _apply_reorder(op, section, result)
    else:
        raise _OpReject("schema", f"unsupported kind: {op.kind}")

    modified_forms.add(file_name)
    result.applied.append(op.op_id)


def _apply_add(
    op: EditOperation,
    form: dict,
    section: dict,
    concepts: dict[str, dict],
    result: EditResult,
    form_name: str,
) -> None:
    payload = op.payload
    field_name = (payload.get("name") or "").strip()
    if not field_name:
        raise _OpReject("schema", "payload.name is required for field.add")
    data_type = payload.get("dataType", "Text")
    if data_type == "Coded":
        opts = payload.get("options")
        if not isinstance(opts, list) or not opts:
            raise _OpReject("schema", "payload.options (non-empty list) is required for Coded fields")

    new_uuid = make_uuid(f"formElement:{form_name}:{field_name}")
    existing = _find_element_by_uuid(section, new_uuid)

    if existing is not None and not existing.get("voided", False):
        raise _OpReject(
            "duplicate_name",
            f"field {field_name!r} already exists (live) in section {section.get('name')!r}",
        )

    if existing is not None and existing.get("voided", False):
        # Reinstate
        existing["voided"] = False
        _apply_properties(existing, payload, field_name)
        result.form_elements_reinstated += 1
    else:
        # New element
        elements = section.setdefault("formElements", [])
        max_order = max((e.get("displayOrder", 0) for e in elements), default=0)
        new_elem = _build_element(form_name, field_name, payload, new_uuid, max_order + 1)
        elements.append(new_elem)
        result.form_elements_added += 1

    # Concept
    concept_uuid = make_uuid(f"concept:{field_name}")
    if concept_uuid not in concepts:
        concepts[concept_uuid] = _build_concept(field_name, data_type, payload, concept_uuid)
        result.concepts_appended += 1


def _apply_rename(
    op: EditOperation,
    form: dict,
    section: dict,
    concepts: dict[str, dict],
    result: EditResult,
    form_name: str,
) -> None:
    field_name = op.target.get("field", "").strip()
    new_name = (op.payload.get("new_name") or "").strip()
    if not field_name or not new_name:
        raise _OpReject("schema", "target.field and payload.new_name are required for field.rename")

    existing = _find_live_element_by_name(section, field_name)
    if existing is None:
        raise _OpReject(
            "not_found",
            f"live field {field_name!r} not found in section {section.get('name')!r}",
        )

    captured = {
        "displayOrder": existing.get("displayOrder"),
        "mandatory": existing.get("mandatory", False),
        "keyValues": existing.get("keyValues", []),
        "dataType": existing.get("concept", {}).get("dataType", "Text"),
        "answers": existing.get("concept", {}).get("answers", []),
    }

    # Void the old element. UUID and displayOrder stay as-is.
    existing["voided"] = True

    # Add new element at the captured displayOrder.
    new_uuid = make_uuid(f"formElement:{form_name}:{new_name}")
    new_existing = _find_element_by_uuid(section, new_uuid)

    if new_existing is not None and not new_existing.get("voided", False):
        # Restore the original element from voided state, since the rename is
        # impossible here.
        existing["voided"] = False
        raise _OpReject(
            "duplicate_name",
            f"cannot rename to {new_name!r}: a live field with that name already exists in section {section.get('name')!r}",
        )

    if new_existing is not None and new_existing.get("voided", False):
        # Reinstate (e.g. renaming back to a previously removed name).
        new_existing["voided"] = False
        # Reinstate keeps its own original properties; do NOT clobber with captured
        # (the user is bringing back a previously-deleted field exactly as it was).
    else:
        new_elem = _build_element_from_captured(new_name, captured, new_uuid)
        section["formElements"].append(new_elem)

    # Concept for the new name
    new_concept_uuid = make_uuid(f"concept:{new_name}")
    if new_concept_uuid not in concepts:
        # Use the captured dataType + answers so the concept inherits the renamed
        # field's nature.
        synth_payload = {
            "dataType": captured["dataType"],
            "options": [a.get("name") for a in captured["answers"]],
        }
        concepts[new_concept_uuid] = _build_concept(
            new_name, captured["dataType"], synth_payload, new_concept_uuid,
        )
        result.concepts_appended += 1

    result.form_elements_renamed += 1


def _apply_remove(op: EditOperation, section: dict, result: EditResult) -> None:
    field_name = op.target.get("field", "").strip()
    if not field_name:
        raise _OpReject("schema", "target.field is required for field.remove")

    existing = _find_live_element_by_name(section, field_name)
    if existing is None:
        raise _OpReject(
            "not_found",
            f"live field {field_name!r} not found in section {section.get('name')!r}",
        )
    existing["voided"] = True
    result.form_elements_voided += 1


def _apply_reorder(op: EditOperation, section: dict, result: EditResult) -> None:
    """Reassign displayOrder values for the section's live form elements.

    Payload: {"order": [<field_name>, <field_name>, ...]}.
    The list must contain every live form element in the section, exactly
    once. Voided elements are not part of the order and keep their existing
    displayOrder (which may now overlap with live ones — that's fine because
    the integrity check enforces uniqueness only among live elements).
    """
    order = op.payload.get("order")
    if not isinstance(order, list) or not order:
        raise _OpReject("schema", "payload.order (non-empty list) required for section.reorder_fields")

    live_elements = [e for e in section.get("formElements", []) if not e.get("voided", False)]
    live_by_norm = {_norm(e.get("name", "")): e for e in live_elements}

    if len(live_by_norm) != len(live_elements):
        raise _OpReject(
            "ambiguous_target",
            f"section {section.get('name')!r} has duplicate live field names; cannot reorder unambiguously",
        )

    requested = [_norm(n) for n in order]
    if len(set(requested)) != len(requested):
        raise _OpReject("schema", "payload.order contains duplicate names")

    missing = [n for n in requested if n not in live_by_norm]
    if missing:
        raise _OpReject("not_found", f"reorder names not found among live fields: {missing}")
    extra = [n for n in live_by_norm if n not in requested]
    if extra:
        raise _OpReject("schema", f"reorder must include every live field; missing: {extra}")

    for idx, norm_name in enumerate(requested, start=1):
        live_by_norm[norm_name]["displayOrder"] = idx


# ── Builders ──────────────────────────────────────────────────────────────────


def _build_element(form_name: str, field_name: str, payload: dict, uuid: str, display_order: int) -> dict:
    data_type = payload.get("dataType", "Text")
    answers = _build_answers(data_type, payload.get("options"))
    elem = {
        "uuid": uuid,
        "name": field_name,
        "displayOrder": display_order,
        "mandatory": bool(payload.get("mandatory", False)),
        "keyValues": _build_key_values(payload),
        "validationDeclarativeRule": "",
        "rule": "",
        "voided": False,
        "concept": {
            "name": field_name,
            "uuid": make_uuid(f"concept:{field_name}"),
            "dataType": data_type,
            "answers": answers,
        },
    }
    return elem


def _build_element_from_captured(field_name: str, captured: dict, uuid: str) -> dict:
    elem = {
        "uuid": uuid,
        "name": field_name,
        "displayOrder": captured["displayOrder"],
        "mandatory": bool(captured.get("mandatory", False)),
        "keyValues": captured.get("keyValues", []),
        "validationDeclarativeRule": "",
        "rule": "",
        "voided": False,
        "concept": {
            "name": field_name,
            "uuid": make_uuid(f"concept:{field_name}"),
            "dataType": captured["dataType"],
            "answers": captured.get("answers", []),
        },
    }
    return elem


def _build_concept(name: str, data_type: str, payload: dict, uuid: str) -> dict:
    answers = _build_answers(data_type, payload.get("options"))
    return {
        "name": name,
        "uuid": uuid,
        "dataType": data_type,
        "active": True,
        "answers": answers,
    }


def _build_answers(data_type: str, options: list | None) -> list[dict]:
    if data_type != "Coded" or not options:
        return []
    seen: set[str] = set()
    answers: list[dict] = []
    for opt in options:
        opt = (opt or "").strip()
        if not opt:
            continue
        u = make_uuid(f"concept:{opt}")
        if u in seen:
            continue
        seen.add(u)
        answers.append({"name": opt, "uuid": u, "order": len(answers), "active": True})
    return answers


def _build_key_values(payload: dict) -> list[dict]:
    out: list[dict] = []
    if payload.get("unit"):
        out.append({"key": "unit", "value": payload["unit"]})
    if payload.get("min") is not None:
        out.append({"key": "min", "value": payload["min"]})
    if payload.get("max") is not None:
        out.append({"key": "max", "value": payload["max"]})
    if payload.get("selectionType") == "MultiSelect":
        out.append({"key": "multiSelect", "value": True})
    return out


def _apply_properties(elem: dict, payload: dict, field_name: str) -> None:
    """Overwrite a reinstated element's mutable properties from the payload."""
    data_type = payload.get("dataType", elem.get("concept", {}).get("dataType", "Text"))
    answers = _build_answers(data_type, payload.get("options"))
    elem["mandatory"] = bool(payload.get("mandatory", elem.get("mandatory", False)))
    elem["keyValues"] = _build_key_values(payload)
    elem["concept"]["dataType"] = data_type
    elem["concept"]["answers"] = answers


# ── Lookups ───────────────────────────────────────────────────────────────────


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _find_form(forms: dict[str, dict], form_name: str, form_uuid: str | None) -> tuple[str, dict]:
    if form_uuid:
        for file_name, form in forms.items():
            if form.get("uuid") == form_uuid:
                return file_name, form
        raise _OpReject("not_found", f"form_uuid {form_uuid!r} not in bundle")
    key = _norm(form_name)
    matches = [
        (file_name, form) for file_name, form in forms.items()
        if _norm(form.get("name", "")) == key
    ]
    if not matches:
        raise _OpReject("not_found", f"form {form_name!r} not in bundle")
    if len(matches) > 1:
        raise _OpReject(
            "ambiguous_target",
            f"form {form_name!r} matches {len(matches)} forms; pass form_uuid",
        )
    return matches[0]


def _find_section(form: dict, section_name: str, section_uuid: str | None) -> dict:
    groups = form.get("formElementGroups", [])
    if section_uuid:
        for g in groups:
            if g.get("uuid") == section_uuid:
                return g
        raise _OpReject("not_found", f"section_uuid {section_uuid!r} not in form {form.get('name')!r}")
    key = _norm(section_name)
    matches = [g for g in groups if _norm(g.get("name", "")) == key]
    if not matches:
        raise _OpReject(
            "not_found",
            f"section {section_name!r} not in form {form.get('name')!r}",
        )
    if len(matches) > 1:
        raise _OpReject(
            "ambiguous_target",
            f"section {section_name!r} matches {len(matches)} sections in form {form.get('name')!r}; pass section_uuid",
        )
    return matches[0]


def _find_element_by_uuid(section: dict, uuid: str) -> dict | None:
    for e in section.get("formElements", []):
        if e.get("uuid") == uuid:
            return e
    return None


def _find_live_element_by_name(section: dict, field_name: str) -> dict | None:
    key = _norm(field_name)
    matches = [
        e for e in section.get("formElements", [])
        if not e.get("voided", False) and _norm(e.get("name", "")) == key
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise _OpReject(
            "ambiguous_target",
            f"field {field_name!r} matches {len(matches)} live elements in section {section.get('name')!r}; pass field_uuid",
        )
    return matches[0]


# ── Integrity ────────────────────────────────────────────────────────────────


def _check_integrity(forms: dict[str, dict], concepts: dict[str, dict]) -> list[str]:
    errors: list[str] = []

    # No duplicate formElement.uuid within a form (includes voided).
    for file_name, form in forms.items():
        seen: dict[str, int] = {}
        for g in form.get("formElementGroups", []):
            for e in g.get("formElements", []):
                u = e.get("uuid")
                if not u:
                    continue
                seen[u] = seen.get(u, 0) + 1
        dups = [u for u, n in seen.items() if n > 1]
        if dups:
            errors.append(f"{file_name}: duplicate formElement uuid(s): {dups}")

    # displayOrder uniqueness among LIVE elements within a section.
    for file_name, form in forms.items():
        for g in form.get("formElementGroups", []):
            seen_orders: dict[int, int] = {}
            for e in g.get("formElements", []):
                if e.get("voided"):
                    continue
                o = e.get("displayOrder")
                if o is None:
                    continue
                seen_orders[o] = seen_orders.get(o, 0) + 1
            dups = [o for o, n in seen_orders.items() if n > 1]
            if dups:
                errors.append(
                    f"{file_name} / section {g.get('name')!r}: duplicate displayOrder among live: {dups}"
                )

    # concepts.json has no duplicate UUIDs (dict keyed by uuid already enforces this).
    # Skip the check.

    return errors


# ── I/O ───────────────────────────────────────────────────────────────────────


class _BundleContext:
    """Context manager that yields a working directory for the bundle."""
    def __init__(self, bundle_path: str):
        self.bundle_path = bundle_path
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._workdir: str = ""

    def __enter__(self) -> str:
        if os.path.isdir(self.bundle_path):
            self._workdir = self.bundle_path
        elif zipfile.is_zipfile(self.bundle_path):
            self._tmp = tempfile.TemporaryDirectory(prefix="bundle-edit-")
            self._workdir = self._tmp.name
            with zipfile.ZipFile(self.bundle_path, "r") as zf:
                zf.extractall(self._workdir)
        else:
            raise FileNotFoundError(f"Not a bundle: {self.bundle_path}")
        return self._workdir

    def __exit__(self, *exc):
        if self._tmp is not None:
            self._tmp.cleanup()


def _open_bundle(bundle_path: str):
    return _BundleContext(bundle_path)


def _load_forms(workdir: str) -> dict[str, dict]:
    forms_dir = os.path.join(workdir, "forms")
    if not os.path.isdir(forms_dir):
        return {}
    out: dict[str, dict] = {}
    for fname in os.listdir(forms_dir):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(forms_dir, fname), encoding="utf-8") as fh:
            out[fname] = json.load(fh)
    return out


def _load_concepts(workdir: str) -> dict[str, dict]:
    path = os.path.join(workdir, "concepts.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        arr = json.load(fh)
    return {c["uuid"]: c for c in arr if c.get("uuid")}


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _repackage_zip(workdir: str, target_zip: str) -> None:
    """Re-zip the working directory in canonical order, overwriting target_zip."""
    forms_dir = os.path.join(workdir, "forms")
    form_entries = []
    if os.path.isdir(forms_dir):
        form_entries = sorted(
            f"forms/{name}" for name in os.listdir(forms_dir) if name.endswith(".json")
        )

    # Write to a sibling temp file then move into place for atomicity.
    tmp_zip = target_zip + ".tmp"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _FLAT_FILES:
            full = os.path.join(workdir, name)
            if os.path.exists(full):
                zf.write(full, name)
        for entry in form_entries:
            zf.write(os.path.join(workdir, entry), entry)
    shutil.move(tmp_zip, target_zip)


# ── Op parsing ───────────────────────────────────────────────────────────────


def _parse_operations(raw: list[dict]) -> tuple[list[EditOperation], list[RejectedOp]]:
    ops: list[EditOperation] = []
    rejects: list[RejectedOp] = []
    for i, item in enumerate(raw):
        op_id = (item.get("op_id") if isinstance(item, dict) else None) or f"op-{i+1}"
        try:
            ops.append(EditOperation(**item))
        except ValidationError as exc:
            rejects.append(RejectedOp(op_id=op_id, kind="schema", reason=str(exc)))
    return ops, rejects


# ── Snapshot loader for edit-from-spec diff ───────────────────────────────────


def load_bundle_snapshot(bundle_path: str) -> dict:
    """Load an existing bundle into the same shape as the generator's output:

        {"forms": [{"file_name": str, "content": dict}, ...],
         "concepts": [<concept_dict>, ...]}

    This matches what `generate_forms` puts into `state["forms_json"]` /
    `state["concepts_json"]`, so the diff function (`domain/diff.py`) can
    compare like-for-like.
    """
    with _open_bundle(bundle_path) as workdir:
        forms_map = _load_forms(workdir)
        concepts_map = _load_concepts(workdir)

    return {
        "forms": [
            {"file_name": fname, "content": form}
            for fname, form in sorted(forms_map.items())
        ],
        "concepts": list(concepts_map.values()),
    }


# ── Helper for the agent: a compact summary of bundle fields ──────────────────


def list_bundle_fields(bundle_path: str) -> dict:
    """Return a compact summary of every form's sections + fields.

    Useful for the agent to ground edit operations + rule intents in real
    names. Coded fields include their `answers` list so the agent can echo
    exact answer strings back to the user before generating rules.
    """
    with _open_bundle(bundle_path) as workdir:
        forms = _load_forms(workdir)
        out_forms: list[dict] = []
        for fname in sorted(forms):
            form = forms[fname]
            sections = []
            for g in form.get("formElementGroups", []):
                fields = []
                for e in g.get("formElements", []):
                    concept = e.get("concept") or {}
                    field: dict = {
                        "name": e.get("name"),
                        "dataType": concept.get("dataType"),
                        "voided": bool(e.get("voided", False)),
                    }
                    if concept.get("dataType") == "Coded":
                        field["answers"] = [
                            a.get("name") for a in (concept.get("answers") or [])
                            if a.get("name") and not a.get("voided")
                        ]
                    fields.append(field)
                sections.append({"name": g.get("name"), "fields": fields})
            out_forms.append({
                "file": fname,
                "form": form.get("name"),
                "formType": form.get("formType"),
                "sections": sections,
            })
        return {"bundle_path": bundle_path, "forms": out_forms}


# ── Rule generation support (VISIT_SCHEDULE_RULE_SDD §9.2) ───────────────────


def load_form_rule_context(bundle_path: str, form_name: str) -> dict | None:
    """Read everything a RuleSpec needs for one form, in one bundle pass.

    Returns:
        {
          "form_type": str,
          "subject_type": str | None,
          "program": str | None,
          "encounter_type": str | None,
          "available_concepts": list[str],
          "available_encounter_types": list[str],
          "available_programs": list[str],
        }
        or None if no form with `form_name` exists in the bundle.
    """
    with _open_bundle(bundle_path) as workdir:
        forms = _load_forms(workdir)
        target_form: dict | None = None
        for form in forms.values():
            if form.get("name") == form_name:
                target_form = form
                break
        if target_form is None:
            return None

        concepts = sorted({
            element.get("name")
            for group in (target_form.get("formElementGroups") or [])
            for element in (group.get("formElements") or [])
            if element.get("name") and not element.get("voided")
        })

        subject_names, subject_by_uuid = _load_entity_index(workdir, "subjectTypes.json")
        program_names, program_by_uuid = _load_entity_index(workdir, "programs.json")
        encounter_names, encounter_by_uuid = _load_entity_index(workdir, "encounterTypes.json")
        mappings = _load_form_mappings(workdir)

        form_uuid = target_form.get("uuid")
        mapping = next(
            (m for m in mappings if m.get("formUUID") == form_uuid),
            None,
        )

        concept_answers = _collect_concept_answers_for(
            forms, mappings, mapping, target_form,
        )

        return {
            "form_type": target_form.get("formType", ""),
            "subject_type": _name_from_uuid(
                mapping.get("subjectTypeUUID") if mapping else None,
                subject_by_uuid,
            ),
            "program": _name_from_uuid(
                mapping.get("programUUID") if mapping else None,
                program_by_uuid,
            ),
            "encounter_type": _name_from_uuid(
                mapping.get("encounterTypeUUID") if mapping else None,
                encounter_by_uuid,
            ),
            "available_concepts": concepts,
            "available_encounter_types": sorted(encounter_names),
            "available_programs": sorted(program_names),
            "concept_answers": concept_answers,
            "_for_internal_use_subject_types": sorted(subject_names),
        }


# Mirrors `_PROGRAM_FORM_TYPES_FOR_GROUNDING` in domain.rules.orchestrator —
# see CONCEPT_ANSWER_GROUNDING_SDD.md §5.
_PROGRAM_FORM_TYPES_FOR_GROUNDING: frozenset[str] = frozenset({
    "ProgramEnrolment", "ProgramExit",
    "ProgramEncounter", "ProgramEncounterCancellation",
})


def _collect_concept_answers_for(
    forms: dict[str, dict],
    mappings: list[dict],
    target_mapping: dict | None,
    target_form: dict,
) -> dict[str, list[str]]:
    """Merge coded-field answer lists across the target form + registration
    + enrolment (when applicable).

    Reads each in-scope form's `formElementGroups[*].formElements[*].concept`
    block — coded fields carry their answers as `answers[*].name`.
    """
    in_scope_uuids: set[str] = {target_form.get("uuid", "")}
    target_type = target_form.get("formType", "")
    target_subject_uuid = target_mapping.get("subjectTypeUUID") if target_mapping else None
    target_program_uuid = target_mapping.get("programUUID") if target_mapping else None

    if target_type in _PROGRAM_FORM_TYPES_FOR_GROUNDING and target_program_uuid:
        enrolment_uuid = next(
            (m.get("formUUID") for m in mappings
             if m.get("formType") == "ProgramEnrolment"
             and m.get("programUUID") == target_program_uuid),
            None,
        )
        if enrolment_uuid:
            in_scope_uuids.add(enrolment_uuid)

    if target_type != "IndividualProfile" and target_subject_uuid:
        registration_uuid = next(
            (m.get("formUUID") for m in mappings
             if m.get("formType") == "IndividualProfile"
             and m.get("subjectTypeUUID") == target_subject_uuid),
            None,
        )
        if registration_uuid:
            in_scope_uuids.add(registration_uuid)

    answers: dict[str, list[str]] = {}
    target_uuid = target_form.get("uuid", "")
    for form in forms.values():
        if form.get("uuid") not in in_scope_uuids:
            continue
        is_target = form.get("uuid") == target_uuid
        for group in (form.get("formElementGroups") or []):
            for element in (group.get("formElements") or []):
                if element.get("voided"):
                    continue
                concept = element.get("concept") or {}
                if concept.get("dataType") != "Coded":
                    continue
                name = element.get("name") or concept.get("name")
                if not name:
                    continue
                if not is_target:
                    continue
                options = [
                    a.get("name") for a in (concept.get("answers") or [])
                    if a.get("name") and not a.get("voided")
                ]
                if options:
                    answers[name] = options
    return answers


def write_form_rule(
    bundle_path: str, form_name: str, rule_field: str, js: str,
    *,
    page_name: str | None = None,
    field_name: str | None = None,
) -> bool:
    """Write a generated rule body into the bundle and re-zip atomically.

    For form-level kinds (``visitScheduleRule`` / ``validationRule`` /
    ``editFormRule`` / ``decisionRule``) the body is written to
    ``form[rule_field]`` at the top level. For ``formElementRule`` the
    body is written to the matching field's ``rule`` slot at
    ``form.formElementGroups[page_name].formElements[field_name].rule``;
    both ``page_name`` and ``field_name`` are required in that case.

    Returns True when the target slot was located and rewritten; False
    when the form, page, or field could not be found.
    """
    with _open_bundle(bundle_path) as workdir:
        forms = _load_forms(workdir)
        target_fname: str | None = None
        for fname, form in forms.items():
            if form.get("name") == form_name:
                target_fname = fname
                break
        if target_fname is None:
            return False
        target_form = forms[target_fname]

        if rule_field == "formElementRule":
            if not page_name or not field_name:
                log.warning(
                    f"write_form_rule: formElementRule requires "
                    f"page_name and field_name; "
                    f"got page={page_name!r} field={field_name!r}"
                )
                return False
            form_element = _find_form_element_by_name(
                target_form, page_name, field_name,
            )
            if form_element is None:
                return False
            form_element["rule"] = js
        else:
            target_form[rule_field] = js

        _write_json(os.path.join(workdir, "forms", target_fname), target_form)
        if zipfile.is_zipfile(bundle_path):
            _repackage_zip(workdir, bundle_path)
    return True


def _find_form_element_by_name(
    form: dict, page_name: str, field_name: str,
) -> dict | None:
    """Locate a single ``formElement`` dict by exact (page, field) names.

    Unlike the pipeline-side helper (which truncates spec names to match the
    generator's output), bundle-side callers already know the names as they
    appear in the bundle JSON — ``list_bundle_fields`` is the canonical
    source — so an exact equality check is sufficient. Returns None when
    no matching page or field is found.
    """
    for group in form.get("formElementGroups") or []:
        if group.get("name") != page_name:
            continue
        for element in group.get("formElements") or []:
            if element.get("name") == field_name:
                return element
    return None


def _load_entity_index(
    workdir: str, file_name: str,
) -> tuple[list[str], dict[str, str]]:
    """Read a top-level array file once; return (names, uuid→name map).

    Single-pass replacement for the previous `_load_entity_names` +
    `_load_entity_uuid_map` pair. Entries missing `name` are dropped; entries
    missing `uuid` still contribute to `names`.
    """
    path = os.path.join(workdir, file_name)
    if not os.path.exists(path):
        return [], {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        return [], {}
    names: list[str] = []
    uuid_to_name: dict[str, str] = {}
    for entry in data:
        name = entry.get("name")
        if not name:
            continue
        names.append(name)
        uuid = entry.get("uuid")
        if uuid:
            uuid_to_name[uuid] = name
    return names, uuid_to_name


def _load_form_mappings(workdir: str) -> list[dict]:
    path = os.path.join(workdir, "formMappings.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def _name_from_uuid(uuid: str | None, mapping: dict[str, str]) -> str | None:
    if not uuid:
        return None
    return mapping.get(uuid)

"""
LangGraph pipeline: parse Avni modelling + scoping documents for a single org
and produce a bundle ZIP.

Pipeline nodes (sequential):
  discover_files → parse_documents → generate_entities → generate_forms
      → generate_form_mappings → package_zip

Output ZIP contains:
  addressLevelTypes.json, subjectTypes.json, operationalSubjectTypes.json,
  encounterTypes.json, operationalEncounterTypes.json, programs.json,
  operationalPrograms.json, concepts.json, forms/*.json, formMappings.json,
  organisationConfig.json
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys
import uuid
import zipfile
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

# ── src/ on sys.path so local private modules (_scoping_parser, _bundle_models)
# are importable without a package prefix when invoked from any working directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

log = logging.getLogger(__name__)

# ── Deterministic UUID (UUID v5, same namespace as avni-ai) ───────────────────
_AVNI_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "avni.project.org")


def _uuid(seed: str) -> str:
    return str(uuid.uuid5(_AVNI_NS, seed))


# ── ZIP canonical order (mirrors BundleService.java / zip_bundle.js) ─────────
_CANONICAL_ORDER = [
    "addressLevelTypes.json",
    "subjectTypes.json",
    "operationalSubjectTypes.json",
    "encounterTypes.json",
    "operationalEncounterTypes.json",
    "programs.json",
    "operationalPrograms.json",
    "concepts.json",
    "__FORMS__",
    "formMappings.json",
    "organisationConfig.json",
]

# ── State ──────────────────────────────────────────────────────────────────────


class BundleState(TypedDict):
    org_name: str
    input_dir: str
    output_dir: str
    file_paths: list[str]
    entity_spec: Any                        # EntitySpec from avni-ai (Pydantic model)
    parse_warnings: list[str]
    subject_types_json: list[dict]
    operational_subject_types_json: dict
    programs_json: list[dict]
    operational_programs_json: dict
    encounter_types_json: list[dict]
    operational_encounter_types_json: dict
    address_level_types_json: list[dict]
    organisation_config_json: dict
    forms_json: list[dict]                  # [{file_name, content}, ...]
    concepts_json: list[dict]
    form_specs_for_mapping: list[dict]      # lightweight mapping metadata per form
    form_mappings_json: list[dict]
    zip_path: str
    errors: list[str]


# ── Node 1: discover_files ────────────────────────────────────────────────────


def discover_files(state: BundleState) -> BundleState:
    input_dir = state["input_dir"]
    errors = list(state.get("errors", []))

    if not os.path.isdir(input_dir):
        errors.append(f"Input directory not found: {input_dir}")
        return {**state, "file_paths": [], "errors": errors}

    file_paths = sorted(glob.glob(os.path.join(input_dir, "*.xlsx")))
    if not file_paths:
        errors.append(f"No .xlsx files found in: {input_dir}")

    log.info("[%s] Found %d file(s): %s", state["org_name"], len(file_paths),
             [os.path.basename(p) for p in file_paths])
    return {**state, "file_paths": file_paths, "errors": errors}


# ── Node 2: parse_documents ───────────────────────────────────────────────────


def parse_documents(state: BundleState) -> BundleState:
    errors = list(state.get("errors", []))

    try:
        from _scoping_parser import parse_scoping_docs  # noqa: PLC0415
    except ImportError as exc:
        errors.append(f"Cannot import scoping parser: {exc}")

        return {**state, "entity_spec": None, "parse_warnings": [], "errors": errors}

    try:
        entity_spec, misc_sheets = parse_scoping_docs(state["file_paths"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Parsing failed: {exc}")
        return {**state, "entity_spec": None, "parse_warnings": [], "errors": errors}

    warnings = list(entity_spec.validation_warnings)
    if misc_sheets:
        names = [s["name"] for s in misc_sheets]
        warnings.append(f"{len(misc_sheets)} unclassified sheet(s): {names}")

    log.info(
        "[%s] Parsed — subjects=%d programs=%d encounters=%d forms=%d",
        state["org_name"],
        len(entity_spec.subject_types),
        len(entity_spec.programs),
        len(entity_spec.encounter_types),
        len(entity_spec.forms),
    )
    return {**state, "entity_spec": entity_spec, "parse_warnings": warnings, "errors": errors}


# ── Node 3: generate_entities ─────────────────────────────────────────────────


def generate_entities(state: BundleState) -> BundleState:
    spec = state["entity_spec"]
    org_name = state["org_name"]

    # ── Subject types ──────────────────────────────────────────────────────────
    subject_types_json: list[dict] = []
    for st in spec.subject_types:
        st_type = st.type  # "Person", "Individual", "Household", "Group"
        is_household = st_type.lower() == "household"
        is_group = st_type.lower() in ("group", "household")
        subject_types_json.append({
            "name": st.name,
            "uuid": _uuid(f"subjectType:{st.name}"),
            "active": True,
            "type": st_type,
            "subjectSummaryRule": "",
            "programEligibilityCheckRule": "",
            "allowEmptyLocation": False,
            "allowMiddleName": False,
            "lastNameOptional": st.lastNameOptional,
            "allowProfilePicture": st.allowProfilePicture,
            "uniqueName": st.uniqueName,
            "shouldSyncByLocation": True,
            "settings": {
                "displayRegistrationDetails": True,
                "displayPlannedEncounters": True,
            },
            "household": is_household,
            "group": is_group,
            "directlyAssignable": False,
            "voided": False,
        })

    operational_subject_types_json = {
        "operationalSubjectTypes": [
            {
                "uuid": _uuid(f"operationalSubjectType:{st['name']}"),
                "name": st["name"],
                "subjectType": {"uuid": st["uuid"], "voided": False},
                "voided": False,
            }
            for st in subject_types_json
        ]
    }

    # ── Programs ───────────────────────────────────────────────────────────────
    programs_json: list[dict] = []
    for p in spec.programs:
        programs_json.append({
            "name": p.name,
            "uuid": _uuid(f"program:{p.name}"),
            "colour": p.colour,
            "voided": False,
            "active": True,
            "enrolmentEligibilityCheckRule": "",
            "enrolmentSummaryRule": "",
            "manualEligibilityCheckRequired": False,
            "allowMultipleEnrolments": p.allow_multiple_enrolments,
        })

    operational_programs_json = {
        "operationalPrograms": [
            {
                "uuid": _uuid(f"operationalProgram:{p['name']}"),
                "name": p["name"],
                "program": {"uuid": p["uuid"], "voided": False},
                "programSubjectLabel": "",
                "voided": False,
            }
            for p in programs_json
        ]
    }

    # ── Encounter types ────────────────────────────────────────────────────────
    encounter_types_json: list[dict] = []
    for et in spec.encounter_types:
        encounter_types_json.append({
            "name": et.name,
            "uuid": _uuid(f"encounterType:{et.name}"),
            "encounterEligibilityCheckRule": "",
            "active": True,
            "immutable": False,
        })

    operational_encounter_types_json = {
        "operationalEncounterTypes": [
            {
                "uuid": _uuid(f"operationalEncounterType:{et['name']}"),
                "name": et["name"],
                "encounterType": {"uuid": et["uuid"], "voided": False},
                "voided": False,
            }
            for et in encounter_types_json
        ]
    }

    # ── Address level types ────────────────────────────────────────────────────
    sorted_levels = sorted(spec.address_levels, key=lambda a: a.level, reverse=True)
    name_to_uuid = {al.name: _uuid(f"addressLevelType:{al.name}") for al in sorted_levels}

    address_level_types_json: list[dict] = []
    for al in sorted_levels:
        entry: dict = {
            "uuid": name_to_uuid[al.name],
            "name": al.name,
            "level": float(al.level),
        }
        if al.parent and al.parent in name_to_uuid:
            entry["parent"] = {"uuid": name_to_uuid[al.parent]}
        address_level_types_json.append(entry)

    # ── Organisation config ────────────────────────────────────────────────────
    organisation_config_json = {
        "uuid": _uuid(f"orgConfig:{org_name}"),
        "settings": {
            "languages": ["en"],
            "customRegistrationLocations": [],
            "searchFilters": [],
            "myDashboardFilters": [],
        },
        "worklistUpdationRule": "",
    }

    return {
        **state,
        "subject_types_json": subject_types_json,
        "operational_subject_types_json": operational_subject_types_json,
        "programs_json": programs_json,
        "operational_programs_json": operational_programs_json,
        "encounter_types_json": encounter_types_json,
        "operational_encounter_types_json": operational_encounter_types_json,
        "address_level_types_json": address_level_types_json,
        "organisation_config_json": organisation_config_json,
    }


# ── Node 4: generate_forms ────────────────────────────────────────────────────

_CANCEL_OPTIONS = ["Data entry error", "Rescheduled", "Participant not available", "Other"]


def _safe_filename(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")


def _trunc(value: str, limit: int = 255) -> str:
    return value[:limit] if len(value) > limit else value


def _build_form(
    name: str,
    form_type: str,
    sections: list,                 # list of SectionSpec
    concepts_registry: dict,        # mutated in-place: lower(name) → concept dict
) -> dict:
    """Produce a form JSON dict and register all concepts encountered."""
    form_uuid = _uuid(f"form:{name}")
    form_element_groups: list[dict] = []

    for g_idx, section in enumerate(sections, start=1):
        group_uuid = _uuid(f"formGroup:{name}:{section.name}")
        form_elements: list[dict] = []

        for e_idx, field in enumerate(section.fields, start=1):
            concept_name = _trunc(field.name)
            concept_uuid = _uuid(f"concept:{concept_name}")

            # Build answer list for Coded fields.
            # Deduplicate by UUID (same option text → same UUID; duplicate rows in
            # the source sheet would otherwise produce (concept_id, answer_concept_id)
            # pairs that violate the unique constraint on upload).
            answers: list[dict] = []
            if field.dataType == "Coded" and field.options:
                seen_answer_uuids: set[str] = set()
                order = 0
                for opt in field.options:
                    a_name = _trunc(opt)
                    a_uuid = _uuid(f"concept:{a_name}")
                    if a_uuid in seen_answer_uuids:
                        continue
                    seen_answer_uuids.add(a_uuid)
                    answers.append({
                        "name": a_name,
                        "uuid": a_uuid,
                        "order": order,
                        "active": True,
                    })
                    order += 1

            # Register concept (deduplicate by lowercased name)
            key = concept_name.lower()
            if key not in concepts_registry:
                concepts_registry[key] = {
                    "name": concept_name,
                    "uuid": concept_uuid,
                    "dataType": field.dataType,
                    "active": True,
                    "answers": answers,
                }

            # keyValues: unit, min, max, multiSelect
            key_values: list[dict] = []
            if field.unit:
                key_values.append({"key": "unit", "value": field.unit})
            if field.min is not None:
                key_values.append({"key": "min", "value": field.min})
            if field.max is not None:
                key_values.append({"key": "max", "value": field.max})
            if field.selectionType == "MultiSelect":
                key_values.append({"key": "multiSelect", "value": True})

            form_elements.append({
                "uuid": _uuid(f"formElement:{name}:{concept_name}"),
                "name": concept_name,
                "displayOrder": e_idx,
                "mandatory": field.mandatory,
                "keyValues": key_values,
                "validationDeclarativeRule": "",
                "rule": "",
                "voided": False,
                "concept": {
                    "name": concept_name,
                    "uuid": concept_uuid,
                    "dataType": field.dataType,
                    "answers": answers,
                },
            })

        form_element_groups.append({
            "uuid": group_uuid,
            "name": _trunc(section.name),
            "displayOrder": g_idx,
            "voided": False,
            "formElements": form_elements,
        })

    return {
        "name": name,
        "uuid": form_uuid,
        "formType": form_type,
        "formElementGroups": form_element_groups,
    }


def _build_cancellation_form(
    parent_name: str,
    parent_form_type: str,
    concepts_registry: dict,
) -> tuple[str, str, dict]:
    """
    Auto-generate a cancellation form for a ProgramEncounter or Encounter form.
    Returns (cancel_name, cancel_form_type, form_dict).
    """
    if parent_form_type == "ProgramEncounter":
        cancel_name = f"{parent_name} Encounter Cancellation"
        cancel_form_type = "ProgramEncounterCancellation"
    else:
        cancel_name = f"{parent_name} Cancellation"
        cancel_form_type = "IndividualEncounterCancellation"

    # Single section with one Coded "Reason for Cancellation" field
    from types import SimpleNamespace  # noqa: PLC0415
    reason_field = SimpleNamespace(
        name="Reason for Cancellation",
        dataType="Coded",
        mandatory=True,
        options=_CANCEL_OPTIONS,
        unit=None,
        min=None,
        max=None,
        selectionType="SingleSelect",
    )
    cancel_section = SimpleNamespace(
        name="Cancellation Details",
        fields=[reason_field],
    )
    form_dict = _build_form(cancel_name, cancel_form_type, [cancel_section], concepts_registry)
    return cancel_name, cancel_form_type, form_dict


def generate_forms(state: BundleState) -> BundleState:
    spec = state["entity_spec"]
    errors = list(state.get("errors", []))

    concepts_registry: dict[str, dict] = {}   # lower(name) → concept dict
    forms_json: list[dict] = []                # [{file_name, content}]
    form_specs_for_mapping: list[dict] = []    # lightweight metadata for Node 5

    for form_spec in spec.forms:
        # Use sections if available; otherwise wrap all fields in one section
        if form_spec.sections:
            sections = form_spec.sections
        elif form_spec.fields:
            from types import SimpleNamespace  # noqa: PLC0415
            sections = [SimpleNamespace(name="Details", fields=form_spec.fields)]
        else:
            sections = []

        form_dict = _build_form(
            form_spec.name, form_spec.formType, sections, concepts_registry
        )
        safe = _safe_filename(form_spec.name)
        forms_json.append({
            "file_name": f"{safe}_{form_dict['uuid']}.json",
            "content": form_dict,
        })
        form_specs_for_mapping.append({
            "name": form_spec.name,
            "uuid": form_dict["uuid"],
            "form_type": form_spec.formType,
            "subject_type": form_spec.subjectType or "",
            "program": form_spec.program or "",
            "encounter_type": form_spec.encounterType or "",
        })

        # Auto-generate cancellation form for encounter-type forms
        if form_spec.formType in ("ProgramEncounter", "Encounter"):
            c_name, c_type, c_dict = _build_cancellation_form(
                form_spec.name, form_spec.formType, concepts_registry
            )
            c_safe = _safe_filename(c_name)
            forms_json.append({
                "file_name": f"{c_safe}_{c_dict['uuid']}.json",
                "content": c_dict,
            })
            form_specs_for_mapping.append({
                "name": c_name,
                "uuid": c_dict["uuid"],
                "form_type": c_type,
                "subject_type": form_spec.subjectType or "",
                "program": form_spec.program or "",
                "encounter_type": form_spec.encounterType or "",
            })

    concepts_json = list(concepts_registry.values())

    log.info(
        "[%s] Generated %d form(s) (%d with cancellation), %d concept(s)",
        state["org_name"],
        len(forms_json),
        sum(1 for f in form_specs_for_mapping if "Cancellation" in f["name"]),
        len(concepts_json),
    )

    return {
        **state,
        "forms_json": forms_json,
        "concepts_json": concepts_json,
        "form_specs_for_mapping": form_specs_for_mapping,
        "errors": errors,
    }


# ── Node 5: generate_form_mappings ────────────────────────────────────────────


def generate_form_mappings(state: BundleState) -> BundleState:
    # Build fast-lookup dicts: name (lowercase) → uuid
    st_uuid = {s["name"].lower(): s["uuid"] for s in state["subject_types_json"]}
    prog_uuid = {p["name"].lower(): p["uuid"] for p in state["programs_json"]}
    et_uuid = {e["name"].lower(): e["uuid"] for e in state["encounter_types_json"]}

    # Import fuzzy match from avni-ai (already on sys.path after parse_documents)
    try:
        from _scoping_parser import _fuzzy_match  # noqa: PLC0415
    except ImportError:
        _fuzzy_match = None  # type: ignore[assignment]

    def _resolve(name: str, lookup: dict[str, str]) -> str:
        if not name:
            return ""
        key = name.lower()
        if key in lookup:
            return lookup[key]
        if _fuzzy_match:
            match = _fuzzy_match(name, set(lookup.keys()))
            if match:
                return lookup[match]
        return ""

    form_mappings_json: list[dict] = []

    for spec in state["form_specs_for_mapping"]:
        form_type = spec["form_type"]
        resolved_st = _resolve(spec["subject_type"], st_uuid)
        resolved_prog = _resolve(spec["program"], prog_uuid)
        resolved_et = _resolve(spec["encounter_type"], et_uuid)

        entry: dict = {
            "uuid": _uuid(f"mapping:{spec['name']}"),
            "formUUID": spec["uuid"],
            "formType": form_type,
            "formName": spec["name"],
            "enableApproval": False,
        }

        if resolved_st:
            entry["subjectTypeUUID"] = resolved_st
        if form_type in (
            "ProgramEnrolment", "ProgramExit",
            "ProgramEncounter", "ProgramEncounterCancellation",
        ) and resolved_prog:
            entry["programUUID"] = resolved_prog
        if form_type in (
            "Encounter", "IndividualEncounterCancellation",
            "ProgramEncounter", "ProgramEncounterCancellation",
        ) and resolved_et:
            entry["encounterTypeUUID"] = resolved_et

        form_mappings_json.append(entry)

    return {**state, "form_mappings_json": form_mappings_json}


# ── Node 6: package_zip ───────────────────────────────────────────────────────


def package_zip(state: BundleState) -> BundleState:
    output_dir = state["output_dir"]
    org_name = state["org_name"]
    errors = list(state.get("errors", []))

    os.makedirs(output_dir, exist_ok=True)
    forms_dir = os.path.join(output_dir, "forms")
    os.makedirs(forms_dir, exist_ok=True)

    def _write(path: str, data: Any) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    # Map filename → JSON data for all flat files
    flat_files: dict[str, Any] = {
        "addressLevelTypes.json": state["address_level_types_json"],
        "subjectTypes.json": state["subject_types_json"],
        "operationalSubjectTypes.json": state["operational_subject_types_json"],
        "encounterTypes.json": state["encounter_types_json"],
        "operationalEncounterTypes.json": state["operational_encounter_types_json"],
        "programs.json": state["programs_json"],
        "operationalPrograms.json": state["operational_programs_json"],
        "concepts.json": state["concepts_json"],
        "formMappings.json": state["form_mappings_json"],
        "organisationConfig.json": state["organisation_config_json"],
    }

    try:
        for filename, data in flat_files.items():
            _write(os.path.join(output_dir, filename), data)

        for form in state["forms_json"]:
            _write(os.path.join(forms_dir, form["file_name"]), form["content"])
    except OSError as exc:
        errors.append(f"Failed to write output files: {exc}")
        return {**state, "errors": errors}

    # Build ZIP in canonical order
    zip_path = os.path.join(output_dir, f"{org_name.capitalize()}.zip")
    try:
        file_map: dict[str, str] = {
            **{name: os.path.join(output_dir, name) for name in flat_files},
            **{
                f"forms/{f['file_name']}": os.path.join(forms_dir, f["file_name"])
                for f in state["forms_json"]
            },
        }
        form_entries = sorted(k for k in file_map if k.startswith("forms/"))

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in _CANONICAL_ORDER:
                if entry == "__FORMS__":
                    for fe in form_entries:
                        zf.write(file_map[fe], fe)
                elif entry in file_map:
                    zf.write(file_map[entry], entry)
    except OSError as exc:
        errors.append(f"Failed to create ZIP: {exc}")
        return {**state, "errors": errors}

    log.info("[%s] Bundle written → %s", org_name, zip_path)
    return {**state, "zip_path": zip_path, "errors": errors}


# ── Conditional edge ──────────────────────────────────────────────────────────


def _can_proceed(state: BundleState) -> str:
    """Abort after discover_files or parse_documents if hard errors exist."""
    if state.get("errors") and not state.get("file_paths"):
        return "abort"
    if state.get("errors") and state.get("entity_spec") is None:
        return "abort"
    return "continue"


# ── Graph ─────────────────────────────────────────────────────────────────────


def build_graph() -> Any:
    graph = StateGraph(BundleState)

    graph.add_node("discover_files", discover_files)
    graph.add_node("parse_documents", parse_documents)
    graph.add_node("generate_entities", generate_entities)
    graph.add_node("generate_forms", generate_forms)
    graph.add_node("generate_form_mappings", generate_form_mappings)
    graph.add_node("package_zip", package_zip)

    graph.set_entry_point("discover_files")
    graph.add_conditional_edges(
        "discover_files", _can_proceed,
        {"continue": "parse_documents", "abort": END},
    )
    graph.add_conditional_edges(
        "parse_documents", _can_proceed,
        {"continue": "generate_entities", "abort": END},
    )
    graph.add_edge("generate_entities", "generate_forms")
    graph.add_edge("generate_forms", "generate_form_mappings")
    graph.add_edge("generate_form_mappings", "package_zip")
    graph.add_edge("package_zip", END)

    return graph.compile()


# ── Public entry point ────────────────────────────────────────────────────────


def run(org_name: str, input_dir: str, output_dir: str) -> BundleState:
    pipeline = build_graph()
    initial: BundleState = {
        "org_name": org_name,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "file_paths": [],
        "entity_spec": None,
        "parse_warnings": [],
        "subject_types_json": [],
        "operational_subject_types_json": {},
        "programs_json": [],
        "operational_programs_json": {},
        "encounter_types_json": [],
        "operational_encounter_types_json": {},
        "address_level_types_json": [],
        "organisation_config_json": {},
        "forms_json": [],
        "concepts_json": [],
        "form_specs_for_mapping": [],
        "form_mappings_json": [],
        "zip_path": "",
        "errors": [],
    }
    return pipeline.invoke(initial)

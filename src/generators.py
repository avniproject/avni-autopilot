"""
Pure JSON-generation functions for an Avni bundle.

Each `make_*` function takes parsed `EntitySpec` content and returns the
corresponding JSON-serialisable structure. No filesystem, no state — these
are easy to unit-test and reuse.

UUIDs are deterministic (UUID v5 over a fixed namespace + a seed string),
so re-running the generator produces identical output and Avni's upsert-by-UUID
behaviour makes uploads idempotent.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Callable

# ── UUID + naming helpers ─────────────────────────────────────────────────────

# Stable namespace shared with avni-ai's bundle generator
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "avni.project.org")


def make_uuid(seed: str) -> str:
    """Deterministic UUID v5 — same seed always returns the same UUID."""
    return str(uuid.uuid5(_NAMESPACE, seed))


def safe_filename(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace(":", "_")


# ── Subject types ─────────────────────────────────────────────────────────────


def make_subject_types(subject_types: list) -> list[dict]:
    """SubjectTypeSpec list → subjectTypes.json."""
    out: list[dict] = []
    for st in subject_types:
        st_type = st.type  # "Person", "Individual", "Household", "Group"
        is_household = st_type.lower() == "household"
        is_group = st_type.lower() in ("group", "household")
        out.append({
            "name": st.name,
            "uuid": make_uuid(f"subjectType:{st.name}"),
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
    return out


def make_operational_subject_types(subject_types_json: list[dict]) -> dict:
    return {
        "operationalSubjectTypes": [
            {
                "uuid": make_uuid(f"operationalSubjectType:{st['name']}"),
                "name": st["name"],
                "subjectType": {"uuid": st["uuid"], "voided": False},
                "voided": False,
            }
            for st in subject_types_json
        ]
    }


# ── Programs ──────────────────────────────────────────────────────────────────


def make_programs(programs: list) -> list[dict]:
    """ProgramSpec list → programs.json."""
    return [
        {
            "name": p.name,
            "uuid": make_uuid(f"program:{p.name}"),
            "colour": p.colour,
            "voided": False,
            "active": True,
            "enrolmentEligibilityCheckRule": "",
            "enrolmentSummaryRule": "",
            "manualEligibilityCheckRequired": False,
            "allowMultipleEnrolments": p.allow_multiple_enrolments,
        }
        for p in programs
    ]


def make_operational_programs(programs_json: list[dict]) -> dict:
    return {
        "operationalPrograms": [
            {
                "uuid": make_uuid(f"operationalProgram:{p['name']}"),
                "name": p["name"],
                "program": {"uuid": p["uuid"], "voided": False},
                "programSubjectLabel": "",
                "voided": False,
            }
            for p in programs_json
        ]
    }


# ── Encounter types ───────────────────────────────────────────────────────────


def make_encounter_types(encounter_types: list) -> list[dict]:
    """EncounterTypeSpec list → encounterTypes.json."""
    return [
        {
            "name": et.name,
            "uuid": make_uuid(f"encounterType:{et.name}"),
            "encounterEligibilityCheckRule": "",
            "active": True,
            "immutable": False,
        }
        for et in encounter_types
    ]


def make_operational_encounter_types(encounter_types_json: list[dict]) -> dict:
    return {
        "operationalEncounterTypes": [
            {
                "uuid": make_uuid(f"operationalEncounterType:{et['name']}"),
                "name": et["name"],
                "encounterType": {"uuid": et["uuid"], "voided": False},
                "voided": False,
            }
            for et in encounter_types_json
        ]
    }


# ── Address level types ───────────────────────────────────────────────────────


def make_address_level_types(address_levels: list) -> list[dict]:
    """AddressLevelSpec list → addressLevelTypes.json (parents linked by UUID)."""
    sorted_levels = sorted(address_levels, key=lambda a: a.level, reverse=True)
    name_to_uuid = {al.name: make_uuid(f"addressLevelType:{al.name}") for al in sorted_levels}

    out: list[dict] = []
    for al in sorted_levels:
        entry: dict = {
            "uuid": name_to_uuid[al.name],
            "name": al.name,
            "level": float(al.level),
        }
        if al.parent and al.parent in name_to_uuid:
            entry["parent"] = {"uuid": name_to_uuid[al.parent]}
        out.append(entry)
    return out


# ── Organisation config ───────────────────────────────────────────────────────


def make_organisation_config(org_name: str) -> dict:
    return {
        "uuid": make_uuid(f"orgConfig:{org_name}"),
        "settings": {
            "languages": ["en"],
            "customRegistrationLocations": [],
            "searchFilters": [],
            "myDashboardFilters": [],
        },
        "worklistUpdationRule": "",
    }


# ── Forms + concepts ──────────────────────────────────────────────────────────

CANCELLATION_OPTIONS = [
    "Data entry error",
    "Rescheduled",
    "Participant not available",
    "Other",
]


def _build_form(
    name: str,
    form_type: str,
    sections: list,           # list of SectionSpec-like (has .name, .fields)
    concepts_registry: dict,  # mutated in-place: lower(name) → concept dict
) -> dict:
    """Build a form JSON dict. Registers all concepts encountered into the registry."""
    form_uuid = make_uuid(f"form:{name}")
    form_element_groups: list[dict] = []

    for g_idx, section in enumerate(sections, start=1):
        group_uuid = make_uuid(f"formGroup:{name}:{section.name}")
        form_elements: list[dict] = []

        for e_idx, field in enumerate(section.fields, start=1):
            concept_name = field.name
            concept_uuid = make_uuid(f"concept:{concept_name}")

            # Build the answer list for Coded fields. The LLM enrichment pass
            # has already validated/disambiguated upstream, so no dedup here.
            answers: list[dict] = []
            if field.dataType == "Coded" and field.options:
                for a_idx, opt in enumerate(field.options):
                    answers.append({
                        "name": opt,
                        "uuid": make_uuid(f"concept:{opt}"),
                        "order": a_idx,
                        "active": True,
                    })

            # Register concept (key by lowercased name; LLM has resolved dupes)
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
                "uuid": make_uuid(f"formElement:{name}:{concept_name}"),
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
            "name": section.name,
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
    """Auto-generate a cancellation form for an Encounter or ProgramEncounter form."""
    if parent_form_type == "ProgramEncounter":
        cancel_name = f"{parent_name} Encounter Cancellation"
        cancel_form_type = "ProgramEncounterCancellation"
    else:
        cancel_name = f"{parent_name} Cancellation"
        cancel_form_type = "IndividualEncounterCancellation"

    reason_field = SimpleNamespace(
        name="Reason for Cancellation",
        dataType="Coded",
        mandatory=True,
        options=CANCELLATION_OPTIONS,
        unit=None,
        min=None,
        max=None,
        selectionType="SingleSelect",
    )
    cancel_section = SimpleNamespace(name="Cancellation Details", fields=[reason_field])
    form_dict = _build_form(cancel_name, cancel_form_type, [cancel_section], concepts_registry)
    return cancel_name, cancel_form_type, form_dict


def make_forms_and_concepts(forms: list) -> dict:
    """
    FormSpec list → forms (with auto-cancellations) + deduped concepts + mapping specs.

    Returns:
        {
          "forms":         [ {file_name, content}, ... ],   # ready to write to forms/<file>
          "concepts":      [ concept dicts for concepts.json ],
          "mapping_specs": [ {name, uuid, form_type, subject_type, program, encounter_type} ],
        }
    """
    concepts_registry: dict[str, dict] = {}
    forms_out: list[dict] = []
    mapping_specs: list[dict] = []

    for form_spec in forms:
        # Use sections if present; otherwise wrap all fields in a single "Details" section
        if form_spec.sections:
            sections = form_spec.sections
        elif form_spec.fields:
            sections = [SimpleNamespace(name="Details", fields=form_spec.fields)]
        else:
            sections = []

        form_dict = _build_form(form_spec.name, form_spec.formType, sections, concepts_registry)
        forms_out.append({
            "file_name": f"{safe_filename(form_spec.name)}_{form_dict['uuid']}.json",
            "content": form_dict,
        })
        mapping_specs.append({
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
            forms_out.append({
                "file_name": f"{safe_filename(c_name)}_{c_dict['uuid']}.json",
                "content": c_dict,
            })
            mapping_specs.append({
                "name": c_name,
                "uuid": c_dict["uuid"],
                "form_type": c_type,
                "subject_type": form_spec.subjectType or "",
                "program": form_spec.program or "",
                "encounter_type": form_spec.encounterType or "",
            })

    return {
        "forms": forms_out,
        "concepts": list(concepts_registry.values()),
        "mapping_specs": mapping_specs,
    }


# ── Form mappings ─────────────────────────────────────────────────────────────

_PROGRAM_FORM_TYPES = {
    "ProgramEnrolment", "ProgramExit",
    "ProgramEncounter", "ProgramEncounterCancellation",
}

_ENCOUNTER_FORM_TYPES = {
    "Encounter", "IndividualEncounterCancellation",
    "ProgramEncounter", "ProgramEncounterCancellation",
}


def make_form_mappings(
    mapping_specs: list[dict],
    subject_types_json: list[dict],
    programs_json: list[dict],
    encounter_types_json: list[dict],
    fuzzy_match: Callable[[str, set[str]], str | None] | None = None,
) -> list[dict]:
    """
    Build formMappings.json by resolving each mapping spec's name references
    against the entity UUID lookups. `fuzzy_match` is optional — when provided,
    used to resolve names that don't match exactly (typos, trailing spaces).
    """
    st_uuid = {s["name"].lower(): s["uuid"] for s in subject_types_json}
    prog_uuid = {p["name"].lower(): p["uuid"] for p in programs_json}
    et_uuid = {e["name"].lower(): e["uuid"] for e in encounter_types_json}

    def _resolve(name: str, lookup: dict[str, str]) -> str:
        if not name:
            return ""
        key = name.lower()
        if key in lookup:
            return lookup[key]
        if fuzzy_match:
            match = fuzzy_match(name, set(lookup.keys()))
            if match:
                return lookup[match]
        return ""

    mappings: list[dict] = []
    for spec in mapping_specs:
        form_type = spec["form_type"]
        resolved_st = _resolve(spec["subject_type"], st_uuid)
        resolved_prog = _resolve(spec["program"], prog_uuid)
        resolved_et = _resolve(spec["encounter_type"], et_uuid)

        entry: dict = {
            "uuid": make_uuid(f"mapping:{spec['name']}"),
            "formUUID": spec["uuid"],
            "formType": form_type,
            "formName": spec["name"],
            "enableApproval": False,
        }
        if resolved_st:
            entry["subjectTypeUUID"] = resolved_st
        if form_type in _PROGRAM_FORM_TYPES and resolved_prog:
            entry["programUUID"] = resolved_prog
        if form_type in _ENCOUNTER_FORM_TYPES and resolved_et:
            entry["encounterTypeUUID"] = resolved_et

        mappings.append(entry)

    return mappings

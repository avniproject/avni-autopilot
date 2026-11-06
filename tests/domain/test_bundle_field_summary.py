"""Unit tests for `list_bundle_fields` and `load_form_rule_context` in
`src/domain/bundle_editor.py`: IDs per field and the subject type, program
and encounter type each form is mapped to.

Usage:
    uv run --extra dev pytest tests/domain/test_bundle_field_summary.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.bundle_editor import list_bundle_fields, load_form_rule_context

_SUBJECT_UUID = "subject-individual"
_PROGRAM_UUID = "program-pregnancy"
_ANC_UUID = "encounter-anc"
_HOME_VISIT_UUID = "encounter-home-visit"
_ANC_FORM_UUID = "form-anc"
_SHARED_FORM_UUID = "form-shared-visit"
_UNMAPPED_FORM_UUID = "form-unmapped"


def _form(uuid: str, name: str, form_type: str, elements: list[dict]) -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "formType": form_type,
        "formElementGroups": [{"name": "Details", "formElements": elements}],
    }


def _element(uuid: str, name: str, concept_uuid: str, data_type: str = "Numeric") -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "concept": {"uuid": concept_uuid, "name": name, "dataType": data_type},
    }


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    files = {
        "subjectTypes.json": [{"uuid": _SUBJECT_UUID, "name": "Individual"}],
        "programs.json": [{"uuid": _PROGRAM_UUID, "name": "Pregnancy"}],
        "encounterTypes.json": [
            {"uuid": _ANC_UUID, "name": "ANC"},
            {"uuid": _HOME_VISIT_UUID, "name": "Home Visit"},
        ],
        "formMappings.json": [
            {
                "uuid": "mapping-anc",
                "formUUID": _ANC_FORM_UUID,
                "subjectTypeUUID": _SUBJECT_UUID,
                "programUUID": _PROGRAM_UUID,
                "encounterTypeUUID": _ANC_UUID,
            },
            {
                "uuid": "mapping-shared-1",
                "formUUID": _SHARED_FORM_UUID,
                "subjectTypeUUID": _SUBJECT_UUID,
                "encounterTypeUUID": _HOME_VISIT_UUID,
            },
            {
                "uuid": "mapping-shared-2",
                "formUUID": _SHARED_FORM_UUID,
                "subjectTypeUUID": _SUBJECT_UUID,
                "encounterTypeUUID": _ANC_UUID,
            },
            {
                "uuid": "mapping-shared-voided",
                "formUUID": _SHARED_FORM_UUID,
                "subjectTypeUUID": _SUBJECT_UUID,
                "encounterTypeUUID": "encounter-deleted",
                "voided": True,
            },
        ],
    }
    for name, content in files.items():
        (tmp_path / name).write_text(json.dumps(content), encoding="utf-8")
    forms_dir = tmp_path / "forms"
    forms_dir.mkdir()
    forms = [
        _form(_ANC_FORM_UUID, "ANC", "ProgramEncounter",
              [_element("fe-anc-weight", "Weight", "concept-weight")]),
        _form(_SHARED_FORM_UUID, "Shared Visit", "Encounter",
              [_element("fe-shared-weight", "Weight", "concept-weight")]),
        _form(_UNMAPPED_FORM_UUID, "Unmapped", "Encounter", []),
    ]
    for form in forms:
        (forms_dir / f"{form['name']}.json").write_text(json.dumps(form), encoding="utf-8")
    return tmp_path


def _form_summary(bundle_dir: Path, form_name: str) -> dict:
    summary = list_bundle_fields(str(bundle_dir))
    return next(f for f in summary["forms"] if f["form"] == form_name)


def test_field_carries_form_element_and_concept_uuid(bundle_dir: Path) -> None:
    field = _form_summary(bundle_dir, "ANC")["sections"][0]["fields"][0]
    assert field["uuid"] == "fe-anc-weight"
    assert field["conceptUuid"] == "concept-weight"


def test_form_carries_uuid_and_resolved_mapping_names(bundle_dir: Path) -> None:
    form = _form_summary(bundle_dir, "ANC")
    assert form["uuid"] == _ANC_FORM_UUID
    assert form["usedIn"] == [
        {"subjectType": "Individual", "program": "Pregnancy", "encounterType": "ANC"},
    ]


def test_form_with_several_mappings_lists_each_and_skips_voided(bundle_dir: Path) -> None:
    form = _form_summary(bundle_dir, "Shared Visit")
    assert form["usedIn"] == [
        {"subjectType": "Individual", "encounterType": "Home Visit"},
        {"subjectType": "Individual", "encounterType": "ANC"},
    ]


def test_unmapped_form_has_empty_used_in(bundle_dir: Path) -> None:
    assert _form_summary(bundle_dir, "Unmapped")["usedIn"] == []


def test_rule_context_still_resolves_mapping_names(bundle_dir: Path) -> None:
    context = load_form_rule_context(str(bundle_dir), "ANC")
    assert context is not None
    assert context["subject_type"] == "Individual"
    assert context["program"] == "Pregnancy"
    assert context["encounter_type"] == "ANC"
    assert context["available_encounter_types"] == ["ANC", "Home Visit"]
    assert context["available_programs"] == ["Pregnancy"]

"""Unit tests for `list_bundle_fields`, `find_bundle_ids` and
`load_form_rule_context` in `src/domain/bundle_editor.py`: IDs for the one
form asked about, ID lookup, deleted forms left out, and the subject type,
program and encounter type each form is mapped to.

Usage:
    uv run --extra dev pytest tests/domain/test_bundle_field_summary.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.bundle_editor import (
    find_bundle_ids,
    list_bundle_fields,
    load_form_rule_context,
    split_pasted_ids,
)

_SUBJECT_UUID = "subject-individual"
_PROGRAM_UUID = "program-pregnancy"
_ANC_UUID = "encounter-anc"
_HOME_VISIT_UUID = "encounter-home-visit"
_ANC_FORM_UUID = "form-anc"
_SHARED_FORM_UUID = "form-shared-visit"
_UNMAPPED_FORM_UUID = "form-unmapped"
_DELETED_FORM_UUID = "form-deleted"
_DUP_A_UUID = "form-dup-a"
_DUP_B_UUID = "form-dup-b"


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
              [_element("fe-anc-weight", "Weight", "concept-weight"),
               {**_element("fe-anc-old", "Old Height", "concept-old-height"), "voided": True}]),
        _form(_SHARED_FORM_UUID, "Shared Visit", "Encounter",
              [_element("fe-shared-weight", "Weight", "concept-weight")]),
        _form(_UNMAPPED_FORM_UUID, "Unmapped", "Encounter", []),
        {**_form(_DELETED_FORM_UUID, "Deleted", "Encounter",
                 [_element("fe-deleted", "Weight", "concept-weight")]), "voided": True},
        {**_form("form-unmapped-deleted", "Unmapped", "Encounter", []), "voided": True},
        _form(_DUP_A_UUID, "Dup", "Encounter", []),
        _form(_DUP_B_UUID, "Dup", "Encounter", []),
    ]
    for form in forms:
        (forms_dir / f"{form['uuid']}.json").write_text(json.dumps(form), encoding="utf-8")
    return tmp_path


def _form_summary(bundle_dir: Path, form_name: str) -> dict:
    summary = list_bundle_fields(str(bundle_dir))
    return next(f for f in summary["forms"] if f["form"] == form_name)


def test_full_list_carries_no_ids(bundle_dir: Path) -> None:
    form = _form_summary(bundle_dir, "ANC")
    assert "uuid" not in form
    field = form["sections"][0]["fields"][0]
    assert "uuid" not in field
    assert "conceptUuid" not in field
    assert field["conceptName"] == "Weight"


def test_lists_leave_out_deleted_fields(bundle_dir: Path) -> None:
    full = _form_summary(bundle_dir, "ANC")
    [one] = list_bundle_fields(str(bundle_dir), form_name="ANC")["forms"]
    for form in (full, one):
        assert [f["name"] for f in form["sections"][0]["fields"]] == ["Weight"]
        assert "voided" not in form["sections"][0]["fields"][0]


def test_find_ids_reports_a_deleted_field(bundle_dir: Path) -> None:
    [match] = find_bundle_ids(str(bundle_dir), ["fe-anc-old"])["matches"]["fe-anc-old"]
    assert (match["form"], match["field"], match["fieldVoided"]) == ("ANC", "Old Height", True)


@pytest.mark.parametrize("pasted", [
    ["FE-ANC-WEIGHT"],
    ['"fe-anc-weight"'],
    ["'Fe-Anc-Weight',"],
    ["(fe-anc-weight)"],
])
def test_find_ids_ignores_case_quotes_and_brackets(bundle_dir: Path, pasted: list[str]) -> None:
    matches = find_bundle_ids(str(bundle_dir), pasted)["matches"]
    assert list(matches) == ["fe-anc-weight"]
    assert [m["field"] for m in matches["fe-anc-weight"]] == ["Weight"]


def test_find_ids_splits_several_ids_in_one_entry(bundle_dir: Path) -> None:
    matches = find_bundle_ids(
        str(bundle_dir), ['"FE-ANC-WEIGHT", \'form-anc\'; nope\nform-anc'],
    )["matches"]
    assert list(matches) == ["fe-anc-weight", "form-anc", "nope"]
    assert matches["form-anc"][0]["matchedAs"] == "form"
    assert matches["nope"] == []


def test_split_pasted_ids_drops_blanks_and_wrappers() -> None:
    assert split_pasted_ids(["", " , ", '""', "[A]"]) == ["a"]


def test_full_list_leaves_out_deleted_forms(bundle_dir: Path) -> None:
    names = [f["form"] for f in list_bundle_fields(str(bundle_dir))["forms"]]
    assert "Deleted" not in names
    assert list_bundle_fields(str(bundle_dir), form_name="Deleted")["forms"] == []


def test_full_list_gives_form_uuid_only_for_shared_names(bundle_dir: Path) -> None:
    forms = list_bundle_fields(str(bundle_dir))["forms"]
    dups = sorted(f["uuid"] for f in forms if f["form"] == "Dup")
    assert dups == [_DUP_A_UUID, _DUP_B_UUID]
    assert all("uuid" not in f for f in forms if f["form"] not in ("Dup", "Unmapped"))


def test_full_list_gives_form_uuid_when_a_deleted_form_shares_the_name(
    bundle_dir: Path,
) -> None:
    # edit_bundle_fields counts deleted forms when it matches a form name, so
    # the live copy needs its uuid to be editable.
    [unmapped] = [
        f for f in list_bundle_fields(str(bundle_dir))["forms"] if f["form"] == "Unmapped"
    ]
    assert unmapped["uuid"] == _UNMAPPED_FORM_UUID


def test_one_form_carries_form_element_and_concept_uuid(bundle_dir: Path) -> None:
    forms = list_bundle_fields(str(bundle_dir), form_name="ANC")["forms"]
    assert [f["form"] for f in forms] == ["ANC"]
    assert forms[0]["uuid"] == _ANC_FORM_UUID
    field = forms[0]["sections"][0]["fields"][0]
    assert field["uuid"] == "fe-anc-weight"
    assert field["conceptUuid"] == "concept-weight"


def test_one_form_by_uuid_among_shared_names(bundle_dir: Path) -> None:
    both = list_bundle_fields(str(bundle_dir), form_name="Dup")["forms"]
    assert sorted(f["uuid"] for f in both) == [_DUP_A_UUID, _DUP_B_UUID]
    one = list_bundle_fields(str(bundle_dir), form_name="Dup", form_uuid=_DUP_B_UUID)["forms"]
    assert [f["uuid"] for f in one] == [_DUP_B_UUID]


def test_find_ids_matches_field_concept_and_form(bundle_dir: Path) -> None:
    matches = find_bundle_ids(
        str(bundle_dir), ["fe-anc-weight", "concept-weight", _ANC_FORM_UUID, "nope"],
    )["matches"]
    [field] = matches["fe-anc-weight"]
    assert (field["form"], field["section"], field["field"], field["matchedAs"]) == (
        "ANC", "Details", "Weight", "field",
    )
    # The concept sits on ANC and Shared Visit; the deleted form is skipped.
    assert sorted(m["form"] for m in matches["concept-weight"]) == ["ANC", "Shared Visit"]
    assert all(m["matchedAs"] == "concept" for m in matches["concept-weight"])
    assert matches[_ANC_FORM_UUID] == [{
        "form": "ANC", "formUuid": _ANC_FORM_UUID, "matchedAs": "form",
        "usedIn": [{"subjectType": "Individual", "program": "Pregnancy", "encounterType": "ANC"}],
    }]
    assert matches["nope"] == []


def test_find_ids_marks_an_id_found_only_on_a_deleted_form(bundle_dir: Path) -> None:
    matches = find_bundle_ids(str(bundle_dir), ["fe-deleted", _DELETED_FORM_UUID])["matches"]
    [field] = matches["fe-deleted"]
    assert (field["form"], field["field"], field["matchedAs"], field["formDeleted"]) == (
        "Deleted", "Weight", "field", True,
    )
    [form] = matches[_DELETED_FORM_UUID]
    assert (form["matchedAs"], form["formDeleted"]) == ("form", True)


def test_find_ids_keeps_only_live_matches_when_an_id_is_on_both(bundle_dir: Path) -> None:
    # concept-weight is on ANC, Shared Visit and the deleted form; a field
    # ID only on the deleted form, pasted with it, keeps its deleted match.
    matches = find_bundle_ids(str(bundle_dir), ["concept-weight", "fe-deleted"])["matches"]
    assert all("formDeleted" not in m for m in matches["concept-weight"])
    assert [m["formDeleted"] for m in matches["fe-deleted"]] == [True]



def test_form_carries_resolved_mapping_names(bundle_dir: Path) -> None:
    form = _form_summary(bundle_dir, "ANC")
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


def test_unlinked_form_names_counts_forms_that_share_a_uuid() -> None:
    from domain.generators import unlinked_form_names, unlinked_forms

    forms = [{"content": {"uuid": "u1", "name": "A"}}, {"content": {"uuid": "u1", "name": "A"}}]
    assert unlinked_form_names(forms, []) == ["A", "A"]
    assert unlinked_forms(forms, []) == {"u1": "A"}

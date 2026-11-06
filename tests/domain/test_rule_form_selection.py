"""Unit tests for picking the form a rule targets when several forms share a
name (`load_form_rule_context` / `write_form_rule` in
`src/domain/bundle_editor.py`) and for the error the chat rule tools return.

Usage:
    uv run --extra dev pytest tests/domain/test_rule_form_selection.py
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chat import tools
from domain.bundle_editor import (
    AmbiguousFormError,
    load_form_rule_context,
    write_form_rule,
)

_FORM_NAME = "QA Program Visit Encounter"
_SUBJECT_UUID = "subject-individual"
_PROGRAM_UUID = "program-qa"
_VISIT_UUID = "encounter-qa-visit"
_FOLLOWUP_UUID = "encounter-qa-followup"


def _form(uuid: str, field_names: list[str], *, voided: bool = False) -> dict:
    return {
        "uuid": uuid,
        "name": _FORM_NAME,
        "formType": "ProgramEncounter",
        "voided": voided,
        "formElementGroups": [{
            "name": "Details",
            "formElements": [
                {
                    "uuid": f"fe-{uuid}-{name}",
                    "name": name,
                    "concept": {"uuid": f"concept-{name}", "name": name, "dataType": "Numeric"},
                }
                for name in field_names
            ],
        }],
    }


def _mapping(uuid: str, form_uuid: str, encounter_type_uuid: str, *, voided: bool = False) -> dict:
    return {
        "uuid": uuid,
        "formUUID": form_uuid,
        "subjectTypeUUID": _SUBJECT_UUID,
        "programUUID": _PROGRAM_UUID,
        "encounterTypeUUID": encounter_type_uuid,
        "voided": voided,
    }


def _write_bundle(root: Path, forms: list[dict], mappings: list[dict]) -> Path:
    files = {
        "subjectTypes.json": [{"uuid": _SUBJECT_UUID, "name": "Individual"}],
        "programs.json": [{"uuid": _PROGRAM_UUID, "name": "QA Program"}],
        "encounterTypes.json": [
            {"uuid": _VISIT_UUID, "name": "QA Visit"},
            {"uuid": _FOLLOWUP_UUID, "name": "QA Followup"},
        ],
        "formMappings.json": mappings,
    }
    for name, content in files.items():
        (root / name).write_text(json.dumps(content), encoding="utf-8")
    forms_dir = root / "forms"
    forms_dir.mkdir()
    # The unmapped copy sorts first, as in the reported bundle.
    for index, form in enumerate(forms):
        (forms_dir / f"{index}-{form['uuid']}.json").write_text(json.dumps(form), encoding="utf-8")
    return root


@pytest.fixture
def one_mapped_copy(tmp_path: Path) -> Path:
    return _write_bundle(
        tmp_path,
        [_form("form-unmapped", []), _form("form-mapped", ["Weight", "Height"])],
        [_mapping("m-1", "form-mapped", _VISIT_UUID)],
    )


@pytest.fixture
def two_mapped_copies(tmp_path: Path) -> Path:
    return _write_bundle(
        tmp_path,
        [_form("form-visit", ["Weight"]), _form("form-followup", ["Height"])],
        [
            _mapping("m-visit", "form-visit", _VISIT_UUID),
            _mapping("m-followup", "form-followup", _FOLLOWUP_UUID),
        ],
    )


def _form_json(bundle: Path, form_uuid: str) -> dict:
    for path in (bundle / "forms").iterdir():
        form = json.loads(path.read_text(encoding="utf-8"))
        if form["uuid"] == form_uuid:
            return form
    raise AssertionError(form_uuid)


def test_rule_context_picks_the_only_mapped_copy(one_mapped_copy: Path) -> None:
    context = load_form_rule_context(str(one_mapped_copy), _FORM_NAME)
    assert context is not None
    assert context["available_concepts"] == ["Height", "Weight"]
    assert context["encounter_type"] == "QA Visit"


def test_rule_context_skips_voided_form_copy(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path,
        [_form("form-voided", ["Old"], voided=True), _form("form-live", ["Weight"])],
        [],
    )
    context = load_form_rule_context(str(bundle), _FORM_NAME)
    assert context is not None
    assert context["available_concepts"] == ["Weight"]


def test_rule_context_ignores_voided_mapping(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path,
        [_form("form-a", ["Weight"])],
        [
            _mapping("m-old", "form-a", _FOLLOWUP_UUID, voided=True),
            _mapping("m-live", "form-a", _VISIT_UUID),
        ],
    )
    context = load_form_rule_context(str(bundle), _FORM_NAME)
    assert context is not None
    assert context["encounter_type"] == "QA Visit"


def test_rule_context_raises_with_candidates_when_several_copies_are_mapped(
    two_mapped_copies: Path,
) -> None:
    with pytest.raises(AmbiguousFormError) as raised:
        load_form_rule_context(str(two_mapped_copies), _FORM_NAME)
    candidates = {c["uuid"]: c["usedIn"] for c in raised.value.candidates}
    assert candidates == {
        "form-visit": [{"subjectType": "Individual", "program": "QA Program", "encounterType": "QA Visit"}],
        "form-followup": [{"subjectType": "Individual", "program": "QA Program", "encounterType": "QA Followup"}],
    }


def test_rule_context_uses_form_uuid_to_pick_a_copy(two_mapped_copies: Path) -> None:
    context = load_form_rule_context(str(two_mapped_copies), _FORM_NAME, "form-followup")
    assert context is not None
    assert context["available_concepts"] == ["Height"]
    assert context["encounter_type"] == "QA Followup"


def test_rule_context_rejects_form_uuid_of_another_form(two_mapped_copies: Path) -> None:
    assert load_form_rule_context(str(two_mapped_copies), _FORM_NAME, "form-unknown") is None


def test_write_form_rule_lands_on_the_mapped_copy(one_mapped_copy: Path) -> None:
    written = write_form_rule(
        str(one_mapped_copy), _FORM_NAME, "formElementRule", "rule-js",
        page_name="Details", field_name="Weight",
    )
    assert written
    element = _form_json(one_mapped_copy, "form-mapped")["formElementGroups"][0]["formElements"][0]
    assert element["rule"] == "rule-js"


def test_field_rule_tool_returns_copies_to_choose_from(two_mapped_copies: Path) -> None:
    session = SimpleNamespace(bundle_path=two_mapped_copies)
    result = tools.suggest_form_element_rule.invoke(
        {
            "form_name": _FORM_NAME,
            "page_name": "Details",
            "field_name": "Weight",
            "intent": "show when 'Height' is greater than 100",
        },
        config={"configurable": {"session": session}},
    )
    assert result["status"] == "error"
    assert "2 forms are named" in result["error"]
    assert {form["uuid"] for form in result["forms"]} == {"form-visit", "form-followup"}

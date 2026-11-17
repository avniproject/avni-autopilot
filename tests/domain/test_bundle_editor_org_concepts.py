"""Chat edits on an organisation set up outside Autopilot use its own concepts
(`domain.bundle_editor`).

Concepts made in App Designer have random UUIDs. avni-server refuses a concept
whose name it already holds under another UUID ("Concept with name 'Yes'
already exists with different UUID"), and the form that refers to it then
fails too, so a field added in the chat never reached the organisation. An
answer or field concept the organisation already has must keep its UUID.

Usage:
    uv run --extra dev pytest tests/domain/test_bundle_editor_org_concepts.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.bundle_editor import apply_field_edits
from domain.generators import make_uuid

FORM = "Women Registration"
PAGE = "1day orientation with individual"
TARGET = {"form": FORM, "section": PAGE}

YES = {"name": "Yes", "uuid": "8509a321-a467-4ce6-8e05-c89ef0fcc832", "dataType": "NA", "answers": []}
NO = {"name": "No", "uuid": "1f2c1a55-0000-4000-8000-0000000000a1", "dataType": "NA", "answers": []}
ATTENDED = {
    "name": "Orientation Training attended", "uuid": "f64b4a3f-0000-4000-8000-0000000000a2",
    "dataType": "Coded",
    "answers": [{"name": "No", "uuid": NO["uuid"], "order": 0.0}, {"name": "Yes", "uuid": YES["uuid"], "order": 1.0}],
}
MOBILE = {"name": "Mobile Number", "uuid": "0a0a0a0a-0000-4000-8000-0000000000a3", "dataType": "Numeric", "answers": []}


def _org_bundle(tmp_path: Path, concepts: list[dict] | None = None) -> Path:
    """A bundle as avni-server exports an organisation made in App Designer."""
    element = {
        "uuid": "ea5a918e-0000-4000-8000-0000000000b1", "name": ATTENDED["name"],
        "displayOrder": 1, "mandatory": True, "type": "SingleSelect", "keyValues": [], "voided": False,
        "concept": {k: ATTENDED[k] for k in ("name", "uuid", "dataType", "answers")},
    }
    form = {"name": FORM, "uuid": "b4d84445-0000-4000-8000-0000000000c1", "formType": "IndividualProfile",
            "formElementGroups": [{"uuid": "group-1", "name": PAGE, "displayOrder": 1, "formElements": [element]}]}
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "women.json").write_text(json.dumps(form), encoding="utf-8")
    all_concepts = concepts if concepts is not None else [YES, NO, ATTENDED, MOBILE]
    (tmp_path / "concepts.json").write_text(json.dumps(all_concepts), encoding="utf-8")
    return tmp_path


def _form(bundle: Path) -> dict:
    return json.loads((bundle / "forms" / "women.json").read_text(encoding="utf-8"))


def _concepts(bundle: Path) -> list[dict]:
    return json.loads((bundle / "concepts.json").read_text(encoding="utf-8"))


def _element(bundle: Path, name: str) -> dict:
    return next(e for g in _form(bundle)["formElementGroups"] for e in g["formElements"]
                if e["name"] == name and not e.get("voided"))


def _add(bundle: Path, **payload) -> object:
    return apply_field_edits(str(bundle), [{"op_id": "op-1", "kind": "field.add", "target": TARGET, "payload": payload}])


def _no_name_under_two_uuids(bundle: Path) -> None:
    """What avni-server checks on import: one UUID per concept name."""
    seen: dict[str, str] = {}
    for concept in _concepts(bundle):
        for entry in [concept, *concept.get("answers", [])]:
            key = entry["name"].strip().lower()
            assert seen.setdefault(key, entry["uuid"]) == entry["uuid"], entry["name"]


def test_a_coded_field_added_in_chat_uses_the_organisations_yes_and_no(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    result = _add(bundle, name="Orientation happened", dataType="Coded", options=["Yes", "No"],
                  selectionType="SingleSelect")

    assert result.rejected == []
    answers = _element(bundle, "Orientation happened")["concept"]["answers"]
    assert [(a["name"], a["uuid"]) for a in answers] == [("Yes", YES["uuid"]), ("No", NO["uuid"])]
    new_concept = next(c for c in _concepts(bundle) if c["name"] == "Orientation happened")
    assert [a["uuid"] for a in new_concept["answers"]] == [YES["uuid"], NO["uuid"]]
    _no_name_under_two_uuids(bundle)


def test_an_answer_is_matched_ignoring_case_and_keeps_the_organisations_spelling(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    _add(bundle, name="Orientation happened", dataType="Coded", options=["yes", " NO "])

    answers = _element(bundle, "Orientation happened")["concept"]["answers"]
    assert [(a["name"], a["uuid"]) for a in answers] == [("Yes", YES["uuid"]), ("No", NO["uuid"])]


def test_an_option_the_organisation_does_not_have_gets_a_new_concept(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    _add(bundle, name="Orientation happened", dataType="Coded", options=["Yes", "Partly"])

    answers = _element(bundle, "Orientation happened")["concept"]["answers"]
    assert [(a["name"], a["uuid"]) for a in answers] == [("Yes", YES["uuid"]), ("Partly", make_uuid("concept:Partly"))]
    _no_name_under_two_uuids(bundle)


def test_an_answer_known_only_from_a_form_is_reused(tmp_path: Path) -> None:
    """A parent organisation's concepts appear on forms but not in concepts.json."""
    bundle = _org_bundle(tmp_path, concepts=[])
    _add(bundle, name="Orientation happened", dataType="Coded", options=["Yes", "No"])

    answers = _element(bundle, "Orientation happened")["concept"]["answers"]
    assert [a["uuid"] for a in answers] == [YES["uuid"], NO["uuid"]]


def test_a_field_named_like_an_organisation_concept_of_the_same_type_reuses_it(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    before = _concepts(bundle)
    result = _add(bundle, name="mobile number", dataType="Numeric")

    assert result.rejected == []
    assert result.concepts_appended == 0
    concept = _element(bundle, "mobile number")["concept"]
    assert (concept["name"], concept["uuid"], concept["dataType"]) == ("Mobile Number", MOBILE["uuid"], "Numeric")
    assert _concepts(bundle) == before


def test_a_field_named_like_an_organisation_concept_of_another_type_is_rejected(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    result = _add(bundle, name="Mobile Number", dataType="Text")

    assert [r.kind for r in result.rejected] == ["not_allowed"]
    assert "already has a Numeric concept named 'Mobile Number'" in result.rejected[0].reason
    assert result.rejected[0].hint == "add it as Numeric or choose another field name"
    assert [e["name"] for e in _form(bundle)["formElementGroups"][0]["formElements"]] == [ATTENDED["name"]]


@pytest.mark.parametrize(("options", "accepted"), [
    (["Yes", "No"], True),
    (["no", "yes"], True),
    (["Yes", "No", "Maybe"], False),
    (["Yes"], False),
])
def test_a_coded_field_reuses_an_organisation_concept_only_with_the_same_options(
    tmp_path: Path, options: list[str], accepted: bool,
) -> None:
    other_page = {**ATTENDED, "uuid": "c0c0c0c0-0000-4000-8000-0000000000a4", "name": "Attended again"}
    bundle = _org_bundle(tmp_path, concepts=[YES, NO, ATTENDED, other_page])
    result = _add(bundle, name="Attended again", dataType="Coded", options=options)

    if accepted:
        assert result.rejected == []
        concept = _element(bundle, "Attended again")["concept"]
        assert concept["uuid"] == other_page["uuid"]
        assert [a["uuid"] for a in concept["answers"]] == [NO["uuid"], YES["uuid"]]
        assert result.concepts_appended == 0
    else:
        assert [r.kind for r in result.rejected] == ["not_allowed"]
        assert "with the options No, Yes" in result.rejected[0].reason


def test_renaming_a_coded_field_keeps_the_organisations_answers(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename", "target": {**TARGET, "field": ATTENDED["name"]},
        "payload": {"new_name": "Attended the orientation"},
    }])

    assert result.rejected == []
    renamed = next(c for c in _concepts(bundle) if c["name"] == "Attended the orientation")
    assert [a["uuid"] for a in renamed["answers"]] == [NO["uuid"], YES["uuid"]]
    _no_name_under_two_uuids(bundle)


def test_a_rename_onto_an_organisation_concept_of_another_type_changes_nothing(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    form_before, concepts_before = _form(bundle), _concepts(bundle)
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename", "target": {**TARGET, "field": ATTENDED["name"]},
        "payload": {"new_name": "Mobile Number"},
    }])

    assert [r.kind for r in result.rejected] == ["not_allowed"]
    assert _form(bundle) == form_before
    assert _concepts(bundle) == concepts_before


def test_an_answer_spelt_exactly_wins_over_one_differing_only_in_case(tmp_path: Path) -> None:
    """JSCS has both "NO" and "No"; concepts.json lists them sorted ignoring case."""
    shouting_no = {"name": "NO", "uuid": "f0e2fdc8-0000-4000-8000-0000000000a5", "dataType": "NA", "answers": []}
    bundle = _org_bundle(tmp_path, concepts=[shouting_no, NO, YES])
    _add(bundle, name="Orientation happened", dataType="Coded", options=["Yes", "No"])

    answers = _element(bundle, "Orientation happened")["concept"]["answers"]
    assert [(a["name"], a["uuid"]) for a in answers] == [("Yes", YES["uuid"]), ("No", NO["uuid"])]


def _op(kind: str, **fields) -> dict:
    return {"op_id": f"op-{kind}", "kind": kind, **fields}


def test_a_removed_field_added_back_keeps_the_organisations_concept(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    _add(bundle, name="mobile number", dataType="Numeric")
    apply_field_edits(str(bundle), [_op("field.remove", target={**TARGET, "field": "mobile number"}, payload={})])
    before = _concepts(bundle)
    result = _add(bundle, name="mobile number", dataType="Numeric")

    assert result.rejected == []
    assert result.form_elements_reinstated == 1
    assert _element(bundle, "mobile number")["concept"]["uuid"] == MOBILE["uuid"]
    assert _concepts(bundle) == before
    _no_name_under_two_uuids(bundle)


def test_a_removed_field_added_back_as_another_type_is_rejected(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    _add(bundle, name="mobile number", dataType="Numeric")
    apply_field_edits(str(bundle), [_op("field.remove", target={**TARGET, "field": "mobile number"}, payload={})])
    result = _add(bundle, name="mobile number", dataType="Text")

    assert [r.kind for r in result.rejected] == ["not_allowed"]
    assert all(e["name"] != "mobile number" or e["voided"] for e in _form(bundle)["formElementGroups"][0]["formElements"])


def test_renaming_onto_a_removed_field_that_used_an_organisation_concept_appends_nothing(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    _add(bundle, name="mobile number", dataType="Numeric")
    apply_field_edits(str(bundle), [_op("field.remove", target={**TARGET, "field": "mobile number"}, payload={})])
    _add(bundle, name="Phone", dataType="Numeric")
    before = _concepts(bundle)
    result = apply_field_edits(str(bundle), [
        _op("field.rename", target={**TARGET, "field": "Phone"}, payload={"new_name": "mobile number"}),
    ])

    assert result.rejected == []
    assert _concepts(bundle) == before
    _no_name_under_two_uuids(bundle)


def test_a_form_cannot_ask_an_organisation_concept_twice(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    form = _form(bundle)
    form["formElementGroups"][0]["formElements"].append({
        "uuid": "e1e1e1e1-0000-4000-8000-0000000000b2", "name": "Mobile", "displayOrder": 2,
        "mandatory": False, "type": "SingleSelect", "keyValues": [], "voided": False,
        "concept": {k: MOBILE[k] for k in ("name", "uuid", "dataType", "answers")},
    })
    (bundle / "forms" / "women.json").write_text(json.dumps(form), encoding="utf-8")
    result = _add(bundle, name="Mobile Number", dataType="Numeric")

    assert [r.kind for r in result.rejected] == ["not_allowed"]
    assert "the field 'Mobile' of this form already asks" in result.rejected[0].reason


def test_a_live_concept_wins_over_a_voided_one_of_the_same_name(tmp_path: Path) -> None:
    voided_yes = {"name": "Yes", "uuid": "dead0000-0000-4000-8000-0000000000a6", "dataType": "NA",
                  "answers": [], "voided": True}
    bundle = _org_bundle(tmp_path, concepts=[voided_yes, YES, NO])
    _add(bundle, name="Orientation happened", dataType="Coded", options=["Yes", "No"])

    answers = _element(bundle, "Orientation happened")["concept"]["answers"]
    assert answers[0]["uuid"] == YES["uuid"]


def test_a_field_named_like_an_answer_option_is_rejected_without_suggesting_na(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    result = _add(bundle, name="Yes", dataType="Text")

    assert [r.kind for r in result.rejected] == ["not_allowed"]
    assert result.rejected[0].reason == "'Yes' is an answer option in this organisation, so a field cannot use that name"
    assert result.rejected[0].hint == "choose another field name"


def _with_voided_answer(bundle: Path) -> None:
    """The field once had a third option, Maybe, since removed."""
    form = _form(bundle)
    element = form["formElementGroups"][0]["formElements"][0]
    element["concept"]["answers"].append({"name": "Maybe", "uuid": "aaaa0000-0000-4000-8000-0000000000a7",
                                          "order": 2.0, "voided": True})
    (bundle / "forms" / "women.json").write_text(json.dumps(form), encoding="utf-8")


def test_a_rename_leaves_out_options_that_were_removed(tmp_path: Path) -> None:
    bundle = _org_bundle(tmp_path)
    _with_voided_answer(bundle)
    apply_field_edits(str(bundle), [
        _op("field.rename", target={**TARGET, "field": ATTENDED["name"]}, payload={"new_name": "Attended the orientation"}),
    ])
    renamed = next(c for c in _concepts(bundle) if c["name"] == "Attended the orientation")
    assert [a["name"] for a in renamed["answers"]] == ["No", "Yes"]


def test_a_rename_onto_an_organisation_concept_ignores_options_that_were_removed(tmp_path: Path) -> None:
    same_options = {**ATTENDED, "uuid": "c0c0c0c0-0000-4000-8000-0000000000a8", "name": "Attended"}
    bundle = _org_bundle(tmp_path, concepts=[YES, NO, ATTENDED, same_options])
    _with_voided_answer(bundle)
    result = apply_field_edits(str(bundle), [
        _op("field.rename", target={**TARGET, "field": ATTENDED["name"]}, payload={"new_name": "Attended"}),
    ])
    assert result.rejected == []
    assert _element(bundle, "Attended")["concept"]["uuid"] == same_options["uuid"]


def test_a_field_brought_back_on_an_organisation_concept_keeps_its_answers(tmp_path: Path) -> None:
    other = {**ATTENDED, "uuid": "c0c0c0c0-0000-4000-8000-0000000000a9", "name": "Attended again"}
    bundle = _org_bundle(tmp_path, concepts=[YES, NO, ATTENDED, other])
    _add(bundle, name="Attended again", dataType="Coded", options=["No", "Yes"])
    apply_field_edits(str(bundle), [_op("field.remove", target={**TARGET, "field": "Attended again"}, payload={})])
    result = _add(bundle, name="Attended again", dataType="Coded", options=["Yes", "No"])

    assert result.form_elements_reinstated == 1
    assert _element(bundle, "Attended again")["concept"]["answers"] == other["answers"]

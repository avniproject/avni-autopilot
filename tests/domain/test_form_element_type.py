"""Multi-select fields carry the form element `type` Avni reads.

avni-server copies `formElement.type` from the bundle and the field app
treats a missing type as single-select; a `multiSelect` key-value is never
read. Every element the generator or the field editor writes therefore
carries `type`, and the update-from-spreadsheets diff reads it back.

Usage:
    uv run --extra dev pytest tests/domain/test_form_element_type.py
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from domain.bundle_editor import apply_field_edits
from domain.diff import _add_payload_from_element
from domain.generators import form_element_type

EKAM_BUNDLE = Path(__file__).resolve().parents[2] / "requirements" / "Ekam.zip"


def _ekam_copy(tmp_path: Path) -> Path:
    bundle = tmp_path / "Ekam.zip"
    shutil.copy(EKAM_BUNDLE, bundle)
    return bundle


def _elements(bundle: Path) -> list[tuple[str, str, dict]]:
    """(form name, section name, element) for every live element."""
    out = []
    with zipfile.ZipFile(bundle) as archive:
        for name in archive.namelist():
            if not (name.startswith("forms/") and name.endswith(".json")):
                continue
            form = json.loads(archive.read(name))
            if form.get("voided"):
                continue
            for group in form.get("formElementGroups", []):
                for element in group.get("formElements", []):
                    if not element.get("voided"):
                        out.append((form["name"], group["name"], element))
    return out


def _element_named(bundle: Path, field_name: str) -> dict:
    return next(e for _f, _s, e in _elements(bundle) if e["name"] == field_name)


def test_only_multi_select_maps_to_the_multi_select_type() -> None:
    assert form_element_type("MultiSelect") == "MultiSelect"
    assert form_element_type("SingleSelect") == "SingleSelect"
    assert form_element_type(None) == "SingleSelect"


@pytest.mark.parametrize("spelling", ["MultiSelect", "multi", "Multi Select", "multiselect", "multi-select", "multi_select", "Multiple", "multi choice"])
def test_any_spelling_of_multi_select_gives_the_multi_select_type(spelling: str) -> None:
    assert form_element_type(spelling) == "MultiSelect"


@pytest.mark.parametrize("spelling", ["SingleSelect", "single", "Single Select", "", "not multi", "single, not multi"])
def test_other_spellings_give_the_single_select_type(spelling: str) -> None:
    assert form_element_type(spelling) == "SingleSelect"


def test_an_added_multi_select_field_carries_the_type(tmp_path: Path) -> None:
    bundle = _ekam_copy(tmp_path)
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.add",
        "target": {"form": "ANC Cancellation", "section": "Details"},
        "payload": {"name": "QA18 Reasons", "dataType": "Coded",
                    "options": ["Travel", "Illness"], "selectionType": "MultiSelect"},
    }])
    assert result.form_elements_added == 1
    element = _element_named(bundle, "QA18 Reasons")
    assert element["type"] == "MultiSelect"
    assert all(kv["key"] != "multiSelect" for kv in element["keyValues"])


def test_a_renamed_field_keeps_its_type(tmp_path: Path) -> None:
    bundle = _ekam_copy(tmp_path)
    form, section, element = next(
        (f, s, e) for f, s, e in _elements(bundle)
        if e.get("type") == "MultiSelect" and e["concept"]["dataType"] == "Coded"
    )
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename",
        "target": {"form": form, "section": section, "field": element["name"]},
        "payload": {"new_name": "QA18 Renamed"},
    }])
    assert result.form_elements_renamed == 1, result.rejected
    assert _element_named(bundle, "QA18 Renamed")["type"] == "MultiSelect"


def test_the_update_diff_reads_multi_select_from_the_type() -> None:
    element = {
        "name": "Danger signs", "mandatory": False, "type": "MultiSelect", "keyValues": [],
        "concept": {"dataType": "Coded", "answers": [{"name": "Bleeding"}, {"name": "Fever"}]},
    }
    payload = _add_payload_from_element(element)
    assert payload["selectionType"] == "MultiSelect"
    assert payload["options"] == ["Bleeding", "Fever"]
    assert "selectionType" not in _add_payload_from_element({**element, "type": "SingleSelect"})


def _coded(name: str, element_type: str | None, uuid: str = "e1") -> dict:
    element = {
        "uuid": uuid, "name": name, "displayOrder": 1, "mandatory": False, "keyValues": [],
        "concept": {"name": name, "dataType": "Coded", "answers": [{"name": "A"}, {"name": "B"}]},
    }
    if element_type:
        element["type"] = element_type
    return element


def _bundle_with(element: dict) -> dict:
    form = {"uuid": "f1", "name": "ANC", "formElementGroups": [
        {"uuid": "g1", "name": "Details", "formElements": [element]},
    ]}
    return {"forms": [{"file_name": "anc.json", "content": form}], "concepts": []}


def test_the_update_diff_never_switches_single_and_multi_select() -> None:
    """avni-server refuses a single/multi select switch on a field it already
    has, so the diff leaves the setting and names the field instead."""
    from domain.diff import diff

    def check(desired: str | None, current: str | None) -> tuple[list[dict], list[str]]:
        result = diff(_bundle_with(_coded("Signs", desired)), _bundle_with(_coded("Signs", current)))
        return result.ops, result.selection_type_differences

    assert check("MultiSelect", None) == (
        [], ["ANC › Signs (single select in the app, multi select in the spreadsheets)"],
    )
    assert check("SingleSelect", "MultiSelect") == (
        [], ["ANC › Signs (multi select in the app, single select in the spreadsheets)"],
    )
    assert check("SingleSelect", None) == ([], [])
    assert check("MultiSelect", "MultiSelect") == ([], [])


def test_types_without_a_selection_setting_are_not_named() -> None:
    from domain.diff import diff

    text = _coded("Remarks", "MultiSelect")
    text["concept"]["dataType"] = "Text"
    assert diff(_bundle_with(text), _bundle_with({**text, "type": None})).selection_type_differences == []


def test_reinstating_a_deleted_field_keeps_its_selection_setting() -> None:
    from domain.diff import diff

    desired = _bundle_with(_coded("Signs", "MultiSelect"))
    current = _bundle_with({**_coded("Signs", "SingleSelect"), "voided": True})
    result = diff(desired, current)
    [op] = result.ops
    assert op["kind"] == "field.add" and op["payload"]["selectionType"] == "SingleSelect"
    assert result.selection_type_differences == [
        "ANC › Signs (single select in the app, multi select in the spreadsheets)",
    ]


def _autopilot_bundle_dir(tmp_path: Path, element_type: str) -> Path:
    """A bundle whose field has the UUID Autopilot derives from its name, so
    re-adding it after a removal reinstates that same element."""
    from domain.generators import make_uuid

    element = _coded("Signs", element_type, uuid=make_uuid("formElement:ANC:Signs"))
    element["concept"]["uuid"] = make_uuid("concept:Signs")
    form = _bundle_with(element)["forms"][0]["content"]
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "anc.json").write_text(json.dumps(form), encoding="utf-8")
    (tmp_path / "concepts.json").write_text(json.dumps([element["concept"]]), encoding="utf-8")
    return tmp_path


def test_the_chat_cannot_switch_a_field_by_removing_and_re_adding_it(tmp_path: Path) -> None:
    bundle = _autopilot_bundle_dir(tmp_path, "SingleSelect")
    target = {"form": "ANC", "section": "Details"}
    removed = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.remove", "target": {**target, "field": "Signs"},
    }])
    assert removed.applied == ["op-1"], removed.rejected
    re_add = {"name": "Signs", "dataType": "Coded", "options": ["A", "B"]}

    switched = apply_field_edits(str(bundle), [{
        "op_id": "op-2", "kind": "field.add", "target": target,
        "payload": {**re_add, "selectionType": "MultiSelect"},
    }])
    assert [r.kind for r in switched.rejected] == ["not_allowed"]
    assert "single select" in switched.rejected[0].reason

    kept = apply_field_edits(str(bundle), [{
        "op_id": "op-3", "kind": "field.add", "target": target, "payload": re_add,
    }])
    assert kept.form_elements_reinstated == 1, kept.rejected
    form = json.loads((bundle / "forms" / "anc.json").read_text(encoding="utf-8"))
    [element] = form["formElementGroups"][0]["formElements"]
    assert (element["voided"], element["type"]) == (False, "SingleSelect")


def test_a_rejected_re_add_leaves_the_removed_field_removed(tmp_path: Path) -> None:
    """A rejected op is not undone, so the check runs before the element is
    touched: in one batch, remove + re-add with the other setting must end
    with the field removed, as the result says."""
    bundle = _autopilot_bundle_dir(tmp_path, "SingleSelect")
    target = {"form": "ANC", "section": "Details"}
    result = apply_field_edits(str(bundle), [
        {"op_id": "op-1", "kind": "field.remove", "target": {**target, "field": "Signs"}},
        {"op_id": "op-2", "kind": "field.add", "target": target,
         "payload": {"name": "Signs", "dataType": "Coded", "options": ["A", "B"],
                     "mandatory": True, "selectionType": "MultiSelect"}},
    ])
    assert result.applied == ["op-1"]
    assert [r.kind for r in result.rejected] == ["not_allowed"]
    form = json.loads((bundle / "forms" / "anc.json").read_text(encoding="utf-8"))
    [element] = form["formElementGroups"][0]["formElements"]
    assert (element["voided"], element["mandatory"], element["type"]) == (True, False, "SingleSelect")


def test_a_field_moved_to_a_new_section_is_not_reported_as_left_as_it_is() -> None:
    """The moved field gets a new element on its new page, so its old setting
    does not carry over and it is not named as left single/multi select."""
    from domain.diff import diff

    current = _bundle_with(_coded("Signs", "MultiSelect"))
    desired = _bundle_with(_coded("Signs", "SingleSelect"))
    groups = desired["forms"][0]["content"]["formElementGroups"]
    moved = groups[0]["formElements"].pop()
    groups.append({"uuid": "g-new", "name": "Symptoms", "formElements": [moved]})
    result = diff(desired, current)
    assert [op["kind"] for op in result.ops] == ["field.remove", "section.add", "field.add"]
    assert result.selection_type_differences == []


def test_the_selection_switch_is_not_an_edit() -> None:
    from models import EditOperation

    with pytest.raises(Exception):
        EditOperation(op_id="op-1", kind="field.set_selection_type")



def test_reinstating_without_a_setting_says_it_came_back_multi_select(tmp_path: Path) -> None:
    bundle = _autopilot_bundle_dir(tmp_path, "MultiSelect")
    target = {"form": "ANC", "section": "Details"}
    result = apply_field_edits(str(bundle), [
        {"op_id": "op-1", "kind": "field.remove", "target": {**target, "field": "Signs"}},
        {"op_id": "op-2", "kind": "field.add", "target": target,
         "payload": {"name": "Signs", "dataType": "Coded", "options": ["A", "B"]}},
    ])
    assert result.form_elements_reinstated == 1, result.rejected
    assert result.warnings == [
        "field 'Signs' came back as multi select, as it was before it was removed; "
        "Avni cannot switch an existing field between single and multi select."
    ]


def test_a_field_of_another_data_type_in_the_app_is_named_by_that_type() -> None:
    from domain.diff import diff

    current = _coded("Signs", None)
    current["concept"]["dataType"] = "Text"
    result = diff(_bundle_with(_coded("Signs", "MultiSelect")), _bundle_with(current))
    assert result.selection_type_differences == [
        "ANC › Signs (a Text field in the app, multi select in the spreadsheets)",
    ]

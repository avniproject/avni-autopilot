"""Fields and pages in an update and in chat edits (`domain.diff` + `domain.bundle_editor`).

An update does not add, remove or reorder pages, and does not move fields
between pages: a moved field would be a new element without the original's
rules and settings. Such fields stay on their page and are named.

Usage:
    uv run --extra dev pytest tests/domain/test_field_page_move.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from domain.bundle_editor import apply_field_edits, load_bundle_snapshot
from domain.diff import diff
from domain.generators import make_uuid

FORM = "ANC"
FORM_FILE = "anc.json"


def _element(name: str, order: int) -> dict:
    return {
        "uuid": make_uuid(f"formElement:{FORM}:{name}"), "name": name,
        "displayOrder": order, "mandatory": False, "type": "SingleSelect",
        "keyValues": [], "voided": False,
        "concept": {"name": name, "uuid": make_uuid(f"concept:{name}"),
                    "dataType": "Text", "answers": []},
    }


def _form(pages: dict[str, list[str]]) -> dict:
    """A form as the generator writes it: page name -> field names, in order."""
    return {
        "name": FORM, "uuid": "form-anc", "formType": "ProgramEncounter",
        "formElementGroups": [
            {"uuid": make_uuid(f"formGroup:{FORM}:{page}"), "name": page,
             "displayOrder": page_order,
             "formElements": [_element(n, o) for o, n in enumerate(names, start=1)]}
            for page_order, (page, names) in enumerate(pages.items(), start=1)
        ],
    }


def _snapshot(form: dict) -> dict:
    return {"forms": [{"file_name": FORM_FILE, "content": form}], "concepts": []}


def _write_bundle(tmp_path: Path, form: dict) -> Path:
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / FORM_FILE).write_text(json.dumps(form), encoding="utf-8")
    concepts = [e["concept"] for g in form["formElementGroups"] for e in g["formElements"]]
    (tmp_path / "concepts.json").write_text(json.dumps(concepts), encoding="utf-8")
    return tmp_path


def _bundle_form(bundle: Path) -> dict:
    return json.loads((bundle / "forms" / FORM_FILE).read_text(encoding="utf-8"))


def _live_pages(bundle: Path) -> dict[str, list[str]]:
    return {
        g["name"]: [e["name"] for e in sorted(g["formElements"], key=lambda e: e["displayOrder"])
                    if not e.get("voided")]
        for g in _bundle_form(bundle)["formElementGroups"]
    }


def _element_uuids(bundle: Path) -> Counter:
    return Counter(
        e["uuid"] for g in _bundle_form(bundle)["formElementGroups"] for e in g["formElements"]
    )


def _update(bundle: Path, desired: dict):
    ops = diff(_snapshot(desired), load_bundle_snapshot(str(bundle))).ops
    return ops, apply_field_edits(str(bundle), ops)


def _ops_against(bundle: Path, pages: dict[str, list[str]]) -> list[dict]:
    return diff(_snapshot(_form(pages)), load_bundle_snapshot(str(bundle))).ops


START = {"Details": ["Weight", "Height"], "History": ["Past illness"]}


@pytest.mark.parametrize("pages", [
    {"Details": ["Weight"], "History": ["Past illness", "Height"]},
    {"History": ["Past illness", "Height"], "Details": ["Weight"]},
])
def test_a_field_on_another_page_in_the_sheet_stays_on_its_page(tmp_path: Path, pages) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    bundle_diff = diff(_snapshot(_form(pages)), load_bundle_snapshot(str(bundle)))

    assert bundle_diff.ops == []
    assert bundle_diff.fields_on_another_page == [
        "ANC › Height (page 'Details' in the app, 'History' in the spreadsheets)"]


def test_a_field_on_another_page_stays_while_the_rest_of_the_update_goes_through(
    tmp_path: Path,
) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight", "Pulse"], "History": ["Height", "Past illness"]}

    ops, result = _update(bundle, _form(pages))

    assert result.rejected == []
    assert [(op["kind"], op["payload"].get("name")) for op in ops] == [("field.add", "Pulse")]
    assert _live_pages(bundle) == {"Details": ["Weight", "Height", "Pulse"], "History": ["Past illness"]}


def test_chat_add_of_a_field_live_on_another_page_is_rejected_alone(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [
        {"op_id": "op-1", "kind": "field.add", "target": {"form": FORM, "section": "History"},
         "payload": {"name": "height", "dataType": "Text"}},
        {"op_id": "op-2", "kind": "field.add", "target": {"form": FORM, "section": "History"},
         "payload": {"name": "Allergies", "dataType": "Text"}},
    ])

    assert [(r.op_id, r.kind) for r in result.rejected] == [("op-1", "duplicate_name")]
    assert "section 'Details'" in result.rejected[0].reason
    assert result.applied == ["op-2"]
    assert _live_pages(bundle)["History"] == ["Past illness", "Allergies"]


def test_chat_remove_then_add_on_another_page_is_refused(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [
        {"op_id": "op-1", "kind": "field.remove",
         "target": {"form": FORM, "section": "Details", "field": "Height"}, "payload": {}},
        {"op_id": "op-2", "kind": "field.add", "target": {"form": FORM, "section": "History"},
         "payload": {"name": "Height", "dataType": "Text"}},
    ])

    assert [(r.op_id, r.kind) for r in result.rejected] == [("op-2", "not_allowed")]
    assert "is on page 'Details'" in result.rejected[0].reason
    assert _live_pages(bundle) == {"Details": ["Weight"], "History": ["Past illness"]}
    assert max(_element_uuids(bundle).values()) == 1


def test_chat_rename_onto_a_field_live_on_another_page_is_rejected(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename",
        "target": {"form": FORM, "section": "History", "field": "Past illness"},
        "payload": {"new_name": "Weight"},
    }])

    assert [r.kind for r in result.rejected] == ["duplicate_name"]
    assert _live_pages(bundle) == {"Details": ["Weight", "Height"], "History": ["Past illness"]}


def test_chat_rename_onto_a_name_removed_from_another_page_is_refused(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.remove",
        "target": {"form": FORM, "section": "Details", "field": "Height"}, "payload": {},
    }])

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-2", "kind": "field.rename",
        "target": {"form": FORM, "section": "History", "field": "Past illness"},
        "payload": {"new_name": "Height"},
    }])

    assert [r.kind for r in result.rejected] == ["not_allowed"]
    assert _live_pages(bundle)["History"] == ["Past illness"]


# ── Pages only on one side ────────────────────────────────────────────────────


def _display_orders_unique(bundle: Path) -> bool:
    groups = _bundle_form(bundle)["formElementGroups"]
    page_orders = [g["displayOrder"] for g in groups]
    return len(set(page_orders)) == len(page_orders) and all(
        len({e["displayOrder"] for e in g["formElements"]}) == len(g["formElements"])
        for g in groups
    )


def _live_page_order(bundle: Path) -> list[str]:
    groups = _bundle_form(bundle)["formElementGroups"]
    return [g["name"] for g in sorted(groups, key=lambda g: g["displayOrder"]) if not g.get("voided")]


@pytest.mark.parametrize("pages", [
    {"Details": ["Weight", "Height"]},                                         # page dropped
    {"Details": ["Weight", "Height"], "History": ["Past illness"], "Body": ["Pulse"]},  # page added
    {"Details": ["Weight", "Height"], "Medical history": ["Past illness"]},   # page renamed
    {"History": ["Past illness"], "Details": ["Weight", "Height"]},           # pages reordered
])
def test_an_update_leaves_pages_as_they_are(tmp_path: Path, pages) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    ops, result = _update(bundle, _form(pages))

    assert ops == [] and result.rejected == []
    assert _live_page_order(bundle) == ["Details", "History"]
    assert _live_pages(bundle) == START


def test_a_field_moved_onto_a_new_page_stays_and_is_named(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight"], "Body": ["Height"], "History": ["Past illness"]}

    bundle_diff = diff(_snapshot(_form(pages)), load_bundle_snapshot(str(bundle)))

    assert bundle_diff.ops == []
    assert bundle_diff.fields_on_another_page == [
        "ANC › Height (page 'Details' in the app, 'Body' in the spreadsheets)"]


# ── Field order and display order ─────────────────────────────────────────────


def test_a_field_brought_back_lands_where_the_sheet_puts_it(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form({"Details": ["Weight", "Height", "Pulse"]}))
    _update(bundle, _form({"Details": ["Weight", "Pulse"]}))

    _update(bundle, _form({"Details": ["Weight", "Height", "Pulse"]}))

    assert _live_pages(bundle)["Details"] == ["Weight", "Height", "Pulse"]
    assert _ops_against(bundle, {"Details": ["Weight", "Height", "Pulse"]}) == []


def test_remove_then_add_in_its_place_leaves_no_shared_display_order(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    target = {"form": FORM, "section": "Details"}

    apply_field_edits(str(bundle), [
        {"op_id": "op-1", "kind": "field.remove", "target": {**target, "field": "Height"}}])
    result = apply_field_edits(str(bundle), [
        {"op_id": "op-2", "kind": "field.add", "target": target,
         "payload": {"name": "Pulse", "dataType": "Text"}},
        {"op_id": "op-3", "kind": "section.reorder_fields", "target": target,
         "payload": {"order": ["Pulse", "Weight"]}},
    ])

    assert result.rejected == []
    assert _live_pages(bundle)["Details"] == ["Pulse", "Weight"]
    assert _display_orders_unique(bundle)


def test_a_chat_rename_leaves_no_shared_display_order(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename",
        "target": {"form": FORM, "section": "Details", "field": "Weight"},
        "payload": {"new_name": "Body weight"}}])

    assert result.rejected == []
    assert _live_pages(bundle)["Details"] == ["Body weight", "Height"]
    assert _display_orders_unique(bundle)


def test_a_bundle_already_sharing_display_orders_is_repaired_when_written(tmp_path: Path) -> None:
    form = _form(START)
    details = form["formElementGroups"][0]["formElements"]
    details.append(dict(_element("Old", 2), uuid="old", voided=True))  # clashes with Height
    bundle = _write_bundle(tmp_path, form)

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.add", "target": {"form": FORM, "section": "History"},
        "payload": {"name": "Allergies", "dataType": "Text"}}])

    assert result.rejected == []
    assert _live_pages(bundle)["Details"] == ["Weight", "Height"]
    assert _display_orders_unique(bundle)


# ── Rows whose names differ only in capitals ──────────────────────────────────


def test_a_row_differing_only_in_capitals_is_left_out_alone(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight", "Height", "Pulse"], "History": ["Past illness", "weight"]}

    bundle_diff = diff(_snapshot(_form(pages)), load_bundle_snapshot(str(bundle)))
    result = apply_field_edits(str(bundle), bundle_diff.ops)

    assert result.rejected == []
    assert bundle_diff.name_clashes == [
        "ANC › 'weight' (page 'History') has the same name as 'Weight' (page 'Details') apart from capitals"]
    assert _live_pages(bundle) == {"Details": ["Weight", "Height", "Pulse"], "History": ["Past illness"]}


# ── Pairing and removal targets ───────────────────────────────────────────────


def test_a_same_named_field_of_another_concept_is_not_taken_for_the_desired_one(
    tmp_path: Path,
) -> None:
    """A field made in the Avni app has its own concept, so it is replaced, as before."""
    form = _form(START)
    app_made = form["formElementGroups"][0]["formElements"][1]
    app_made["uuid"] = "made-in-app"
    app_made["concept"] = {"name": "Height", "uuid": "app-concept", "dataType": "Text", "answers": []}
    bundle = _write_bundle(tmp_path, form)

    ops, result = _update(bundle, _form(START))

    assert [(op["kind"], op["target"].get("field_uuid")) for op in ops] == [
        ("field.remove", "made-in-app"), ("field.add", None)]
    assert result.rejected == []


def test_an_update_removes_one_of_two_same_named_fields_by_uuid(tmp_path: Path) -> None:
    form = _form(START)
    leftover = dict(_element("Weight", 3), uuid="leftover-weight",
                    concept={"name": "Weight", "uuid": "old-concept", "dataType": "Text", "answers": []})
    form["formElementGroups"][0]["formElements"].append(leftover)
    bundle = _write_bundle(tmp_path, form)

    _, result = _update(bundle, _form(START))

    assert result.rejected == []
    voided = [e["uuid"] for g in _bundle_form(bundle)["formElementGroups"]
              for e in g["formElements"] if e.get("voided")]
    assert voided == ["leftover-weight"]


def test_a_removed_multi_select_field_back_as_text_is_not_multi_select(tmp_path: Path) -> None:
    form = _form(START)
    signs = dict(_element("Signs", 3), type="MultiSelect", voided=True,
                 concept={"name": "Signs", "uuid": make_uuid("concept:Signs"),
                          "dataType": "Coded", "answers": [{"name": "A"}]})
    form["formElementGroups"][0]["formElements"].append(signs)
    bundle = _write_bundle(tmp_path, form)

    _, result = _update(bundle, _form({"Details": ["Weight", "Height", "Signs"],
                                       "History": ["Past illness"]}))

    assert result.form_elements_reinstated == 1
    back = next(e for e in _bundle_form(bundle)["formElementGroups"][0]["formElements"]
                if e["name"] == "Signs")
    assert back["concept"]["dataType"] == "Text"
    assert back["type"] == "SingleSelect"


# ── Chat edits ────────────────────────────────────────────────────────────────


def _two_weights(tmp_path: Path) -> Path:
    form = _form(START)
    form["formElementGroups"][0]["formElements"].append(dict(_element("weight", 3), uuid="second-weight"))
    return _write_bundle(tmp_path, form)


def test_chat_remove_by_field_uuid_picks_one_of_two_same_named_fields(tmp_path: Path) -> None:
    bundle = _two_weights(tmp_path)
    target = {"form": FORM, "section": "Details"}

    by_name = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.remove", "target": {**target, "field": "Weight"}}])
    assert [(r.kind, r.hint) for r in by_name.rejected] == [("ambiguous_target", "pass field_uuid")]

    by_uuid = apply_field_edits(str(bundle), [{
        "op_id": "op-2", "kind": "field.remove", "target": {**target, "field_uuid": "second-weight"}}])
    assert by_uuid.rejected == []
    assert _live_pages(bundle)["Details"] == ["Weight", "Height"]


def test_chat_rename_by_field_uuid(tmp_path: Path) -> None:
    bundle = _two_weights(tmp_path)

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename",
        "target": {"form": FORM, "section": "Details", "field_uuid": "second-weight"},
        "payload": {"new_name": "Weight at birth"}}])

    assert result.rejected == []
    assert _live_pages(bundle)["Details"] == ["Weight", "Height", "Weight at birth"]


def test_chat_edit_with_an_unknown_field_uuid_is_not_found(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.remove",
        "target": {"form": FORM, "section": "Details", "field_uuid": "nope"}}])

    assert [r.kind for r in result.rejected] == ["not_found"]


@pytest.mark.parametrize("op", [
    {"kind": "field.add", "target": {"form": FORM, "section": "Details"},
     "payload": {"name": "x" * 256, "dataType": "Text"}},
    {"kind": "field.add", "target": {"form": FORM, "section": "Details"},
     "payload": {"name": "Signs", "dataType": "Coded", "options": ["A", "y" * 256]}},
    {"kind": "field.rename", "target": {"form": FORM, "section": "Details", "field": "Weight"},
     "payload": {"new_name": "z" * 256}},
])
def test_chat_names_over_the_limit_are_rejected(tmp_path: Path, op: dict) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [{"op_id": "op-1", **op}])

    assert [r.kind for r in result.rejected] == ["schema"]
    assert "256 characters" in result.rejected[0].reason
    assert _live_pages(bundle) == START


def test_a_name_of_exactly_the_limit_is_accepted(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.add", "target": {"form": FORM, "section": "Details"},
        "payload": {"name": "x" * 255, "dataType": "Text"}}])

    assert result.rejected == []


def test_an_integrity_abort_reports_nothing_applied(tmp_path: Path) -> None:
    form = _form(START)
    details = form["formElementGroups"][0]["formElements"]
    details[1]["uuid"] = details[0]["uuid"]  # already broken
    bundle = _write_bundle(tmp_path, form)
    before = (bundle / "forms" / FORM_FILE).read_bytes()

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.remove",
        "target": {"form": FORM, "section": "History", "field": "Past illness"}}])

    assert result.applied == [] and result.form_elements_voided == 0
    assert [r.kind for r in result.rejected] == ["integrity"]
    assert (bundle / "forms" / FORM_FILE).read_bytes() == before


def test_the_integrity_check_counts_voided_questions() -> None:
    from domain.bundle_editor import _check_integrity

    form = _form(START)
    form["formElementGroups"][0]["formElements"].append(dict(_element("Old", 1), uuid="old", voided=True))
    errors = _check_integrity({FORM_FILE: form}, {FORM_FILE})
    assert errors == [f"{FORM_FILE} / section 'Details': duplicate displayOrder: [1]"]
    assert _check_integrity({FORM_FILE: form}, set()) == []


# ── Pages and fields as the Avni app may have changed them ────────────────────


def test_a_page_added_in_the_app_with_the_spreadsheet_name_is_left_as_it_is(tmp_path: Path) -> None:
    form = _form(START)
    form["formElementGroups"][1]["uuid"] = "made-in-app-designer"
    bundle = _write_bundle(tmp_path, form)

    bundle_diff = diff(_snapshot(_form(START)), load_bundle_snapshot(str(bundle)))

    assert bundle_diff.ops == [] and bundle_diff.fields_on_another_page == []


def test_a_page_and_field_renamed_in_the_app_are_reordered_by_their_app_names(
    tmp_path: Path,
) -> None:
    form = _form(START)
    form["formElementGroups"][0]["name"] = "Measurements"  # page renamed in the app
    form["formElementGroups"][0]["formElements"][0]["name"] = "Body weight"  # field renamed
    bundle = _write_bundle(tmp_path, form)
    pages = {"Details": ["Height", "Weight"], "History": ["Past illness"]}

    ops, result = _update(bundle, _form(pages))

    assert result.rejected == []
    assert _live_pages(bundle)["Measurements"] == ["Height", "Body weight"]


def test_of_two_rows_differing_in_capitals_the_one_the_app_has_is_kept(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form({"Details": ["weight", "Height"], "History": ["Past illness"]}))

    bundle_diff = diff(_snapshot(_form({"Details": ["Weight", "weight", "Height"],
                                        "History": ["Past illness"]})),
                       load_bundle_snapshot(str(bundle)))

    assert bundle_diff.name_clashes == [
        "ANC › 'Weight' (page 'Details') has the same name as 'weight' (page 'Details') apart from capitals"]
    assert apply_field_edits(str(bundle), bundle_diff.ops).rejected == []


def test_a_rename_bringing_back_a_removed_field_takes_the_renamed_fields_place(
    tmp_path: Path,
) -> None:
    form = _form({"Details": ["Weight", "Height", "Pulse"]})
    signs = dict(_element("Signs", 9), type="MultiSelect", voided=True,
                 concept={"name": "Signs", "uuid": make_uuid("concept:Signs"),
                          "dataType": "Coded", "answers": [{"name": "A"}]})
    form["formElementGroups"][0]["formElements"].append(signs)
    bundle = _write_bundle(tmp_path, form)

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename",
        "target": {"form": FORM, "section": "Details", "field": "Height"},
        "payload": {"new_name": "Signs"}}])

    assert result.rejected == []
    assert _live_pages(bundle)["Details"] == ["Weight", "Signs", "Pulse"]
    assert result.warnings == [
        "field 'Signs' came back as multi select, as it was before it was removed; "
        "Avni cannot switch an existing field between single and multi select."]


# ── Review fixes ──────────────────────────────────────────────────────────────


def test_a_field_with_another_uuid_but_the_same_concept_on_its_page_is_kept(tmp_path: Path) -> None:
    """E.g. moved by an earlier build with a page-tied UUID, or added in App Designer."""
    form = _form(START)
    form["formElementGroups"][1]["formElements"][0]["uuid"] = "page-tied"
    form["formElementGroups"][1]["formElements"][0]["rule"] = "skip logic"
    bundle = _write_bundle(tmp_path, form)

    ops, result = _update(bundle, _form(START))

    assert ops == [] and result.rejected == []


def test_pages_on_one_side_only_are_named(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    bundle_diff = diff(_snapshot(_form({"Details": ["Weight", "Height"], "Body": ["Pulse"]})),
                       load_bundle_snapshot(str(bundle)))

    assert bundle_diff.ops == []
    assert bundle_diff.pages_not_added == ["ANC › Body"]
    assert bundle_diff.pages_not_removed == ["ANC › History"]


def test_a_reorder_keeps_a_field_the_sheet_puts_on_another_page(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form({"Details": ["Weight", "Height", "Pulse"], "History": ["Past illness"]}))
    pages = {"Details": ["Pulse", "Weight"], "History": ["Past illness", "Height"]}

    ops, result = _update(bundle, _form(pages))

    assert result.rejected == []
    assert [op["kind"] for op in ops] == ["section.reorder_fields"]
    assert _live_pages(bundle)["Details"] == ["Pulse", "Weight", "Height"]


def test_fields_for_a_voided_page_are_not_added_to_it(tmp_path: Path) -> None:
    form = _form(START)
    form["formElementGroups"][1]["voided"] = True
    form["formElementGroups"][1]["formElements"][0]["voided"] = True
    bundle = _write_bundle(tmp_path, form)

    bundle_diff = diff(_snapshot(_form(START)), load_bundle_snapshot(str(bundle)))

    assert bundle_diff.ops == []
    assert bundle_diff.pages_not_added == ["ANC › History"]


def test_a_field_removed_from_another_page_is_named_not_refused(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    _update(bundle, _form({"Details": ["Weight"], "History": ["Past illness"]}))  # Height removed

    ops, result = _update(bundle, _form({"Details": ["Weight"], "History": ["Past illness", "Height"]}))

    assert ops == [] and result.rejected == []
    assert diff(_snapshot(_form({"Details": ["Weight"], "History": ["Past illness", "Height"]})),
                load_bundle_snapshot(str(bundle))).fields_on_another_page == [
        "ANC › Height (removed from page 'Details' in the app, on 'History' in the spreadsheets)"]


# ── Setting changes an update does not apply ──────────────────────────────────


def _coded(name: str, options: list[str], **extra) -> dict:
    element = _element(name, 1)
    element["concept"] = {"name": name, "uuid": make_uuid(f"concept:{name}"), "dataType": "Coded",
                          "answers": [{"name": o} for o in options]}
    element.update(extra)
    return element


@pytest.mark.parametrize("change, expected", [
    ({"concept": {"name": "Signs", "uuid": make_uuid("concept:Signs"), "dataType": "Coded",
                  "answers": [{"name": "A"}, {"name": "B"}, {"name": "C"}]}}, "options"),
    ({"mandatory": True}, "mandatory"),
    ({"concept": {"name": "Signs", "uuid": make_uuid("concept:Signs"), "dataType": "Numeric", "answers": []}},
     "data type"),
    ({"keyValues": [{"key": "max", "value": 10}, {"key": "unit", "value": "kg"}]}, "unit"),
    ({"keyValues": [{"key": "max", "value": 20}]}, "limits"),
])
def test_a_setting_change_an_update_does_not_apply_is_named(change: dict, expected: str) -> None:
    current = _coded("Signs", ["A", "B"], keyValues=[{"key": "max", "value": 10}])
    desired = {**_coded("Signs", ["A", "B"], keyValues=[{"key": "max", "value": 10}]), **change}
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    bundle_diff = diff(_snapshot(form(desired)), _snapshot(form(current)))

    assert bundle_diff.ops == []
    assert bundle_diff.unapplied_changes == [f"ANC › Signs ({expected})"]


def test_equal_settings_written_differently_are_not_a_change() -> None:
    current = _coded("Signs", ["a", "B", "Gone"], keyValues=[{"key": "max", "value": "10.0"}])
    current["concept"]["answers"][2]["voided"] = True
    desired = _coded("Signs", ["B", "A"], keyValues=[{"key": "max", "value": 10}])
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == []


def test_a_unit_and_limits_the_app_keeps_on_the_concept_match_the_sheet() -> None:
    """App Designer keeps them on the concept, not on the field."""
    current = _element("Age", 1)
    current["concept"] = {"name": "Age", "uuid": make_uuid("concept:Age"), "dataType": "Numeric",
                          "lowAbsolute": 18.0, "highAbsolute": 60.0, "unit": "years"}
    desired = _element("Age", 1)
    desired["concept"] = {"name": "Age", "uuid": make_uuid("concept:Age"), "dataType": "Numeric"}
    desired["keyValues"] = [{"key": "min", "value": 18}, {"key": "max", "value": 60}, {"key": "unit", "value": "years"}]
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == []
    current["concept"]["highAbsolute"] = 65.0
    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == ["ANC › Age (limits)"]


def test_settings_the_sheet_leaves_blank_keep_the_app_s_and_are_not_flagged() -> None:
    """A blank Mandatory, unit or range cell, or one side of a range, says nothing to change."""
    current = _element("Age", 1)
    current["mandatory"] = True
    current["concept"] = {"name": "Age", "uuid": make_uuid("concept:Age"), "dataType": "Numeric",
                          "lowAbsolute": 0.0, "highAbsolute": 120.0, "unit": "kg"}
    desired = _element("Age", 1)
    desired["mandatory"] = False
    desired["concept"] = {"name": "Age", "uuid": make_uuid("concept:Age"), "dataType": "Numeric"}
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == []
    desired["keyValues"] = [{"key": "max", "value": 120}]
    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == []
    desired["keyValues"] = [{"key": "max", "value": 100}]
    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == ["ANC › Age (limits)"]


@pytest.mark.parametrize("sheet_type, app_type", [("Text", "Notes"), ("Text", "Subject"), ("ImageV2", "Image")],
                         ids=["blank or unknown cell vs Notes", "blank or unknown cell vs Subject", "Image renamed"])
def test_a_data_type_the_sheet_does_not_give_is_not_flagged(sheet_type: str, app_type: str) -> None:
    """A blank or unknown Data Type cell is read as Text; Image and ImageV2 are the same type."""
    def element(data_type: str) -> dict:
        e = _element("Remarks", 1)
        e["concept"] = {"name": "Remarks", "uuid": make_uuid("concept:Remarks"), "dataType": data_type}
        return e
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    assert diff(_snapshot(form(element(sheet_type))), _snapshot(form(element(app_type)))).unapplied_changes == []


def test_a_kind_the_bundle_has_no_file_for_is_not_called_new() -> None:
    from domain.diff import entities_not_in_bundle

    assert entities_not_in_bundle({"program": ["Pregnancy"], "visit type": ["ANC"]}, {"program": set()}) == [
        "program 'Pregnancy'"]


def test_new_subject_types_programs_and_visit_types_are_named() -> None:
    from domain.diff import entities_not_in_bundle

    found = entities_not_in_bundle(
        {"subject type": ["Cohort", "Household"], "program": ["Pregnancy"], "visit type": ["session", "Home visit"]},
        {"subject type": {"cohort"}, "program": set(), "visit type": {"session"}},
    )
    assert found == ["subject type 'Household'", "program 'Pregnancy'", "visit type 'Home visit'"]


@pytest.mark.parametrize("sheet_options", [["A"], []], ids=["fewer options", "blank options cell"])
def test_options_the_app_has_but_the_sheet_lacks_are_not_flagged(sheet_options: list[str]) -> None:
    """The app's concept can hold more answers: shared with another form, excluded on this one."""
    current = _coded("Signs", ["A", "B", "Shared elsewhere"])
    desired = _coded("Signs", sheet_options)
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    assert diff(_snapshot(form(desired)), _snapshot(form(current))).unapplied_changes == []


def test_a_new_visit_type_listed_twice_is_named_once() -> None:
    from domain.diff import entities_not_in_bundle

    assert entities_not_in_bundle({"visit type": ["ANC Visit", "anc visit "]}, {"visit type": set()}) == [
        "visit type 'ANC Visit'"]


def test_a_bundle_without_an_entity_file_leaves_that_kind_out(tmp_path: Path) -> None:
    import json as _json
    import zipfile
    from domain.bundle_editor import load_bundle_contents

    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("programs.json", _json.dumps([{"name": "Pregnancy"}, {"name": "Old", "voided": True}]))
    names = load_bundle_contents(str(bundle)).entity_names
    assert names == {"program": {"pregnancy"}}


@pytest.mark.parametrize("content", ["{not json", '{"name": "x"}', "{}", '["Pregnancy"]'],
                         ids=["truncated", "object not list", "empty object", "list of strings"])
def test_an_unreadable_entity_file_leaves_that_kind_out(tmp_path: Path, content: str) -> None:
    import zipfile
    from domain.bundle_editor import load_bundle_contents

    bundle = tmp_path / "b.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("programs.json", content)
        z.writestr("subjectTypes.json", json.dumps([{"name": "Person"}, {"name": None}]))
    assert load_bundle_contents(str(bundle)).entity_names == {"subject type": {"person"}}


def test_a_nameless_entity_is_not_named() -> None:
    from domain.diff import entities_not_in_bundle

    assert entities_not_in_bundle({"subject type": [None, " ", "Household"]}, {"subject type": set()}) == [
        "subject type 'Household'"]


def test_a_unit_differing_only_in_capitals_is_not_a_change() -> None:
    def element(unit: str) -> dict:
        e = _element("Weight", 1)
        e["concept"] = {"name": "Weight", "uuid": make_uuid("concept:Weight"), "dataType": "Numeric", "unit": unit}
        return e
    form = lambda e: {"name": FORM, "uuid": "f", "formElementGroups": [  # noqa: E731
        {"uuid": "g", "name": "Details", "displayOrder": 1, "formElements": [e]}]}

    assert diff(_snapshot(form(element("kg "))), _snapshot(form(element("Kg")))).unapplied_changes == []
    assert diff(_snapshot(form(element("g"))), _snapshot(form(element("Kg")))).unapplied_changes == [
        "ANC › Weight (unit)"]

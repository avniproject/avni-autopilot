"""Moving a field to another page of a form (`domain.diff` + `domain.bundle_editor`).

A field's element UUID comes from its form and name, not its page. avni-server
looks an element up only within its own page and needs element UUIDs unique,
so a moved field is voided on its old page and gets a new element, with a UUID
tied to its new page, that keeps the same concept.

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


@pytest.mark.parametrize("pages_after_move", [
    {"Details": ["Weight"], "History": ["Past illness", "Height"]},
    # Target page listed first: the add would run before the remove without
    # removals ordered first.
    {"History": ["Past illness", "Height"], "Details": ["Weight"]},
])
def test_moving_a_field_to_another_page_is_applied(tmp_path: Path, pages_after_move) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    _, result = _update(bundle, _form(pages_after_move))

    assert result.rejected == []
    assert _live_pages(bundle) == {"Details": ["Weight"], "History": ["Past illness", "Height"]}
    assert max(_element_uuids(bundle).values()) == 1
    moved = next(e for g in _bundle_form(bundle)["formElementGroups"] if g["name"] == "History"
                 for e in g["formElements"] if e["name"] == "Height")
    assert moved["uuid"] != make_uuid(f"formElement:{FORM}:Height")
    assert moved["concept"]["uuid"] == make_uuid("concept:Height")


def test_updating_again_after_a_move_changes_nothing(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    moved = _form({"Details": ["Weight"], "History": ["Past illness", "Height"]})
    _update(bundle, moved)

    ops, _ = _update(bundle, moved)

    assert ops == []


def test_moving_a_field_back_reinstates_its_first_element(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    _update(bundle, _form({"Details": ["Weight"], "History": ["Past illness", "Height"]}))

    _, result = _update(bundle, _form(START))

    assert result.rejected == []
    assert result.form_elements_reinstated == 1
    assert _live_pages(bundle) == {"Details": ["Weight", "Height"], "History": ["Past illness"]}
    assert max(_element_uuids(bundle).values()) == 1
    assert _ops_against(bundle, START) == []


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


def test_chat_remove_then_add_on_another_page_moves_the_field(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    result = apply_field_edits(str(bundle), [
        {"op_id": "op-1", "kind": "field.remove",
         "target": {"form": FORM, "section": "Details", "field": "Height"}, "payload": {}},
        {"op_id": "op-2", "kind": "field.add", "target": {"form": FORM, "section": "History"},
         "payload": {"name": "Height", "dataType": "Text"}},
    ])

    assert result.rejected == []
    assert _live_pages(bundle) == {"Details": ["Weight"], "History": ["Past illness", "Height"]}
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


def test_chat_rename_onto_a_name_removed_from_another_page_is_applied(tmp_path: Path) -> None:
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

    assert result.rejected == []
    assert _live_pages(bundle)["History"] == ["Height"]
    assert max(_element_uuids(bundle).values()) == 1


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


def test_a_field_moved_onto_a_new_page_moves_with_the_page_added(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight"], "Body": ["Height"], "History": ["Past illness"]}

    ops, result = _update(bundle, _form(pages))

    assert result.rejected == [] and result.sections_added == 1
    assert _live_pages(bundle) == pages
    assert _live_page_order(bundle) == ["Details", "Body", "History"]
    assert _display_orders_unique(bundle)
    assert _ops_against(bundle, pages) == []


def test_a_page_dropped_from_the_spreadsheets_is_removed_with_its_fields(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))

    ops, result = _update(bundle, _form({"Details": ["Weight", "Height"]}))

    assert result.rejected == [] and result.sections_removed == 1
    assert [op["kind"] for op in ops] == ["field.remove", "section.remove"]
    assert _live_pages(bundle) == {"Details": ["Weight", "Height"], "History": []}
    assert _live_page_order(bundle) == ["Details"]
    assert _ops_against(bundle, {"Details": ["Weight", "Height"]}) == []


def test_a_field_moved_off_a_dropped_page_is_kept(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight", "Past illness", "Height"]}

    _, result = _update(bundle, _form(pages))

    assert result.rejected == []
    assert _live_pages(bundle)["Details"] == ["Weight", "Past illness", "Height"]
    assert _live_page_order(bundle) == ["Details"]
    assert _ops_against(bundle, pages) == []


def test_a_renamed_page_moves_its_fields_to_the_new_name(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight", "Height"], "Medical history": ["Past illness"]}

    _, result = _update(bundle, _form(pages))

    assert result.rejected == []
    assert result.sections_added == 1 and result.sections_removed == 1
    assert _live_page_order(bundle) == ["Details", "Medical history"]
    assert _live_pages(bundle)["Medical history"] == ["Past illness"]
    assert max(_element_uuids(bundle).values()) == 1
    assert _display_orders_unique(bundle)
    assert _ops_against(bundle, pages) == []


def test_a_page_brought_back_reuses_its_voided_page(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    _update(bundle, _form({"Details": ["Weight", "Height"]}))

    _, result = _update(bundle, _form(START))

    assert result.rejected == []
    assert len(_bundle_form(bundle)["formElementGroups"]) == 2
    assert _live_pages(bundle) == START
    assert _display_orders_unique(bundle)
    assert _ops_against(bundle, START) == []


def test_pages_reordered_in_the_spreadsheets_are_reordered(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"History": ["Past illness"], "Details": ["Weight", "Height"]}

    ops, result = _update(bundle, _form(pages))

    assert [op["kind"] for op in ops] == ["form.reorder_sections"]
    assert _live_page_order(bundle) == ["History", "Details"]
    assert _ops_against(bundle, pages) == []


# ── Field order and display order ─────────────────────────────────────────────


def test_a_moved_field_lands_where_the_sheet_puts_it(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path, _form(START))
    pages = {"Details": ["Weight"], "History": ["Height", "Past illness"]}

    _update(bundle, _form(pages))

    assert _live_pages(bundle) == pages
    assert _ops_against(bundle, pages) == []


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


def test_a_field_brought_back_at_the_end_of_its_page_lands_last(tmp_path: Path) -> None:
    """No reorder is sent when the sheet puts it last, so it must go to the end."""
    bundle = _write_bundle(tmp_path, _form({"Details": ["Weight", "Height", "Pulse"]}))
    _update(bundle, _form({"Details": ["Weight", "Pulse"]}))

    ops, _ = _update(bundle, _form({"Details": ["Weight", "Pulse", "Height"]}))

    assert [op["kind"] for op in ops] == ["field.add"]
    assert _live_pages(bundle)["Details"] == ["Weight", "Pulse", "Height"]


def test_the_integrity_check_counts_voided_questions_and_pages() -> None:
    from domain.bundle_editor import _check_integrity

    form = _form(START)
    form["formElementGroups"][0]["formElements"].append(dict(_element("Old", 1), uuid="old", voided=True))
    form["formElementGroups"].append({"uuid": "gone", "name": "Gone", "displayOrder": 2,
                                      "voided": True, "formElements": []})
    errors = _check_integrity({FORM_FILE: form}, {FORM_FILE})
    assert errors == [
        f"{FORM_FILE}: duplicate page displayOrder: [2]",
        f"{FORM_FILE} / section 'Details': duplicate displayOrder: [1]",
    ]
    assert _check_integrity({FORM_FILE: form}, set()) == []


# ── Pages and fields as the Avni app may have changed them ────────────────────


def test_a_page_added_in_the_app_with_the_spreadsheet_name_is_kept(tmp_path: Path) -> None:
    form = _form(START)
    form["formElementGroups"][1]["uuid"] = "made-in-app-designer"
    bundle = _write_bundle(tmp_path, form)

    ops, _ = _update(bundle, _form(START))

    assert ops == []


def test_a_page_and_field_renamed_in_the_app_are_reordered_by_their_app_names(
    tmp_path: Path,
) -> None:
    form = _form(START)
    form["formElementGroups"][1]["name"] = "Past"           # page renamed in the app
    form["formElementGroups"][0]["formElements"][0]["name"] = "Body weight"  # field renamed
    bundle = _write_bundle(tmp_path, form)
    pages = {"History": ["Past illness"], "Details": ["Height", "Weight"]}

    ops, result = _update(bundle, _form(pages))

    assert result.rejected == []
    assert _live_page_order(bundle) == ["Past", "Details"]
    assert _live_pages(bundle)["Details"] == ["Height", "Body weight"]


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


def test_adding_a_page_renamed_in_the_app_is_refused(tmp_path: Path) -> None:
    form = _form(START)
    form["formElementGroups"][1]["name"] = "Past"
    bundle = _write_bundle(tmp_path, form)

    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "section.add", "target": {"form": FORM},
        "payload": {"name": "History"}}])

    assert [r.kind for r in result.rejected] == ["duplicate_name"]
    assert _live_page_order(bundle) == ["Details", "Past"]

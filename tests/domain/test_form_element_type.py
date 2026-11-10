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


def test_the_update_diff_switches_single_and_multi_select_on_existing_fields() -> None:
    from domain.diff import diff

    def ops(desired: str | None, current: str | None) -> list[dict]:
        return diff(_bundle_with(_coded("Signs", desired)), _bundle_with(_coded("Signs", current)))

    assert ops("MultiSelect", None) == [{
        "op_id": "diff-1", "kind": "field.set_selection_type",
        "target": {"form": "ANC", "section": "Details", "field": "Signs"},
        "payload": {"selectionType": "MultiSelect"},
    }]
    assert ops("SingleSelect", "MultiSelect")[0]["payload"] == {"selectionType": "SingleSelect"}
    assert ops("SingleSelect", None) == []
    assert ops("MultiSelect", "MultiSelect") == []


def test_set_selection_type_fixes_a_field_and_drops_the_stale_key_value(tmp_path: Path) -> None:
    bundle = _ekam_copy(tmp_path)
    form, section, element = next(
        (f, s, e) for f, s, e in _elements(bundle)
        if e.get("type") == "SingleSelect" and e["concept"]["dataType"] == "Coded"
    )
    target = {"form": form, "section": section, "field": element["name"]}
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.set_selection_type", "target": target,
        "payload": {"selectionType": "MultiSelect"},
    }])
    assert result.form_elements_retyped == 1, result.rejected
    assert _element_named(bundle, element["name"])["type"] == "MultiSelect"

    before = bundle.read_bytes()
    again = apply_field_edits(str(bundle), [{
        "op_id": "op-2", "kind": "field.set_selection_type", "target": target,
        "payload": {"selectionType": "MultiSelect"},
    }])
    assert again.applied == ["op-2"] and again.forms_modified == []
    assert bundle.read_bytes() == before


"""Field names in chat edits match ignoring case (`domain.bundle_editor`).

Avni shows "importance" and "Importance" as the same question, and an update
cannot tell two such fields apart, so a chat edit never makes a second one.

Usage:
    uv run --extra dev pytest tests/domain/test_bundle_editor_field_names.py
"""

from __future__ import annotations

import json
from pathlib import Path

from domain.bundle_editor import apply_field_edits
from domain.generators import make_uuid

TARGET = {"form": "ANC", "section": "Details"}


def _bundle_dir(tmp_path: Path, *field_names: str) -> Path:
    elements = [
        {
            "uuid": make_uuid(f"formElement:ANC:{name}"), "name": name,
            "displayOrder": order, "mandatory": False, "type": "SingleSelect",
            "keyValues": [], "voided": False,
            "concept": {"name": name, "uuid": make_uuid(f"concept:{name}"),
                        "dataType": "Text", "answers": []},
        }
        for order, name in enumerate(field_names, start=1)
    ]
    form = {"name": "ANC", "uuid": "form-anc", "formType": "ProgramEncounter",
            "formElementGroups": [{"uuid": "group-details", "name": "Details",
                                   "displayOrder": 1, "formElements": elements}]}
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "anc.json").write_text(json.dumps(form), encoding="utf-8")
    (tmp_path / "concepts.json").write_text(
        json.dumps([e["concept"] for e in elements]), encoding="utf-8",
    )
    return tmp_path


def _live_names(bundle: Path) -> list[str]:
    form = json.loads((bundle / "forms" / "anc.json").read_text(encoding="utf-8"))
    return [e["name"] for e in form["formElementGroups"][0]["formElements"] if not e.get("voided")]


def test_adding_a_field_that_differs_only_by_case_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle_dir(tmp_path, "Importance")
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.add", "target": TARGET,
        "payload": {"name": "importance", "dataType": "Text"},
    }])
    assert [r.kind for r in result.rejected] == ["duplicate_name"]
    assert "'Importance' already exists" in result.rejected[0].reason
    assert _live_names(bundle) == ["Importance"]


def test_renaming_onto_a_field_that_differs_only_by_case_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle_dir(tmp_path, "Importance", "Priority")
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename", "target": {**TARGET, "field": "Priority"},
        "payload": {"new_name": "importance"},
    }])
    assert [r.kind for r in result.rejected] == ["duplicate_name"]
    assert _live_names(bundle) == ["Importance", "Priority"]


def test_a_field_can_be_renamed_to_its_own_name_in_another_case(tmp_path: Path) -> None:
    bundle = _bundle_dir(tmp_path, "importance")
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename", "target": {**TARGET, "field": "importance"},
        "payload": {"new_name": "Importance"},
    }])
    assert result.form_elements_renamed == 1, result.rejected
    assert _live_names(bundle) == ["Importance"]


def _rename_in_app(bundle: Path, old_name: str, new_name: str) -> None:
    """Rename as the Avni app does: same UUID, new name."""
    path = bundle / "forms" / "anc.json"
    form = json.loads(path.read_text(encoding="utf-8"))
    for element in form["formElementGroups"][0]["formElements"]:
        if element["name"] == old_name:
            element["name"] = new_name
    path.write_text(json.dumps(form), encoding="utf-8")


def test_adding_a_name_whose_id_a_renamed_field_holds_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle_dir(tmp_path, "Age")
    _rename_in_app(bundle, "Age", "Patient age")
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.add", "target": TARGET,
        "payload": {"name": "Age", "dataType": "Text"},
    }])
    assert [r.kind for r in result.rejected] == ["duplicate_name"]
    assert "'Patient age' already exists" in result.rejected[0].reason
    assert _live_names(bundle) == ["Patient age"]


def test_renaming_to_a_name_whose_id_a_renamed_field_holds_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle_dir(tmp_path, "Age", "Weight")
    _rename_in_app(bundle, "Age", "Patient age")
    result = apply_field_edits(str(bundle), [{
        "op_id": "op-1", "kind": "field.rename", "target": {**TARGET, "field": "Weight"},
        "payload": {"new_name": "Age"},
    }])
    assert [r.kind for r in result.rejected] == ["duplicate_name"]
    assert _live_names(bundle) == ["Patient age", "Weight"]

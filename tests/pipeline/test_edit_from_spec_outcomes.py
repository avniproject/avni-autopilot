"""End-to-end tests of an update from spreadsheets (`edit_bundle_from_spec`)
on real Durga India spreadsheets: the bundle card appears only when the bundle
changed, and spreadsheet forms the update cannot add are named.

LLM enrichment and rule generation are switched off and every network
connection fails, so no API is called.

Usage:
    uv run --extra dev pytest tests/pipeline/test_edit_from_spec_outcomes.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

import chat.tools as tools
from config import Settings
from domain.bundle_editor import BundleOrigin, classify_bundle
from domain.llm import LLMHelper
from domain.rules.generator import RuleGenerator
from web.chat_service import ChatService
from web.sessions import ChatSession

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ORG = "durga_india"
ORG_SHEETS = PROJECT_ROOT / "resources" / "input" / ORG
REMOVED_FORM = "Cohort Registration"


@pytest.fixture
def offline_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Org input folder holding the org's sheets (upper-case names), no LLM, no network."""
    def no_network(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(LLMHelper, "is_available", lambda _self: False)
    monkeypatch.setattr(RuleGenerator, "is_available", lambda _self: False)
    org_dir = tmp_path / "input" / ORG
    org_dir.mkdir(parents=True)
    for sheet in ORG_SHEETS.glob("*.xlsx"):
        shutil.copy(sheet, org_dir / sheet.name.upper())
    monkeypatch.setattr(
        tools, "settings",
        Settings(input_root=str(tmp_path / "input"), output_root=str(tmp_path / "output")),
    )
    return org_dir


def _build() -> Path:
    result = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))
    assert result["status"] == "done", result.get("errors")
    return Path(result["zip_path"])


def _update(bundle: Path) -> dict:
    session = SimpleNamespace(bundle_path=bundle, org_setup_loading=False)
    return asyncio.run(
        tools.edit_bundle_from_spec.ainvoke({"org": ORG}, config={"configurable": {"session": session}})
    )


def _without_form(bundle: Path, form_name: str, dest: Path) -> Path:
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(dest, "w") as out:
        for item in src.infolist():
            data = src.read(item)
            if item.filename.startswith("forms/") and json.loads(data).get("name") == form_name:
                continue
            out.writestr(item, data)
    return dest


def _bundle_ready_events(result: dict, tmp_path: Path, bundle: Path | None = None) -> list:
    session = ChatSession(session_id="s", org_name="Durga India", username="u", workdir=tmp_path)
    session.bundle_path = bundle
    service = ChatService(session, agent_factory=lambda: None)
    service._handle_tool_message(ToolMessage(content=json.dumps(result), tool_call_id="c"))
    return [e for e in session.bus.replay_since(0) if e.type == "bundle.ready"]


def test_build_from_upper_case_sheets_is_an_autopilot_bundle(offline_pipeline: Path) -> None:
    assert classify_bundle(str(_build())) is BundleOrigin.AUTOPILOT


def test_update_with_no_changes_shows_no_card(offline_pipeline: Path, tmp_path: Path) -> None:
    bundle = _build()
    before = bundle.read_bytes()
    result = _update(bundle)
    assert result["status"] == "done"
    assert result["no_changes"] is True
    assert "bundle_updated" not in result
    assert "main_forms" not in result
    assert bundle.read_bytes() == before
    assert _bundle_ready_events(result, tmp_path) == []


def test_update_names_forms_it_cannot_add(offline_pipeline: Path, tmp_path: Path) -> None:
    bundle = _without_form(_build(), REMOVED_FORM, tmp_path / "partial.zip")
    result = _update(bundle)
    assert result["status"] == "error"
    assert result["unmatched_forms"] == [REMOVED_FORM]
    assert REMOVED_FORM in result["error"]
    assert "bundle_updated" not in result
    assert _bundle_ready_events(result, tmp_path) == []


def test_update_that_changes_the_bundle_shows_the_card(offline_pipeline: Path, tmp_path: Path) -> None:
    bundle = _build()
    session = SimpleNamespace(bundle_path=bundle, org_setup_loading=False)
    fields = tools.list_bundle_fields.invoke({}, config={"configurable": {"session": session}})
    form = next(f for f in fields["result"]["forms"] if f["form"] == REMOVED_FORM)
    section = form["sections"][0]
    removal = tools.edit_bundle_fields.invoke(
        {"operations": [{
            "op_id": "op-1", "kind": "field.remove",
            "target": {"form": REMOVED_FORM, "section": section["name"], "field": section["fields"][0]["name"]},
            "payload": {},
        }]},
        config={"configurable": {"session": session}},
    )
    assert removal["bundle_updated"] is True

    result = _update(bundle)
    assert result["status"] == "done"
    assert result["bundle_updated"] is True
    assert str(bundle) not in json.dumps(result)
    assert result["edit_result"]["form_elements_reinstated"] + result["edit_result"]["form_elements_added"] >= 1
    assert "main_forms" not in result
    events = _bundle_ready_events(result, tmp_path, bundle)
    assert len(events) == 1
    assert "main_forms" not in events[0].data["summary"]


def test_unmatched_forms_are_named_once() -> None:
    from domain.diff import unmatched_desired_forms

    desired = {"forms": [
        {"file_name": "a1.json", "content": {"uuid": "u1", "name": "ANC Cancellation"}},
        {"file_name": "a2.json", "content": {"uuid": "u1", "name": "ANC Cancellation"}},
        {"file_name": "b.json", "content": {"uuid": "u2", "name": "Birth"}},
    ]}
    current = {"forms": [{"file_name": "b.json", "content": {"uuid": "u2", "name": "Birth"}}]}
    assert unmatched_desired_forms(desired, current) == ["ANC Cancellation"]


def test_update_whose_edits_are_all_rejected_shows_no_card(offline_pipeline: Path) -> None:
    from pipeline.nodes import apply_diff_edits

    bundle = _build()
    before = bundle.read_bytes()
    state = {
        "org_name": ORG,
        "bundle_path": str(bundle),
        "errors": [],
        "diff_ops": [{
            "op_id": "diff-1", "kind": "field.add",
            "target": {"form": "No Such Form", "section": "Details"},
            "payload": {"name": "X", "dataType": "Text"},
        }],
    }
    update = apply_diff_edits(state)
    assert update["zip_path"] == ""
    assert update["edit_result"]["rejected"]
    assert bundle.read_bytes() == before


def _as_built_by_the_old_generator(bundle: Path, dest: Path) -> Path:
    """Elements without `type`, multi-select recorded only as a `multiSelect` key-value."""
    with zipfile.ZipFile(bundle) as src, zipfile.ZipFile(dest, "w") as out:
        for item in src.infolist():
            data = src.read(item)
            if item.filename.startswith("forms/"):
                form = json.loads(data)
                for group in form.get("formElementGroups", []):
                    for element in group.get("formElements", []):
                        if element.pop("type", None) == "MultiSelect":
                            element["keyValues"].append({"key": "multiSelect", "value": True})
                data = json.dumps(form)
            out.writestr(item, data)
    return dest


def _multi_select_elements(bundle: Path) -> set[str]:
    with zipfile.ZipFile(bundle) as archive:
        return {
            element["uuid"]
            for name in archive.namelist() if name.startswith("forms/")
            for group in json.loads(archive.read(name)).get("formElementGroups", [])
            for element in group["formElements"]
            if element.get("type") == "MultiSelect" and not element.get("voided")
        }


def test_update_fixes_multi_select_fields_of_a_bundle_built_by_the_old_generator(
    offline_pipeline: Path, tmp_path: Path,
) -> None:
    built = _build()
    expected = _multi_select_elements(built)
    assert expected
    old = _as_built_by_the_old_generator(built, tmp_path / "old.zip")
    assert _multi_select_elements(old) == set()

    result = _update(old)
    assert result["status"] == "done", result.get("error")
    assert result["bundle_updated"] is True
    assert result["edit_result"]["form_elements_retyped"] == len(expected)
    assert _multi_select_elements(old) == expected

    assert _update(old)["no_changes"] is True

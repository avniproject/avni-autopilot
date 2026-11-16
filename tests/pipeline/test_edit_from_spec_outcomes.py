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
import socket
import zipfile
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from langchain_core.messages import ToolMessage

import chat.tools as tools
from config import Settings
from domain.bundle_editor import BundleOrigin, classify_bundle
from domain.generators import NAME_LIMIT
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
        _copy_with_short_names(sheet, org_dir / sheet.name.upper())
    monkeypatch.setattr(
        tools, "settings",
        Settings(input_root=str(tmp_path / "input"), output_root=str(tmp_path / "output")),
    )
    return org_dir


def _copy_with_short_names(sheet: Path, dest: Path) -> None:
    """Copy a workbook with every text over Avni's name limit cut to fit.

    Without the AI review nothing offers shorter names, and a build with a
    name over the limit is an error.
    """
    workbook = openpyxl.load_workbook(sheet)
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and len(cell.value) > NAME_LIMIT:
                    cell.value = cell.value[:NAME_LIMIT - 5]
    workbook.save(dest)


def _build() -> Path:
    result = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))
    assert result["status"] == "done", result.get("errors")
    return Path(result["zip_path"])


def _web_session(bundle: Path) -> SimpleNamespace:
    return SimpleNamespace(
        input_dir=Path(tools.settings.input_root) / ORG,
        output_dir=Path(tools.settings.output_root) / ORG,
        bundle_path=bundle,
        org_setup_loading=False,
        fields_added_in_chat=[],
    )


def _update(bundle: Path) -> dict:
    session = _web_session(bundle)
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
    session = _web_session(bundle)
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


def test_update_names_a_field_added_in_chat_that_it_removes(offline_pipeline: Path) -> None:
    """The spreadsheets are the spec, so an update removes a field added in
    the chat; the user is told which, whatever the model says."""
    bundle = _build()
    session = _web_session(bundle)
    config = {"configurable": {"session": session}}
    form = next(
        f for f in tools.list_bundle_fields.invoke({}, config=config)["result"]["forms"]
        if f["form"] == REMOVED_FORM
    )
    added = tools.edit_bundle_fields.invoke({"operations": [{
        "op_id": "op-1", "kind": "field.add",
        "target": {"form": REMOVED_FORM, "section": form["sections"][0]["name"]},
        "payload": {"name": "QA17 Chat Field", "dataType": "Text"},
    }]}, config=config)
    assert added["bundle_updated"] is True
    assert session.fields_added_in_chat == [(REMOVED_FORM, "QA17 Chat Field")]
    before = bundle.read_bytes()

    held = asyncio.run(tools.edit_bundle_from_spec.ainvoke({"org": ORG}, config=config))

    assert held["status"] == "error"
    assert f"{REMOVED_FORM} › QA17 Chat Field" in held["error"]
    assert "confirmed_chat_fields_may_be_removed=true" in held["error"]
    assert bundle.read_bytes() == before

    result = asyncio.run(tools.edit_bundle_from_spec.ainvoke(
        {"org": ORG, "confirmed_chat_fields_may_be_removed": True}, config=config,
    ))

    assert result["status"] == "done"
    assert session.fields_added_in_chat == []
    assert result["removed_fields"] == [f"{REMOVED_FORM} › QA17 Chat Field"]
    [notice] = [n for n in session.pending_notices if "QA17 Chat Field" in n]
    assert "update removed them" in notice and "add it to the spreadsheet" in notice
    assert notice in result["already_shown_to_user"]


def _chat_edit(config: dict, op: dict) -> None:
    result = tools.edit_bundle_fields.invoke({"operations": [op]}, config=config)
    assert result["status"] == "done" and not result["result"]["rejected"], result


def _section_of(config: dict, form_name: str) -> str:
    forms = tools.list_bundle_fields.invoke({"form_name": form_name}, config=config)
    return forms["result"]["forms"][0]["sections"][0]["name"]


def test_an_update_does_not_ask_about_a_field_the_chat_added_then_removed(
    offline_pipeline: Path,
) -> None:
    session = _web_session(_build())
    config = {"configurable": {"session": session}}
    section = _section_of(config, REMOVED_FORM)
    _chat_edit(config, {"op_id": "op-1", "kind": "field.add",
                        "target": {"form": REMOVED_FORM, "section": section},
                        "payload": {"name": "QA17 Chat Field", "dataType": "Text"}})
    _chat_edit(config, {"op_id": "op-2", "kind": "field.remove",
                        "target": {"form": REMOVED_FORM, "section": section,
                                   "field": "qa17 chat field "}, "payload": {}})
    assert session.fields_added_in_chat == []

    result = asyncio.run(tools.edit_bundle_from_spec.ainvoke({"org": ORG}, config=config))

    assert result["status"] == "done", result.get("error")


def test_an_update_names_a_chat_field_by_its_name_after_a_rename(offline_pipeline: Path) -> None:
    session = _web_session(_build())
    config = {"configurable": {"session": session}}
    section = _section_of(config, REMOVED_FORM)
    _chat_edit(config, {"op_id": "op-1", "kind": "field.add",
                        "target": {"form": REMOVED_FORM, "section": section},
                        "payload": {"name": "QA17 Chat Field", "dataType": "Text"}})
    _chat_edit(config, {"op_id": "op-2", "kind": "field.rename",
                        "target": {"form": REMOVED_FORM, "section": section,
                                   "field": "QA17 Chat Field"},
                        "payload": {"new_name": "QA17 Renamed Field"}})

    held = asyncio.run(tools.edit_bundle_from_spec.ainvoke({"org": ORG}, config=config))

    assert held["status"] == "error"
    assert f"{REMOVED_FORM} › QA17 Renamed Field" in held["error"]
    assert "QA17 Chat Field" not in held["error"]


def test_each_build_in_a_chat_writes_its_own_bundle_file(offline_pipeline: Path) -> None:
    """A later build (withheld or not) never overwrites the bundle the chat has."""
    session = _web_session(None)
    session.build_needs_confirmation = False
    session.pending_notices = []
    config = {"configurable": {"session": session}}

    asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}, config=config))
    first = session.bundle_path
    first_bytes = first.read_bytes()
    asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}, config=config))

    assert session.bundle_path != first
    assert session.bundle_path.parent.parent == first.parent.parent == session.output_dir
    assert first.read_bytes() == first_bytes


def test_an_update_that_removes_nothing_names_no_field(offline_pipeline: Path) -> None:
    bundle = _build()
    session = _web_session(bundle)
    result = asyncio.run(tools.edit_bundle_from_spec.ainvoke(
        {"org": ORG}, config={"configurable": {"session": session}},
    ))
    assert "removed_fields" not in result
    assert not any("update removed them" in n for n in session.pending_notices)


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


def test_update_leaves_old_multi_select_fields_and_names_them(
    offline_pipeline: Path, tmp_path: Path,
) -> None:
    """avni-server refuses a single/multi select switch on fields it already
    has, so an update of an old-generator bundle must not switch them."""
    built = _build()
    expected = _multi_select_elements(built)
    assert expected
    old = _as_built_by_the_old_generator(built, tmp_path / "old.zip")
    before = old.read_bytes()
    session = _web_session(old)
    session.pending_notices = []

    result = asyncio.run(tools.edit_bundle_from_spec.ainvoke(
        {"org": ORG}, config={"configurable": {"session": session}},
    ))

    assert result["status"] == "done", result.get("error")
    assert result["no_changes"] is True
    assert "bundle_updated" not in result
    assert old.read_bytes() == before
    assert _multi_select_elements(old) == set()
    differences = result["selection_type_differences"]
    assert len(differences) == len(expected)
    assert all("(single select in the app, multi select in the spreadsheets)" in d for d in differences)
    [notice] = [n for n in session.pending_notices if "single and multi select" in n]
    assert differences[0] in notice


def _restore_long_names(org_dir: Path) -> None:
    for sheet in ORG_SHEETS.glob("*.xlsx"):
        (org_dir / sheet.name.upper()).write_bytes(sheet.read_bytes())


def test_a_build_with_a_name_over_the_limit_is_not_offered(offline_pipeline: Path) -> None:
    """Without the AI review nothing offers a shorter name, and the server
    refuses a concept name over the limit, so no bundle is offered and the
    error says which names."""
    _restore_long_names(offline_pipeline)
    result = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))
    assert result["status"] == "error"
    assert not result.get("zip_path")
    assert "main_forms" not in result
    assert result["error"].startswith("The app was not built. Avni cannot import")
    assert "Baseline for Men › Household Roles: field 'In many families" in result["error"]
    assert "(306 characters)" in result["error"]
    assert not list((Path(tools.settings.output_root) / ORG).glob("**/*.zip"))


def test_an_update_with_a_name_over_the_limit_changes_nothing(offline_pipeline: Path) -> None:
    bundle = _build()
    before = bundle.read_bytes()
    _restore_long_names(offline_pipeline)
    result = _update(bundle)
    assert result["status"] == "error"
    assert result["error"].startswith("Nothing in the bundle was changed. Avni cannot import")
    assert bundle.read_bytes() == before


def test_an_update_change_the_editor_refused_is_worded_for_the_user() -> None:
    from models import RejectedOp
    from pipeline.nodes import _rejected_update_message

    message = _rejected_update_message([RejectedOp(
        op_id="op-1", kind="ambiguous_target",
        reason="field 'Importance' matches 2 live elements in section 'Details'",
        hint="pass field_uuid",
    )], changed=True)
    assert message == (
        "The update could not make 1 change(s); the other changes were made, so the "
        "app does not fully match the spreadsheets: field 'Importance' matches 2 live "
        "elements in section 'Details'."
    )
    assert "edit_from_spec" not in message and "field_uuid" not in message


def test_an_update_aborted_by_the_integrity_check_says_nothing_was_applied() -> None:
    from models import RejectedOp
    from pipeline.nodes import _rejected_update_message

    message = _rejected_update_message([
        RejectedOp(op_id="*", kind="integrity", reason="anc.json: duplicate formElement uuid(s): ['u1']"),
    ], changed=False)
    assert message.startswith("The update was not applied")
    assert "other changes were made" not in message


def test_an_integrity_abort_still_names_the_changes_refused_before_it() -> None:
    from models import RejectedOp
    from pipeline.nodes import _rejected_update_message

    message = _rejected_update_message([
        RejectedOp(op_id="op-1", kind="not_allowed", reason="field 'Signs' is multi select"),
        RejectedOp(op_id="*", kind="integrity", reason="anc.json: duplicate formElement uuid(s): ['u1']"),
    ], changed=False)
    assert message.startswith("The update was not applied")
    assert "These changes could not be made either: field 'Signs' is multi select." in message


def test_an_update_whose_every_change_was_refused_never_says_others_were_made() -> None:
    from models import RejectedOp
    from pipeline.nodes import _rejected_update_message

    message = _rejected_update_message([
        RejectedOp(op_id="op-1", kind="duplicate_name", reason="field 'A' already exists"),
        RejectedOp(op_id="op-2", kind="not_allowed", reason="field 'B' is multi select"),
    ], changed=False)
    assert message == (
        "The update could not make any of its 2 change(s): "
        "field 'A' already exists; field 'B' is multi select."
    )


def test_only_names_sent_to_avni_are_checked() -> None:
    """A Text field's options never become answers, an update checks only the
    names in its `field.add` ops, and a shared answer is named once."""
    from domain.enricher import added_names_over_limit, names_over_limit
    from models import FieldSpec, FormSpec, SectionSpec

    long_name = "x" * 300
    form = FormSpec(name="New Survey", formType="Encounter", sections=[
        SectionSpec(name="Page", fields=[FieldSpec(name="Notes", dataType="Text", options=[long_name])]),
    ])
    assert names_over_limit([form]) == []
    ops = [
        {"kind": "field.add", "target": {"form": "ANC", "section": "Details"},
         "payload": {"name": "Signs", "dataType": "Coded", "options": [long_name, "B"]}},
        {"kind": "field.add", "target": {"form": "ANC", "section": "More"},
         "payload": {"name": "Other signs", "dataType": "Coded", "options": [long_name]}},
    ]
    [entry] = added_names_over_limit(ops)
    assert entry.startswith("ANC › Details › 'Signs': answer 'xxx")


def test_the_long_name_error_names_the_first_ten() -> None:
    from domain.enricher import name_limit_error

    error = name_limit_error([f"Form › Page: field 'F{n}' (300 characters)" for n in range(25)])
    assert "(25):" in error and "'F9'" in error and "'F10'" not in error
    assert error.endswith("and 15 more.")


def _update_summary(**state) -> dict:
    from chat.tools import _summarize

    return _summarize({"mode": "edit_from_spec", "org_name": ORG, "zip_path": "",
                       "errors": [], "forms_json": [{}], **state})


def test_an_update_names_fields_left_on_their_page() -> None:
    entry = "Session › Session Aim (page 'Session Details' in the app, 'Observation' in the spreadsheets)"
    summary = _update_summary(fields_on_another_page=[entry])

    assert summary["fields_on_another_page"] == [entry]
    assert any(entry in n and "does not move fields between pages" in n for n in summary["notices"])


@pytest.mark.parametrize("mode", ["edit_from_spec", "generate"])
def test_unread_selection_type_cells_are_a_notice(mode: str) -> None:
    from chat.tools import _summarize

    cell = "Session › Importance: 'Multi Select (Checkbox)'"
    summary = _summarize({"mode": mode, "org_name": ORG, "zip_path": "", "errors": [],
                          "forms_json": [{}], "unread_selection_types": [cell]})

    assert summary["unread_selection_types"] == [cell]
    assert any(cell in n and "read as single select" in n for n in summary["notices"])


def test_an_update_that_changes_nothing_still_says_it_matches() -> None:
    summary = _update_summary()

    assert summary["message"] == "The bundle already matches the spreadsheets; nothing was changed."


def test_rows_left_out_for_capitals_are_named_and_never_called_a_match() -> None:
    clash = "ANC › 'weight' (page 'History') has the same name as 'Weight' (page 'Details') apart from capitals"
    summary = _update_summary(name_clashes=[clash], selection_type_differences=["ANC › Signs (x)"])

    assert summary["name_clashes"] == [clash]
    assert any(clash in n and "Rename one row of each pair" in n for n in summary["notices"])
    assert "match" not in summary["message"]
    assert "differ from another only in capitals were left out" in summary["message"]
    assert "cannot switch a field it already has" in summary["message"]


def test_a_multi_select_checkbox_cell_reaches_the_user_as_a_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Himesh's testing note: "Multi Select (Checkbox)" as a Selection Type is named in the chat."""
    import pandas as pd

    import pipeline.nodes as nodes
    from chat.tools import _summarize
    from domain.parser import parse_form_df
    from models import EntitySpec, SubjectTypeSpec

    form = parse_form_df(pd.DataFrame([
        ["Field Name", "Data Type", "Options", "Selection Type"],
        ["Importance", "Coded", "High\nLow", "Multi Select (Checkbox)"],
    ]), "Session", None)
    spec = EntitySpec(subject_types=[SubjectTypeSpec(name="Cohort")], forms=[form])
    monkeypatch.setattr(nodes, "parse_scoping_docs", lambda _paths: (spec, []))

    parsed = nodes.parse_documents({"org_name": ORG, "file_paths": [], "errors": []})
    summary = _summarize({"mode": "generate", "org_name": ORG, "zip_path": "", "errors": [], **parsed})

    assert parsed["unread_selection_types"] == ["Session › Importance: 'Multi Select (Checkbox)'"]
    assert any("'Multi Select (Checkbox)'" in n for n in summary["notices"])


def test_an_update_names_pages_it_left_as_they_are_and_never_calls_it_a_match() -> None:
    summary = _update_summary(pages_not_added=["ANC › Body"], pages_not_removed=["ANC › History"])

    assert any("does not add pages, so they and their fields were left out: ANC › Body" in n
               for n in summary["notices"])
    assert any("does not remove pages, so they and their fields were left as they are: ANC › History" in n
               for n in summary["notices"])
    assert "match" not in summary["message"]

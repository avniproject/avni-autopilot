"""A build or update whose spreadsheets hold no subject type ends with an
error and no bundle card; every sheet nothing was read from is named with
its reason, whether the build succeeds or not; and an update names the
app's forms the spreadsheets do not mention instead of calling it a match.

Form linking is scripted, LLM enrichment and rule generation are switched
off and every network connection fails, so no API is called.

Usage:
    uv run --extra dev pytest tests/pipeline/test_nothing_built_and_skipped_sheets.py
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest
from langchain_core.messages import ToolMessage
from openpyxl.styles import Font
from test_spreadsheet_format_prompt import WORKBOOK, _write_workbook

import chat.tools as tools
from chat.prompts import SYSTEM_PROMPT
from config import Settings
from domain.diff import current_forms_not_in_desired
from domain.llm import LLMHelper
from domain.parser import SKIPPED_NO_ROWS, SKIPPED_UNRECOGNISED, parse_scoping_docs
from domain.rules.generator import RuleGenerator
from pipeline.nodes import (
    _CHECK_HEADINGS_ADVICE,
    _FILL_IN_ROWS_ADVICE,
    _NO_FORM_TO_BUILD_ERROR,
    _NO_SUBJECT_TYPE_ERROR,
    _NOTHING_TO_BUILD_ERROR,
    _skipped_sheets_messages,
)
from web.chat_service import ChatService
from web.sessions import ChatSession

ORG = "skipped_sheets"

# Sheets the builder does not read: two-column tabs with other headings, and
# a one-column list. A sheet whose every cell is struck through (out of
# scope) or holds only spaces is blank to the parser and is not reported.
UNRECOGNISED_SHEETS = {
    "Project Summary": [["Topic", "Detail"], ["Goal", "Track nutrition"]],
    "Reports": [["Report", "Owner"], ["Monthly", "Block officer"]],
    "Villages": [["Village"], ["Rampur"], ["Sitapur"]],
}
STRUCK_OUT_SHEET = "Dropped Ideas"
SPACES_SHEET = "Spaces"
SKIPPED_IN_EXTRAS = (
    "These sheets were skipped because their headings do not match any sheet "
    "the builder reads: 'Project Summary' in Extras.xlsx, 'Reports' in "
    "Extras.xlsx, 'Villages' in Extras.xlsx."
)


def _write_sheets(path: Path, sheets: dict[str, list[list]]) -> Path:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    workbook.save(path)
    return path


def _format_sheets(*names: str) -> dict[str, list[list]]:
    return {name: WORKBOOK[name] for name in names}


FORM_SHEETS = (
    "Woman Registration", "Household Registration", "Pregnancy Enrolment", "ANC", "Household Survey",
)


def _write_unrecognised_workbook(path: Path) -> Path:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in UNRECOGNISED_SHEETS.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    struck_out = workbook.create_sheet(STRUCK_OUT_SHEET)
    for row in (["Idea", "Reason"], ["SMS reminders", "No budget"]):
        struck_out.append(row)
    for cells in struck_out.iter_rows():
        for cell in cells:
            cell.font = Font(strike=True)
    spaces = workbook.create_sheet(SPACES_SHEET)
    for row in (["  ", " "], [" ", "   "]):
        spaces.append(row)
    workbook.save(path)
    return path


@pytest.fixture
def org_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scripted_form_linking: None,
) -> Path:
    def no_network(*_args, **_kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(LLMHelper, "is_available", lambda _self: False)
    monkeypatch.setattr(RuleGenerator, "is_available", lambda _self: False)
    directory = tmp_path / "input" / ORG
    directory.mkdir(parents=True)
    monkeypatch.setattr(
        tools, "settings",
        Settings(input_root=str(tmp_path / "input"), output_root=str(tmp_path / "output")),
    )
    return directory


def _build() -> dict:
    return asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))


def _model_reads_notes_only_as_already_shown(result: dict, notices: list[str]) -> None:
    """The model gets the shown notes only under `already_shown_to_user`, never as notices."""
    assert "notices" not in result
    assert "forms_not_in_spreadsheets" not in result
    assert result.get("already_shown_to_user", []) == notices


def _web_build() -> tuple[dict, list[str]]:
    """A build in a webapp session: what the model reads, and the notices the chat shows."""
    session = SimpleNamespace(
        bundle_path=None, org_setup_loading=False, build_needs_confirmation=False, pending_notices=[],
    )
    result = asyncio.run(
        tools.generate_bundle.ainvoke({"org": ORG}, config={"configurable": {"session": session}})
    )
    _model_reads_notes_only_as_already_shown(result, session.pending_notices)
    result.pop("already_shown_to_user", None)
    return result, session.pending_notices


def _web_update(bundle: Path) -> tuple[dict, list[str]]:
    """An update in a webapp session: what the model reads, and the notices the chat shows."""
    session = SimpleNamespace(bundle_path=bundle, org_setup_loading=False, pending_notices=[])
    result = asyncio.run(
        tools.edit_bundle_from_spec.ainvoke({"org": ORG}, config={"configurable": {"session": session}})
    )
    _model_reads_notes_only_as_already_shown(result, session.pending_notices)
    result.pop("already_shown_to_user", None)
    return result, session.pending_notices


def _update(bundle: Path) -> dict:
    return _web_update(bundle)[0]


def _built_bundle(org_dir: Path, tmp_path: Path) -> Path:
    """A bundle built from the format workbook, kept outside the output folder."""
    modelling = _write_workbook(org_dir / "Modelling.xlsx")
    result = _build()
    assert result["status"] == "done", result.get("errors")
    bundle = shutil.copy(result["zip_path"], tmp_path / "current.zip")
    modelling.unlink()
    return Path(bundle)


def _bundle_ready_events(result: dict, tmp_path: Path) -> list:
    session = ChatSession(session_id="s", org_name="Org", username="u", workdir=tmp_path)
    service = ChatService(session, agent_factory=lambda: None)
    service._handle_tool_message(ToolMessage(content=json.dumps(result), tool_call_id="c"))
    return [e for e in session.bus.replay_since(0) if e.type == "bundle.ready"]


def test_the_parser_reports_one_column_sheets_but_not_blank_ones(tmp_path: Path) -> None:
    _spec, skipped = parse_scoping_docs([_write_unrecognised_workbook(tmp_path / "Extras.xlsx")])
    assert [(s["name"], s["file"], s["reason"]) for s in skipped] == [
        (name, "Extras.xlsx", SKIPPED_UNRECOGNISED) for name in UNRECOGNISED_SHEETS
    ]


def test_sheets_with_the_right_headings_and_no_rows_are_reported_as_having_no_rows(
    tmp_path: Path,
) -> None:
    headings_only = {
        "Subjects": [WORKBOOK["Subjects"][0]],
        "Woman Registration": [WORKBOOK["Woman Registration"][0]],
        "ANC": [["ANC visit form"], WORKBOOK["ANC"][0]],
        "Rules": [WORKBOOK["Rules"][0], ["   ", ""]],
    }
    _spec, skipped = parse_scoping_docs([_write_sheets(tmp_path / "Empty.xlsx", headings_only)])
    assert [(s["name"], s["reason"]) for s in skipped] == [
        (name, SKIPPED_NO_ROWS) for name in headings_only
    ]


def test_each_skip_reason_gets_its_own_message_naming_sheets_and_files() -> None:
    sheets = [
        {"name": "Project Summary", "file": "Extras.xlsx", "reason": SKIPPED_UNRECOGNISED},
        {"name": "Subjects", "file": "Model.xlsx", "reason": SKIPPED_NO_ROWS},
        {"name": "Villages", "file": "Extras.xlsx", "reason": SKIPPED_UNRECOGNISED},
    ]
    assert _skipped_sheets_messages(sheets) == [
        (
            "These sheets were skipped because their headings do not match any sheet "
            "the builder reads: 'Project Summary' in Extras.xlsx, 'Villages' in Extras.xlsx."
        ),
        (
            "These sheets have the right headings but no rows under them, so nothing "
            "was read from them: 'Subjects' in Model.xlsx."
        ),
    ]
    assert _skipped_sheets_messages([]) == []


def test_a_form_sheet_with_headings_only_is_named_on_a_build_that_succeeds(org_dir: Path) -> None:
    sheets = _format_sheets("Locations", "Subjects", "Household Registration")
    sheets["Woman Registration"] = [WORKBOOK["Woman Registration"][0]]
    _write_sheets(org_dir / "Model.xlsx", sheets)

    result = _build()

    assert result["status"] == "done", result.get("error")
    assert (
        "These sheets have the right headings but no rows under them, so nothing "
        "was read from them: 'Woman Registration' in Model.xlsx."
    ) in result["notices"]


def test_a_build_that_finds_nothing_ends_with_an_error_and_no_card(
    org_dir: Path, tmp_path: Path,
) -> None:
    _write_unrecognised_workbook(org_dir / "Extras.xlsx")

    result = _build()

    assert result == {
        "status": "error",
        "error": f"{_NOTHING_TO_BUILD_ERROR} {_CHECK_HEADINGS_ADVICE}",
        "notices": [SKIPPED_IN_EXTRAS],
    }
    assert not list((tmp_path / "output").rglob("*.zip"))
    assert str(tmp_path) not in result["error"]
    assert _bundle_ready_events(result, tmp_path) == []


def test_an_update_from_spreadsheets_that_hold_nothing_changes_nothing(
    org_dir: Path, tmp_path: Path,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    before = bundle.read_bytes()
    _write_unrecognised_workbook(org_dir / "Extras.xlsx")

    result, notices = _web_update(bundle)

    assert result == {"status": "error", "error": f"{_NOTHING_TO_BUILD_ERROR} {_CHECK_HEADINGS_ADVICE}"}
    assert notices == [SKIPPED_IN_EXTRAS]
    assert bundle.read_bytes() == before


def test_a_build_with_subject_types_but_no_forms_ends_with_an_error_and_no_card(
    org_dir: Path, tmp_path: Path,
) -> None:
    _write_sheets(org_dir / "Modelling.xlsx", {
        **_format_sheets("Locations", "Subjects", "Programs", "Visits"),
        "Woman Registration": [WORKBOOK["Woman Registration"][0]],
    })

    result = _build()

    assert result == {"status": "error", "error": _NO_FORM_TO_BUILD_ERROR, "notices": [
        "These sheets have the right headings but no rows under them, so nothing "
        "was read from them: 'Woman Registration' in Modelling.xlsx."
    ]}
    assert not list((tmp_path / "output").rglob("*.zip"))
    assert _bundle_ready_events(result, tmp_path) == []


@pytest.mark.parametrize("run", ["build", "update"])
def test_when_every_sheet_has_headings_and_no_rows_the_error_asks_for_rows_not_headings(
    org_dir: Path, tmp_path: Path, run: str,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    _write_sheets(org_dir / "Model.xlsx", {
        "Subjects": [WORKBOOK["Subjects"][0]],
        "Woman Registration": [WORKBOOK["Woman Registration"][0]],
    })

    result, notices = _web_build() if run == "build" else _web_update(bundle)

    assert result == {"status": "error", "error": f"{_NOTHING_TO_BUILD_ERROR} {_FILL_IN_ROWS_ADVICE}"}
    assert notices == [
        "These sheets have the right headings but no rows under them, so nothing "
        "was read from them: 'Subjects' in Model.xlsx, 'Woman Registration' in Model.xlsx."
    ]


def test_a_mix_of_unrecognised_and_empty_sheets_still_asks_to_check_headings(
    org_dir: Path,
) -> None:
    _write_sheets(org_dir / "Model.xlsx", {
        "Subjects": [WORKBOOK["Subjects"][0]],
        "Project Summary": [["Topic", "Detail"], ["Goal", "Track nutrition"]],
    })

    result = _build()

    assert result["status"] == "error"
    assert result["error"] == f"{_NOTHING_TO_BUILD_ERROR} {_CHECK_HEADINGS_ADVICE}"
    assert result["notices"] == [
        "These sheets were skipped because their headings do not match any sheet "
        "the builder reads: 'Project Summary' in Model.xlsx.",
        "These sheets have the right headings but no rows under them, so nothing "
        "was read from them: 'Subjects' in Model.xlsx.",
    ]


@pytest.mark.parametrize("run", ["build", "update"])
def test_forms_without_a_subject_type_end_with_an_error_that_names_the_cause(
    org_dir: Path, tmp_path: Path, run: str,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    before = bundle.read_bytes()
    _write_sheets(org_dir / "Forms.xlsx", _format_sheets(*FORM_SHEETS))

    result, notices = _web_build() if run == "build" else _web_update(bundle)

    assert result == {"status": "error", "error": _NO_SUBJECT_TYPE_ERROR}
    assert notices == []
    assert bundle.read_bytes() == before


def test_an_update_whose_spreadsheets_have_none_of_the_apps_forms_names_them(
    org_dir: Path, tmp_path: Path,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    before = bundle.read_bytes()
    _write_sheets(org_dir / "Model.xlsx", _format_sheets("Locations", "Subjects"))

    result, notices = _web_update(bundle)

    app_forms = [
        "ANC", "ANC Encounter Cancellation", "Household Registration", "Household Survey",
        "Household Survey Cancellation", "Pregnancy Enrolment", "Woman Registration",
    ]
    assert result["status"] == "error"
    assert "no_changes" not in result
    assert result["error"] == (
        "Nothing in the bundle was changed. None of the app's forms are in the "
        "spreadsheets, so there was nothing to update."
    )
    assert notices == [
        "These forms are in the app but not in the spreadsheets, and an update does "
        "not remove forms, so they were left as they are: " + ", ".join(app_forms) + "."
    ]
    assert bundle.read_bytes() == before


def test_an_update_missing_some_of_the_apps_forms_does_not_call_it_a_match(
    org_dir: Path, tmp_path: Path,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    _write_sheets(
        org_dir / "Model.xlsx",
        {name: rows for name, rows in WORKBOOK.items() if name != "Household Survey"},
    )

    result, notices = _web_update(bundle)

    assert result["status"] == "done"
    assert result["no_changes"] is True
    assert "Household Survey" not in json.dumps(result)
    assert result["message"] == (
        "Nothing was changed: the forms in the spreadsheets already match the app, "
        "and the app's other forms were left as they are."
    )
    assert notices == [
        "These forms are in the app but not in the spreadsheets, and an update does "
        "not remove forms, so they were left as they are: Household Survey, "
        "Household Survey Cancellation."
    ]


def test_a_failed_update_shows_the_sheets_it_skipped_without_the_model(
    org_dir: Path, tmp_path: Path,
) -> None:
    without_survey = {name: rows for name, rows in WORKBOOK.items() if name != "Household Survey"}
    _write_sheets(org_dir / "Modelling.xlsx", without_survey)
    built = _build()
    assert built["status"] == "done", built.get("error")
    bundle = Path(shutil.copy(built["zip_path"], tmp_path / "current.zip"))
    _write_workbook(org_dir / "Modelling.xlsx")
    _write_unrecognised_workbook(org_dir / "Extras.xlsx")

    result, notices = _web_update(bundle)

    assert result["status"] == "error"
    assert "update cannot add new forms" in result["error"]
    assert SKIPPED_IN_EXTRAS not in result["error"]
    assert notices == [SKIPPED_IN_EXTRAS]


def test_an_update_from_the_full_spreadsheets_reports_no_missing_forms(
    org_dir: Path, tmp_path: Path,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    _write_workbook(org_dir / "Modelling.xlsx")

    result, notices = _web_update(bundle)

    assert notices == []
    assert result["message"] == "The bundle already matches the spreadsheets; nothing was changed."


def test_voided_forms_are_not_listed_as_missing_from_the_spreadsheets() -> None:
    def bundle(*forms: tuple[str, str, bool]) -> dict:
        return {"forms": [
            {"content": {"uuid": uuid, "name": name, "voided": voided}} for uuid, name, voided in forms
        ]}

    current = bundle(("u1", "ANC", False), ("u2", "Old Survey", True), ("u3", "Household", False))
    desired = bundle(("u1", "ANC", False))
    assert current_forms_not_in_desired(desired, current) == ["Household"]


def test_a_build_names_the_sheets_it_skipped(org_dir: Path) -> None:
    _write_workbook(org_dir / "Modelling.xlsx")
    _write_unrecognised_workbook(org_dir / "Extras.xlsx")

    result = _build()

    assert result["status"] == "done", result.get("errors")
    assert result["main_forms"] > 0
    assert result["notices"] == [SKIPPED_IN_EXTRAS]
    assert SKIPPED_IN_EXTRAS not in result["parse_warnings"]
    assert Path(result["zip_path"]).is_file()


def test_a_web_build_hands_its_notices_to_the_chat_and_marks_them_shown_for_the_model(org_dir: Path) -> None:
    _write_workbook(org_dir / "Modelling.xlsx")
    _write_unrecognised_workbook(org_dir / "Extras.xlsx")

    result, notices = _web_build()

    assert result["status"] == "done"
    assert result["bundle_updated"] is True
    assert notices == [SKIPPED_IN_EXTRAS]


def test_an_update_that_changes_nothing_still_names_the_sheets_it_skipped(
    org_dir: Path, tmp_path: Path,
) -> None:
    bundle = _built_bundle(org_dir, tmp_path)
    _write_workbook(org_dir / "Modelling.xlsx")
    _write_unrecognised_workbook(org_dir / "Extras.xlsx")

    result, notices = _web_update(bundle)

    assert result["status"] == "done"
    assert result["no_changes"] is True
    assert notices == [SKIPPED_IN_EXTRAS]


def test_a_build_with_no_spreadsheet_says_so_without_a_path(org_dir: Path, tmp_path: Path) -> None:
    result = _build()
    assert result["status"] == "error"
    assert "No spreadsheet has been uploaded" in result["error"]
    assert str(tmp_path) not in result["error"]


def test_the_prompt_tells_the_model_to_name_skipped_sheets_and_not_call_an_empty_build_ready() -> None:
    prompt = re.sub(r"\s+", " ", SYSTEM_PROMPT)
    assert "has `notices`, tell the user each one as written, whether it succeeded or failed" in prompt
    assert "say the app already matches the spreadsheets" not in prompt
    assert "do not add that the app matches the spreadsheets" in prompt
    assert "hold no subject type, or a build whose spreadsheets hold no form, returns an error and no card" in prompt
    assert "never announce that the app is ready" in prompt
    assert "Never say the app matches the spreadsheets while an update result's `forms_not_in_spreadsheets` is not empty" in prompt

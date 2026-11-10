"""The spreadsheet format the chat describes is the one the bundle builder reads.

A workbook is built from the headings in the prompt's "Spreadsheet format
the app builder reads" section, parsed, and built into a bundle offline.
Every heading used must appear in that section, and every Data Type value
the section lists must map to an Avni data type, so the prompt and the
parser cannot drift apart unnoticed.

LLM enrichment and rule generation are switched off, the model's form-linking
call is replaced by a scripted one (`scripted_form_linking`), and every
network connection fails, so no API is called.

Usage:
    uv run --extra dev pytest tests/pipeline/test_spreadsheet_format_prompt.py
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import zipfile
from pathlib import Path

import openpyxl
import pytest

import chat.tools as tools
from chat.prompts import SYSTEM_PROMPT
from config import Settings
from domain.llm import LLMHelper
from domain.parser import _DATA_TYPE_MAP, parse_scoping_docs
from domain.rules.generator import RuleGenerator

ORG = "format_check"

_SECTION_START = "Spreadsheet format the app builder reads:"
_SECTION_END = "  Generation:"

WORKBOOK: dict[str, list[list[str]]] = {
    "Locations": [
        ["Location Type", "Count"],
        ["State", "1"],
        ["District", "4"],
        ["Village", "120"],
    ],
    "Subjects": [
        ["Subject Type Name", "Type", "Registration Form"],
        ["Woman", "Person", "Woman Registration"],
        ["Household", "Household", "Household Registration"],
    ],
    "Programs": [
        ["Program Name", "Target Subject Type", "Enrolment Form", "Exit Form"],
        ["Pregnancy", "Woman", "Pregnancy Enrolment", ""],
    ],
    "Visits": [
        ["Encounter Name", "Subject Type", "Encounter Type (Scheduled/Unscheduled)",
         "Forms Linked", "Cancellation Form"],
        ["Household Survey", "Household", "Unscheduled", "Household Survey", ""],
    ],
    "Program Visits": [
        ["Encounter Name", "Program Name", "Encounter Type (Scheduled/Unscheduled)",
         "Forms Linked", "Cancellation Form"],
        ["ANC", "Pregnancy", "Scheduled", "ANC", ""],
    ],
    "Woman Registration": [
        ["Page Name", "Field Name", "Data Type", "Mandatory", "Options",
         "Selection Type", "Min and Max", "Unit", "When to show"],
        ["Basic details", "Age", "Numeric", "Yes", "", "", "15-49", "years", ""],
        ["", "Mobile", "Phone Number", "No", "", "", "", "", ""],
        ["", "Works outside home", "Yes/No", "No", "", "", "", "", ""],
        ["Health", "Conditions", "Coded", "No", "Anaemia\nDiabetes", "Multi", "", "", ""],
        ["", "Smoker", "Multi Select", "No", "Yes\nNo", "Single", "", "", ""],
        ["", "Other condition", "Text", "No", "", "", "", "", "When 'Conditions' is Other"],
    ],
    "Household Registration": [
        ["Field Name", "Data Type"],
        ["Household size", "Numeric"],
    ],
    "Pregnancy Enrolment": [
        ["Field Name", "Data Type", "Mandatory"],
        ["LMP", "Date", "Yes"],
    ],
    "ANC": [
        ["Field Name", "Data Type", "Options", "Default value", "Validation"],
        ["Blood pressure", "Notes", "", "", ""],
        ["Danger signs", "Multi Select", "Bleeding\nFever", "", ""],
        ["Risk level", "Coded", "Low; High", "Low", ""],
        ["Weight", "Numeric", "", "", "Block save when weight is above 150"],
    ],
    "Household Survey": [
        ["Field Name", "Data Type"],
        ["Water source", "Single Select"],
    ],
    "Rules": [
        ["Form name", "Visit Schedule Rule", "Validation Rule"],
        ["ANC", "Schedule the next ANC 30 days later", ""],
    ],
}


def _format_section() -> str:
    start = SYSTEM_PROMPT.index(_SECTION_START)
    return SYSTEM_PROMPT[start:SYSTEM_PROMPT.index(_SECTION_END, start)]


def _write_workbook(path: Path) -> Path:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in WORKBOOK.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    workbook.save(path)
    return path


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_every_heading_used_is_in_the_prompt() -> None:
    section = _flatten(_format_section())
    headings = {row for rows in WORKBOOK.values() for row in rows[0]}
    missing = sorted(h for h in headings if f'"{h}"' not in section)
    assert missing == []


def test_every_data_type_the_prompt_lists_maps_to_an_avni_type() -> None:
    section = _flatten(_format_section())
    listed = section.split("Data Type values:")[1].split(".")[0]
    names = [n.strip() for n in listed.split(",") if n.strip()]
    assert len(names) >= 10
    unknown = [n for n in names if n.lower() not in _DATA_TYPE_MAP]
    assert unknown == []
    for alias in ("Single Select", "Multi Select", "Yes/No"):
        assert _DATA_TYPE_MAP[alias.lower()] == "Coded"


def test_a_workbook_in_the_described_format_is_read_in_full(tmp_path: Path) -> None:
    spec, skipped = parse_scoping_docs([_write_workbook(tmp_path / "Modelling.xlsx")])

    assert [a.name for a in spec.address_levels] == ["State", "District", "Village"]
    assert {(s.name, s.type) for s in spec.subject_types} == {
        ("Woman", "Person"), ("Household", "Household"),
    }
    assert [(p.name, p.target_subject_type) for p in spec.programs] == [("Pregnancy", "Woman")]
    visits = {e.name: e for e in spec.encounter_types}
    assert visits["Household Survey"].subject_type == "Household"
    assert visits["Household Survey"].is_scheduled is False
    assert visits["ANC"].program_name == "Pregnancy" and visits["ANC"].is_scheduled
    assert skipped == []

    forms = {f.name: f for f in spec.forms}
    assert set(forms) == {
        "Woman Registration", "Household Registration", "Pregnancy Enrolment",
        "ANC", "Household Survey",
    }
    registration = {f.name: f for s in forms["Woman Registration"].sections for f in s.fields}
    assert [s.name for s in forms["Woman Registration"].sections] == ["Basic details", "Health"]
    assert registration["Age"].dataType == "Numeric" and registration["Age"].mandatory
    assert (registration["Age"].min, registration["Age"].max, registration["Age"].unit) == (15, 49, "years")
    assert registration["Mobile"].dataType == "PhoneNumber"
    assert registration["Works outside home"].options == ["Yes", "No"]
    assert registration["Conditions"].options == ["Anaemia", "Diabetes"]
    assert registration["Conditions"].selectionType == "MultiSelect"
    assert registration["Smoker"].selectionType == "SingleSelect"
    assert "Conditions" in registration["Other condition"].rule_intent

    anc = {f.name: f for s in forms["ANC"].sections for f in s.fields}
    assert anc["Risk level"].options == ["Low", "High"]
    assert anc["Danger signs"].selectionType == "MultiSelect"
    assert anc["Risk level"].rule_intent and anc["Weight"].rule_intent
    assert forms["ANC"].rule_intents


@pytest.fixture
def offline_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    def no_network(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(LLMHelper, "is_available", lambda _self: False)
    monkeypatch.setattr(RuleGenerator, "is_available", lambda _self: False)
    org_dir = tmp_path / "input" / ORG
    org_dir.mkdir(parents=True)
    _write_workbook(org_dir / "Modelling.xlsx")
    monkeypatch.setattr(
        tools, "settings",
        Settings(input_root=str(tmp_path / "input"), output_root=str(tmp_path / "output")),
    )
    return org_dir


def test_a_workbook_in_the_described_format_builds_a_bundle(
    offline_pipeline: Path, scripted_form_linking: None,
) -> None:
    result = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))
    assert result["status"] == "done", result.get("errors")
    assert result["subject_types"] == 2
    assert result["programs"] == 1
    assert result["encounter_types"] == 2
    assert result["main_forms"] >= 5
    assert "unlinked_forms" not in result

    with zipfile.ZipFile(result["zip_path"]) as bundle:
        forms = {
            form["name"]: form
            for form in (json.loads(bundle.read(name)) for name in bundle.namelist()
                         if name.startswith("forms/"))
        }
        mappings = {m["formName"]: m for m in json.loads(bundle.read("formMappings.json"))}
    assert set(mappings) == set(forms)
    assert mappings["Woman Registration"]["formType"] == "IndividualProfile"
    assert mappings["Pregnancy Enrolment"]["formType"] == "ProgramEnrolment"
    assert mappings["ANC"]["formType"] == "ProgramEncounter"
    assert mappings["ANC"].keys() >= {"subjectTypeUUID", "programUUID", "encounterTypeUUID"}
    assert mappings["Household Survey"]["formType"] == "Encounter"
    assert {"Woman Registration", "ANC", "Household Survey"} <= set(forms)
    element_types = {
        element["name"]: element["type"]
        for form in forms.values()
        for group in form["formElementGroups"]
        for element in group["formElements"]
    }
    assert element_types["Danger signs"] == "MultiSelect"
    assert element_types["Conditions"] == "MultiSelect"
    assert element_types["Smoker"] == "SingleSelect"
    assert element_types["Risk level"] == "SingleSelect"


def test_a_build_whose_forms_cannot_be_linked_is_not_offered(offline_pipeline: Path) -> None:
    """Without the model's form-linking call every form is left unlinked."""
    result = asyncio.run(tools.generate_bundle.ainvoke({"org": ORG}))
    assert result["status"] == "error"
    assert result["zip_path"] == ""
    assert "None of the forms could be linked" in result["error"]
    assert "ANC" in result["unlinked_forms"]
    reported_counts = {"subject_types", "programs", "encounter_types", "main_forms",
                       "cancellation_forms", "concepts", "form_mappings"}
    assert reported_counts.isdisjoint(result)
    warnings = " ".join(result["parse_warnings"] + result["enrich_warnings"] + result["errors"])
    assert "API_KEY" not in warnings
    assert "AI service is not available" in warnings


def test_unrecognised_data_types_are_named_and_known_spellings_are_read(tmp_path: Path) -> None:
    spec, _skipped = _parse_sheets(tmp_path, {"Visit": [
        ["Field Name", "Data Type", "Options"],
        ["Symptoms", "MultiSelect", "Fever\nCough"],
        ["Visit time", "Time", ""],
        ["Seen at", "Date Time", ""],
        ["Card number", "Id", ""],
    ]})
    fields = {f.name: f for s in spec.forms[0].sections for f in s.fields}
    assert (fields["Symptoms"].dataType, fields["Symptoms"].selectionType) == ("Coded", "MultiSelect")
    assert fields["Visit time"].dataType == "Time"
    assert fields["Seen at"].dataType == "DateTime"
    assert fields["Card number"].dataType == "Text"
    assert [w for w in spec.validation_warnings if "Data Type" in w] == [
        "Form 'Visit', field 'Card number': Data Type 'Id' is not one the builder "
        "reads, so the field was read as Text."
    ]


def _parse_sheets(tmp_path: Path, sheets: dict[str, list[list[str]]]):
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    path = tmp_path / "Sheets.xlsx"
    workbook.save(path)
    return parse_scoping_docs([path])


def test_a_one_column_location_sheet_is_skipped(tmp_path: Path) -> None:
    spec, _skipped = _parse_sheets(tmp_path, {"Locations": [["Location Type"], ["Region"], ["Village"]]})
    assert [a.name for a in spec.address_levels] == ["State", "District", "Village"]


def test_a_program_heading_turns_visits_into_program_visits(tmp_path: Path) -> None:
    spec, _skipped = _parse_sheets(tmp_path, {"Visits": [
        ["Encounter Name", "Subject Type", "Program Notes"],
        ["Household Survey", "Household", "Pregnancy"],
    ]})
    assert spec.encounter_types[0].is_program_encounter


def test_an_invented_layout_reads_nothing(tmp_path: Path) -> None:
    """A layout the chat once described in production ("Sheet name: config",
    a "Read Only" column) is not a layout the builder reads."""
    spec, _skipped = _parse_sheets(tmp_path, {"config": [
        ["Entity", "Name", "Read Only"],
        ["Subject Type", "Woman", "No"],
        ["Program", "Pregnancy", "No"],
    ]})
    assert spec.subject_types == [] and spec.programs == [] and spec.forms == []

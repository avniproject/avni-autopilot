"""
LangGraph pipeline that wires the bundle generation workflow:

  discover_files → parse_documents → generate_entities → generate_forms
      → generate_form_mappings → package_zip

Each node is thin — heavy lifting lives in `generators.py` and `parser.py`.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys
import zipfile
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

# src/ on sys.path so sibling modules (parser, generators, models) are importable
# regardless of working directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from generators import (  # noqa: E402
    make_address_level_types,
    make_encounter_types,
    make_form_mappings,
    make_forms_and_concepts,
    make_operational_encounter_types,
    make_operational_programs,
    make_operational_subject_types,
    make_organisation_config,
    make_programs,
    make_subject_types,
)

log = logging.getLogger(__name__)

# ── ZIP canonical order (mirrors BundleService.java / zip_bundle.js) ─────────
_CANONICAL_ORDER = [
    "addressLevelTypes.json",
    "subjectTypes.json",
    "operationalSubjectTypes.json",
    "encounterTypes.json",
    "operationalEncounterTypes.json",
    "programs.json",
    "operationalPrograms.json",
    "concepts.json",
    "__FORMS__",
    "formMappings.json",
    "organisationConfig.json",
]


# ── State ─────────────────────────────────────────────────────────────────────


class BundleState(TypedDict):
    org_name: str
    input_dir: str
    output_dir: str
    file_paths: list[str]
    entity_spec: Any                        # EntitySpec — populated by parse_documents
    parse_warnings: list[str]
    subject_types_json: list[dict]
    operational_subject_types_json: dict
    programs_json: list[dict]
    operational_programs_json: dict
    encounter_types_json: list[dict]
    operational_encounter_types_json: dict
    address_level_types_json: list[dict]
    organisation_config_json: dict
    forms_json: list[dict]                  # [{file_name, content}, ...]
    concepts_json: list[dict]
    mapping_specs: list[dict]               # intermediate metadata for mapping resolution
    form_mappings_json: list[dict]
    zip_path: str
    errors: list[str]


# ── Node 1: discover input files ──────────────────────────────────────────────


def discover_files(state: BundleState) -> BundleState:
    input_dir = state["input_dir"]
    errors = list(state.get("errors", []))

    if not os.path.isdir(input_dir):
        errors.append(f"Input directory not found: {input_dir}")
        return {**state, "file_paths": [], "errors": errors}

    file_paths = sorted(glob.glob(os.path.join(input_dir, "*.xlsx")))
    if not file_paths:
        errors.append(f"No .xlsx files found in: {input_dir}")

    log.info(
        "[%s] Found %d file(s): %s",
        state["org_name"], len(file_paths),
        [os.path.basename(p) for p in file_paths],
    )
    return {**state, "file_paths": file_paths, "errors": errors}


# ── Node 2: parse documents into an EntitySpec ────────────────────────────────


def parse_documents(state: BundleState) -> BundleState:
    errors = list(state.get("errors", []))

    try:
        from parser import parse_scoping_docs  # noqa: PLC0415
    except ImportError as exc:
        errors.append(f"Cannot import scoping parser: {exc}")
        return {**state, "entity_spec": None, "parse_warnings": [], "errors": errors}

    try:
        entity_spec, misc_sheets = parse_scoping_docs(state["file_paths"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Parsing failed: {exc}")
        return {**state, "entity_spec": None, "parse_warnings": [], "errors": errors}

    warnings = list(entity_spec.validation_warnings)
    if misc_sheets:
        names = [s["name"] for s in misc_sheets]
        warnings.append(f"{len(misc_sheets)} unclassified sheet(s): {names}")

    log.info(
        "[%s] Parsed — subjects=%d programs=%d encounters=%d forms=%d",
        state["org_name"],
        len(entity_spec.subject_types),
        len(entity_spec.programs),
        len(entity_spec.encounter_types),
        len(entity_spec.forms),
    )
    return {**state, "entity_spec": entity_spec, "parse_warnings": warnings, "errors": errors}


# ── Node 3: generate entity-level JSON ────────────────────────────────────────


def generate_entities(state: BundleState) -> BundleState:
    spec = state["entity_spec"]

    subject_types_json = make_subject_types(spec.subject_types)
    programs_json = make_programs(spec.programs)
    encounter_types_json = make_encounter_types(spec.encounter_types)

    return {
        **state,
        "subject_types_json": subject_types_json,
        "operational_subject_types_json": make_operational_subject_types(subject_types_json),
        "programs_json": programs_json,
        "operational_programs_json": make_operational_programs(programs_json),
        "encounter_types_json": encounter_types_json,
        "operational_encounter_types_json": make_operational_encounter_types(encounter_types_json),
        "address_level_types_json": make_address_level_types(spec.address_levels),
        "organisation_config_json": make_organisation_config(state["org_name"]),
    }


# ── Node 4: generate forms + concepts ─────────────────────────────────────────


def generate_forms(state: BundleState) -> BundleState:
    spec = state["entity_spec"]

    result = make_forms_and_concepts(spec.forms)
    forms_json = result["forms"]
    concepts_json = result["concepts"]
    mapping_specs = result["mapping_specs"]

    cancel_count = sum(1 for f in mapping_specs if "Cancellation" in f["name"])
    log.info(
        "[%s] Generated %d form(s) (%d with cancellation), %d concept(s)",
        state["org_name"], len(forms_json), cancel_count, len(concepts_json),
    )

    return {
        **state,
        "forms_json": forms_json,
        "concepts_json": concepts_json,
        "mapping_specs": mapping_specs,
    }


# ── Node 5: generate form mappings ────────────────────────────────────────────


def generate_form_mappings(state: BundleState) -> BundleState:
    # Use parser's fuzzy matcher to resolve typos/trailing-spaces in entity names
    try:
        from parser import _fuzzy_match  # noqa: PLC0415
    except ImportError:
        _fuzzy_match = None  # type: ignore[assignment]

    mappings = make_form_mappings(
        state["mapping_specs"],
        state["subject_types_json"],
        state["programs_json"],
        state["encounter_types_json"],
        fuzzy_match=_fuzzy_match,
    )
    return {**state, "form_mappings_json": mappings}


# ── Node 6: write JSON files + ZIP bundle ─────────────────────────────────────


def package_zip(state: BundleState) -> BundleState:
    output_dir = state["output_dir"]
    org_name = state["org_name"]
    errors = list(state.get("errors", []))

    os.makedirs(output_dir, exist_ok=True)
    forms_dir = os.path.join(output_dir, "forms")
    os.makedirs(forms_dir, exist_ok=True)

    def _write(path: str, data: Any) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

    flat_files: dict[str, Any] = {
        "addressLevelTypes.json": state["address_level_types_json"],
        "subjectTypes.json": state["subject_types_json"],
        "operationalSubjectTypes.json": state["operational_subject_types_json"],
        "encounterTypes.json": state["encounter_types_json"],
        "operationalEncounterTypes.json": state["operational_encounter_types_json"],
        "programs.json": state["programs_json"],
        "operationalPrograms.json": state["operational_programs_json"],
        "concepts.json": state["concepts_json"],
        "formMappings.json": state["form_mappings_json"],
        "organisationConfig.json": state["organisation_config_json"],
    }

    try:
        for filename, data in flat_files.items():
            _write(os.path.join(output_dir, filename), data)
        for form in state["forms_json"]:
            _write(os.path.join(forms_dir, form["file_name"]), form["content"])
    except OSError as exc:
        errors.append(f"Failed to write output files: {exc}")
        return {**state, "errors": errors}

    zip_path = os.path.join(output_dir, f"{org_name.capitalize()}.zip")
    try:
        file_map: dict[str, str] = {
            **{name: os.path.join(output_dir, name) for name in flat_files},
            **{
                f"forms/{f['file_name']}": os.path.join(forms_dir, f["file_name"])
                for f in state["forms_json"]
            },
        }
        form_entries = sorted(k for k in file_map if k.startswith("forms/"))

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in _CANONICAL_ORDER:
                if entry == "__FORMS__":
                    for fe in form_entries:
                        zf.write(file_map[fe], fe)
                elif entry in file_map:
                    zf.write(file_map[entry], entry)
    except OSError as exc:
        errors.append(f"Failed to create ZIP: {exc}")
        return {**state, "errors": errors}

    log.info("[%s] Bundle written → %s", org_name, zip_path)
    return {**state, "zip_path": zip_path, "errors": errors}


# ── Conditional edge: abort early on hard errors ──────────────────────────────


def _can_proceed(state: BundleState) -> str:
    if state.get("errors") and not state.get("file_paths"):
        return "abort"
    if state.get("errors") and state.get("entity_spec") is None:
        return "abort"
    return "continue"


# ── Graph assembly ────────────────────────────────────────────────────────────


def build_graph() -> Any:
    graph = StateGraph(BundleState)

    graph.add_node("discover_files", discover_files)
    graph.add_node("parse_documents", parse_documents)
    graph.add_node("generate_entities", generate_entities)
    graph.add_node("generate_forms", generate_forms)
    graph.add_node("generate_form_mappings", generate_form_mappings)
    graph.add_node("package_zip", package_zip)

    graph.set_entry_point("discover_files")
    graph.add_conditional_edges(
        "discover_files", _can_proceed,
        {"continue": "parse_documents", "abort": END},
    )
    graph.add_conditional_edges(
        "parse_documents", _can_proceed,
        {"continue": "generate_entities", "abort": END},
    )
    graph.add_edge("generate_entities", "generate_forms")
    graph.add_edge("generate_forms", "generate_form_mappings")
    graph.add_edge("generate_form_mappings", "package_zip")
    graph.add_edge("package_zip", END)

    return graph.compile()


# ── Public entry point ────────────────────────────────────────────────────────


def run(org_name: str, input_dir: str, output_dir: str) -> BundleState:
    pipeline = build_graph()
    initial: BundleState = {
        "org_name": org_name,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "file_paths": [],
        "entity_spec": None,
        "parse_warnings": [],
        "subject_types_json": [],
        "operational_subject_types_json": {},
        "programs_json": [],
        "operational_programs_json": {},
        "encounter_types_json": [],
        "operational_encounter_types_json": {},
        "address_level_types_json": [],
        "organisation_config_json": {},
        "forms_json": [],
        "concepts_json": [],
        "mapping_specs": [],
        "form_mappings_json": [],
        "zip_path": "",
        "errors": [],
    }
    return pipeline.invoke(initial)

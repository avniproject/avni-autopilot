"""
Node functions for the bundle generation pipeline.

Each node is a function `state → partial state dict`. Domain work is
delegated to modules under `domain/`; the node itself only handles state
shape and any LangGraph mechanics (`interrupt()` for human-in-the-loop).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import zipfile
from typing import Any

import pandas as pd
from langgraph.types import interrupt

from domain.changes import apply_resolutions
from domain.enricher import enrich_forms
from domain.generators import (
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
from domain.llm import LLMHelper
from domain.parser import _fuzzy_match, parse_scoping_docs
from models import Change
from pipeline.state import BundleState

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


# ── Node 1: discover input files ──────────────────────────────────────────────


def discover_files(state: BundleState) -> dict:
    input_dir = state["input_dir"]
    errors = list(state.get("errors", []))

    if not os.path.isdir(input_dir):
        errors.append(f"Input directory not found: {input_dir}")
        return {"file_paths": [], "errors": errors}

    file_paths = sorted(glob.glob(os.path.join(input_dir, "*.xlsx")))
    if not file_paths:
        errors.append(f"No .xlsx files found in: {input_dir}")

    log.info(
        "[%s] Found %d file(s): %s",
        state["org_name"], len(file_paths),
        [os.path.basename(p) for p in file_paths],
    )
    return {"file_paths": file_paths, "errors": errors}


# ── Node 2: parse documents into an EntitySpec ────────────────────────────────


def parse_documents(state: BundleState) -> dict:
    errors = list(state.get("errors", []))

    try:
        entity_spec, misc_sheets = parse_scoping_docs(state["file_paths"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Parsing failed: {exc}")
        return {"entity_spec": None, "parse_warnings": [], "errors": errors}

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
    return {"entity_spec": entity_spec, "parse_warnings": warnings, "errors": errors}


# ── Node 2.5: LLM enrichment ──────────────────────────────────────────────────


def _load_form_sheets(file_paths: list[str]) -> dict[str, pd.DataFrame]:
    """Re-read every .xlsx sheet keyed by sheet name (matches FormSpec.name)."""
    sheets: dict[str, pd.DataFrame] = {}
    for path in file_paths:
        if not str(path).lower().endswith((".xlsx", ".xls")):
            continue
        try:
            xf = pd.ExcelFile(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open %s for enrichment: %s", path, exc)
            continue
        for name in xf.sheet_names:
            try:
                df = pd.read_excel(xf, sheet_name=name, header=None)
                sheets[name.strip()] = df
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read sheet '%s' from %s: %s", name, path, exc)
    return sheets


def enrich_with_llm(state: BundleState) -> dict:
    """Call Claude once to refine each form; store any pending changes in state.

    The actual confirmation interrupt + apply lives in `apply_user_decisions`
    so that resuming after the user confirms only re-runs the apply node — not
    the LLM call. Re-running the LLM produced fresh non-deterministic
    change_ids that didn't match the resolutions the user had answered against.
    """
    spec = state["entity_spec"]
    if spec is None or not spec.forms:
        return {"pending_changes": [], "applied_changes": [], "enrich_warnings": []}

    helper = LLMHelper()
    if not helper.is_available():
        log.info("[%s] ANTHROPIC_API_KEY not set; skipping LLM enrichment.", state["org_name"])
        return {
            "pending_changes": [],
            "applied_changes": [],
            "enrich_warnings": ["LLM enrichment skipped: ANTHROPIC_API_KEY not set."],
        }

    sheets = _load_form_sheets(state["file_paths"])
    refined_forms, applied, pending, warnings = enrich_forms(
        spec.forms, sheets, state.get("user_instructions"), helper,
    )

    spec.forms = refined_forms

    log.info(
        "[%s] Enrichment: %d forms refined, %d auto-applied, %d pending, %d warnings.",
        state["org_name"], len(refined_forms), len(applied), len(pending), len(warnings),
    )

    return {
        "entity_spec": spec,
        "pending_changes": [c.model_dump() for c in pending],
        "applied_changes": [c.model_dump() for c in applied],
        "enrich_warnings": warnings,
    }


def apply_user_decisions(state: BundleState) -> dict:
    """If there are pending changes, ask the user via interrupt and apply.

    On resume, only this node re-executes — `enrich_with_llm` already ran on
    the first pass and its pending list is in state. The change_ids the user
    confirmed therefore still match.
    """
    pending_dicts = state.get("pending_changes") or []
    if not pending_dicts:
        return {}

    resolutions = interrupt({
        "kind": "confirm_changes",
        "org": state["org_name"],
        "changes": pending_dicts,
    })

    pending = [Change(**d) for d in pending_dicts]
    spec = state["entity_spec"]
    confirmed_applied, post_warnings = apply_resolutions(
        spec.forms, pending, resolutions or {},
    )

    applied_dicts = list(state.get("applied_changes") or [])
    applied_dicts.extend(c.model_dump() for c in confirmed_applied)
    warnings = list(state.get("enrich_warnings") or [])
    warnings.extend(post_warnings)

    return {
        "entity_spec": spec,
        "pending_changes": [],
        "applied_changes": applied_dicts,
        "enrich_warnings": warnings,
    }


# ── Node 3: generate entity-level JSON ────────────────────────────────────────


def generate_entities(state: BundleState) -> dict:
    spec = state["entity_spec"]

    subject_types_json = make_subject_types(spec.subject_types)
    programs_json = make_programs(spec.programs)
    encounter_types_json = make_encounter_types(spec.encounter_types)

    return {
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


def generate_forms(state: BundleState) -> dict:
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
        "forms_json": forms_json,
        "concepts_json": concepts_json,
        "mapping_specs": mapping_specs,
    }


# ── Node 5: generate form mappings ────────────────────────────────────────────


def generate_form_mappings(state: BundleState) -> dict:
    mappings = make_form_mappings(
        state["mapping_specs"],
        state["subject_types_json"],
        state["programs_json"],
        state["encounter_types_json"],
        fuzzy_match=_fuzzy_match,
    )
    return {"form_mappings_json": mappings}


# ── Node 6: write JSON files + ZIP bundle ─────────────────────────────────────


def package_zip(state: BundleState) -> dict:
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
        return {"errors": errors}

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
        return {"errors": errors}

    log.info("[%s] Bundle written → %s", org_name, zip_path)
    return {"zip_path": zip_path, "errors": errors}

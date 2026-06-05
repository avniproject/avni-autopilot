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

from domain.bundle_editor import apply_field_edits, load_bundle_snapshot
from domain.changes import apply_resolutions
from domain.diff import diff
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
from domain.rules.generator import RuleGenerator
from domain.rules.rule_spec import RuleKind, RuleSpec
from domain.rules.validator import check as validate_rule
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


# ── Node 5.5: generate rule JS for forms that carry a rule_intent ─────────────


def generate_rules(state: BundleState) -> dict:
    """Generate `visitScheduleRule` (and future kinds) for forms with intents.

    Runs after `generate_form_mappings` so `RuleSpec` is built from the same
    bundle shape the chat tool sees on its calls (SDD §4.3). Forms without any
    rule intent are skipped — zero embedding / Anthropic cost when nothing
    needs generating.

    Mutates the form JSON in `state["forms_json"]` in place to set the rule
    field on each form whose generation passes validation; failures flow into
    `parse_warnings` namespaced `rules.<kind>.<form>: <reason>`.
    """
    spec = state["entity_spec"]
    if spec is None or not spec.forms:
        return {}

    forms_with_intents = [f for f in spec.forms if f.rule_intents]
    if not forms_with_intents:
        return {}

    warnings = list(state.get("parse_warnings", []))
    forms_json = state["forms_json"]
    forms_by_name: dict[str, dict] = {
        entry["content"]["name"]: entry for entry in forms_json
    }
    available_encounter_types = [et["name"] for et in state["encounter_types_json"]]
    available_programs = [p["name"] for p in state["programs_json"]]

    generator = RuleGenerator()
    if not generator.is_available():
        warnings.append(
            "rules: skipped — ANTHROPIC_API_KEY not set"
        )
        log.info("[%s] Rule generation skipped — no ANTHROPIC_API_KEY",
                 state["org_name"])
        return {"parse_warnings": warnings}

    written = 0
    for form_spec in forms_with_intents:
        for kind_value, intent in form_spec.rule_intents.items():
            try:
                kind = RuleKind(kind_value)
            except ValueError:
                warnings.append(
                    f"rules.{kind_value}.{form_spec.name}: "
                    f"unknown rule kind, skipped"
                )
                continue

            rule_spec = _build_rule_spec(
                form_spec, kind, intent,
                available_encounter_types, available_programs,
            )
            result = generator.generate(rule_spec)
            ok, val_warnings = validate_rule(result, rule_spec)

            for warning in (*result.warnings, *val_warnings):
                warnings.append(
                    f"rules.{kind_value}.{form_spec.name}: {warning}"
                )

            if not (ok and result.js):
                continue

            target = forms_by_name.get(form_spec.name)
            if target is None:
                warnings.append(
                    f"rules.{kind_value}.{form_spec.name}: "
                    f"form JSON not found in state; cannot write"
                )
                continue
            target["content"][kind_value] = result.js
            written += 1

    log.info(
        "[%s] Rules: %d form(s) had intents, %d rule(s) written.",
        state["org_name"], len(forms_with_intents), written,
    )
    return {"forms_json": forms_json, "parse_warnings": warnings}


def _build_rule_spec(
    form_spec: Any,
    kind: RuleKind,
    intent: str,
    available_encounter_types: list[str],
    available_programs: list[str],
) -> RuleSpec:
    """Compose a RuleSpec from a FormSpec and the bundle's vocabulary."""
    available_concepts = sorted({
        field.name
        for section in (form_spec.sections or [])
        for field in (section.fields or [])
        if field.name
    })
    return RuleSpec(
        rule_kind=kind,
        intent=intent,
        form_name=form_spec.name,
        form_type=form_spec.formType,
        subject_type=form_spec.subjectType,
        program=form_spec.program,
        encounter_type=form_spec.encounterType,
        available_concepts=available_concepts,
        available_encounter_types=available_encounter_types,
        available_programs=available_programs,
    )


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


# ── Edit-from-spec branch nodes (BUNDLE_EDIT_FROM_SPEC_SDD) ───────────────────


def diff_against_bundle(state: BundleState) -> dict:
    """Diff the freshly-generated desired bundle (in state) against the
    existing bundle on disk. Emits `diff_ops`.

    No I/O beyond loading the existing bundle. Computes ops in memory.
    """
    bundle_path = state.get("bundle_path", "")
    errors = list(state.get("errors", []))

    if not bundle_path or not os.path.exists(bundle_path):
        errors.append(f"edit_from_spec: bundle not found at {bundle_path!r}")
        return {"diff_ops": [], "errors": errors}

    desired_bundle = {
        "forms": state.get("forms_json", []),
        "concepts": state.get("concepts_json", []),
    }
    current_bundle = load_bundle_snapshot(bundle_path)

    ops = diff(desired_bundle, current_bundle)

    log.info(
        "[%s] diff vs %s: %d op(s) (%s)",
        state["org_name"], os.path.basename(bundle_path), len(ops),
        ", ".join(f"{kind}={n}" for kind, n in _count_by_kind(ops).items()) or "none",
    )
    return {"diff_ops": ops, "errors": errors}


def apply_diff_edits(state: BundleState) -> dict:
    """Apply the diff_ops to the existing bundle in place.

    `apply_field_edits` handles loading, applying, integrity check,
    write-back, and atomic re-zip. The node is intentionally thin.
    """
    bundle_path = state["bundle_path"]
    ops = state.get("diff_ops", [])
    errors = list(state.get("errors", []))

    if not ops:
        log.info("[%s] edit_from_spec: no ops to apply (bundle already in sync)",
                 state["org_name"])
        return {"edit_result": {"bundle_path": bundle_path, "applied": [],
                                "rejected": [], "warnings": [],
                                "forms_modified": []}}

    result = apply_field_edits(bundle_path, ops)

    if result.rejected:
        errors.append(
            f"edit_from_spec: {len(result.rejected)} op(s) rejected — bundle may be partially updated"
        )

    log.info(
        "[%s] edit_from_spec applied: +%d added, %d voided, %d renamed, %d reinstated",
        state["org_name"], result.form_elements_added, result.form_elements_voided,
        result.form_elements_renamed, result.form_elements_reinstated,
    )
    return {"edit_result": result.model_dump(), "errors": errors,
            "zip_path": bundle_path}


def _count_by_kind(ops: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for op in ops:
        k = op.get("kind", "?")
        counts[k] = counts.get(k, 0) + 1
    return counts

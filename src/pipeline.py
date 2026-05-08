"""
LangGraph pipeline that wires the bundle generation workflow:

  discover_files → parse_documents → enrich_with_llm → generate_entities
      → generate_forms → generate_form_mappings → package_zip

Each node is thin — heavy lifting lives in `generators.py`, `parser.py`, and
`enricher.py`.

`enrich_with_llm` calls Claude (Haiku) to refine each form's parsed FormSpec
when the parser had to guess (missing data type, mixed delimiters, long names,
etc.). Auto-apply changes are baked in immediately; pending changes that need
user confirmation pause the graph via `interrupt()` — the caller resumes with
`Command(resume=resolutions)`.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys
import zipfile
from typing import Any, TypedDict

import pandas as pd
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

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
    # LLM enrichment (SDD §8)
    user_instructions: str | None           # passed through from chat tool
    pending_changes: list[dict]             # surfaced via interrupt(); empty after resume
    applied_changes: list[dict]             # for the run summary + audit log
    enrich_warnings: list[str]
    # Generated JSON
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


def enrich_with_llm(state: BundleState) -> BundleState:
    """Call Claude once to refine each form; store any pending changes in state.

    The actual confirmation interrupt + apply lives in `apply_user_decisions`
    so that resuming after the user confirms only re-runs the apply node — not
    the LLM call. Re-running the LLM produced fresh non-deterministic
    change_ids that didn't match the resolutions the user had answered against.
    """
    spec = state["entity_spec"]
    if spec is None or not spec.forms:
        return {**state, "pending_changes": [], "applied_changes": [], "enrich_warnings": []}

    from enricher import enrich_forms  # noqa: PLC0415
    from llm_helper import LLMHelper  # noqa: PLC0415

    helper = LLMHelper()
    if not helper.is_available():
        log.info("[%s] ANTHROPIC_API_KEY not set; skipping LLM enrichment.", state["org_name"])
        return {
            **state,
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
        **state,
        "entity_spec": spec,
        "pending_changes": [c.model_dump() for c in pending],
        "applied_changes": [c.model_dump() for c in applied],
        "enrich_warnings": warnings,
    }


def apply_user_decisions(state: BundleState) -> BundleState:
    """If there are pending changes, ask the user via interrupt and apply.

    On resume, only this node re-executes — `enrich_with_llm` already ran
    on the first pass and its pending list is in state. The change_ids the
    user confirmed therefore still match.
    """
    pending_dicts = state.get("pending_changes") or []
    if not pending_dicts:
        return state

    from models import Change  # noqa: PLC0415

    resolutions = interrupt({
        "kind": "confirm_changes",
        "org": state["org_name"],
        "changes": pending_dicts,
    })

    pending = [Change(**d) for d in pending_dicts]
    spec = state["entity_spec"]
    confirmed_applied, post_warnings = _apply_resolutions(
        spec.forms, pending, resolutions or {},
    )

    applied_dicts = list(state.get("applied_changes") or [])
    applied_dicts.extend(c.model_dump() for c in confirmed_applied)
    warnings = list(state.get("enrich_warnings") or [])
    warnings.extend(post_warnings)

    return {
        **state,
        "entity_spec": spec,
        "pending_changes": [],
        "applied_changes": applied_dicts,
        "enrich_warnings": warnings,
    }


def _apply_resolutions(
    forms: list,
    pending: list,
    resolutions: dict[str, str],
) -> tuple[list, list[str]]:
    """Apply user-confirmed Changes (from `interrupt()`) to the FormSpec list.

    Each resolution is one of:
        "yes"               — apply Claude's `after` payload as-is
        "no"                — skip this change
        "edit:<value>"      — apply with the user's value substituted into `after`
                              (format depends on kind; see _parse_edit)

    Mutates the FormSpec list in place. Returns (applied_changes, warnings).
    """
    from models import Change  # noqa: PLC0415

    applied: list[Change] = []
    warnings: list[str] = []
    by_id = {c.change_id: c for c in pending}
    forms_by_name = {f.name: f for f in forms}

    for change_id, decision in resolutions.items():
        change = by_id.get(change_id)
        if change is None:
            warnings.append(f"Resolution for unknown change_id '{change_id}'")
            continue
        if decision == "no" or decision is False:
            continue
        target_form = forms_by_name.get(change.form)
        if target_form is None:
            warnings.append(f"Change {change_id} targets unknown form '{change.form}'")
            continue

        # Resolve `after` — either Claude's proposal or the user's edit override.
        after = dict(change.after or {})
        if isinstance(decision, str) and decision.startswith("edit:"):
            after = _parse_edit(decision[len("edit:"):], change.kind, after, warnings, change_id)

        try:
            ok = _apply_one(target_form, change, after, warnings)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Apply {change_id} ({change.kind}) raised: {exc}")
            continue
        if ok:
            applied.append(change.model_copy(update={"after": after}))
            log.info("[apply] %s renamed to %r", change_id, after.get("name"))
    return applied, warnings


def _all_fields_in(form) -> list:
    """Yield every FieldSpec in a form, walking sections in order."""
    out: list = []
    for section in form.sections or []:
        out.extend(section.fields)
    return out


def _find_field(form, name: str, section: str | None = None):
    """Locate a FieldSpec by name; if `section` is given, prefer that section's copy.

    Used by duplicate_field to disambiguate two same-named occurrences.
    """
    name_l = name.strip().lower()
    if section:
        section_l = section.strip().lower()
        for sec in form.sections or []:
            if sec.name.strip().lower() == section_l:
                for f in sec.fields:
                    if f.name.strip().lower() == name_l:
                        return f
        # Section hint didn't match; fall through to any match.
    for f in _all_fields_in(form):
        if f.name.strip().lower() == name_l:
            return f
    return None


def _apply_one(form, change, after: dict, warnings: list[str]) -> bool:
    """Mutate the form per `change.kind`. Returns True if mutation happened.

    Only `long_name` and `duplicate_field` are currently handled — those are
    the only kinds the LLM is allowed to emit (see `llm_helper._SYSTEM_PROMPT`
    and the `ChangeKind` Literal in `models.py`).
    """
    kind = change.kind
    field_name = change.field
    new_name = after.get("name")
    if not new_name:
        return False

    if kind == "long_name":
        f = _find_field(form, field_name)
        if f is None:
            warnings.append(
                f"long_name: field '{field_name[:80]}…' not found in '{form.name}'"
            )
            return False
        f.name = new_name
        return True

    if kind == "duplicate_field":
        # `before.section` disambiguates which occurrence to rename.
        before = change.before or {}
        section = before.get("section") if isinstance(before, dict) else None
        f = _find_field(form, field_name, section=section)
        if f is None:
            warnings.append(
                f"duplicate_field: '{field_name}' not found in section '{section}' of '{form.name}'"
            )
            return False
        f.name = new_name
        return True

    warnings.append(f"Unknown change kind '{kind}' for {change.change_id}")
    return False


def _parse_edit(raw: str, kind: str, fallback: dict, warnings: list[str], change_id: str) -> dict:
    """Translate a user 'edit:<new name>' string into an `after` dict.

    Both supported kinds (long_name, duplicate_field) take the raw value as
    the new field name.
    """
    out = dict(fallback)
    if kind in ("long_name", "duplicate_field"):
        out["name"] = raw.strip()
    else:
        warnings.append(f"edit not supported for kind={kind} on {change_id}; using LLM proposal")
    return out


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


def build_graph(checkpointer=None) -> Any:
    graph = StateGraph(BundleState)

    graph.add_node("discover_files", discover_files)
    graph.add_node("parse_documents", parse_documents)
    graph.add_node("enrich_with_llm", enrich_with_llm)
    graph.add_node("apply_user_decisions", apply_user_decisions)
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
        {"continue": "enrich_with_llm", "abort": END},
    )
    graph.add_edge("enrich_with_llm", "apply_user_decisions")
    graph.add_edge("apply_user_decisions", "generate_entities")
    graph.add_edge("generate_entities", "generate_forms")
    graph.add_edge("generate_forms", "generate_form_mappings")
    graph.add_edge("generate_form_mappings", "package_zip")
    graph.add_edge("package_zip", END)

    # A checkpointer is required for `interrupt()` to be resumable.
    return graph.compile(checkpointer=checkpointer or MemorySaver())


# ── Public entry point ────────────────────────────────────────────────────────


def initial_state(org_name: str, input_dir: str, output_dir: str,
                   user_instructions: str | None = None) -> BundleState:
    return {
        "org_name": org_name,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "file_paths": [],
        "entity_spec": None,
        "parse_warnings": [],
        "user_instructions": user_instructions,
        "pending_changes": [],
        "applied_changes": [],
        "enrich_warnings": [],
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


def run(org_name: str, input_dir: str, output_dir: str,
         user_instructions: str | None = None,
         thread_id: str | None = None) -> BundleState:
    """Run the pipeline end-to-end. If it interrupts, the partial state is returned.

    The chat host typically uses `build_graph()` directly to stream events and
    handle interrupts; `run()` is a convenience for non-interactive callers
    (no API key, clean inputs, no confirmation needed).
    """
    pipeline = build_graph()
    config = {"configurable": {"thread_id": thread_id or f"thread-{org_name}"}}
    return pipeline.invoke(
        initial_state(org_name, input_dir, output_dir, user_instructions),
        config=config,
    )

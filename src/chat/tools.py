"""
Agent tools exposed to the chat ReAct agent.

Each `@tool` returns a JSON-serialisable dict that the agent can read into
its messages. Tool I/O is the only bridge between the chat agent and the
bundle pipeline (or the bundle editor).
"""

from __future__ import annotations

import os
import time

from langchain_core.tools import tool
from langgraph.types import Command

from config import settings
from domain.bundle_editor import (
    apply_field_edits,
    list_bundle_fields as _list_bundle_fields,
)
from pipeline import build_graph, initial_state

# ── Pipeline graph (compiled once, shared by every tool call) ────────────────
#
# The graph's checkpointer keeps per-thread state, so a paused run can be
# resumed later by passing the same `thread_id` back via `resume_bundle`.

_pipeline_graph = build_graph()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _summarize(state: dict) -> dict:
    """Build the success-summary dict the chat agent reports."""
    cancel_count = sum(
        1 for f in state.get("mapping_specs", [])
        if "Cancellation" in f["name"]
    )
    main_forms = len(state.get("forms_json", [])) - cancel_count
    return {
        "status": "done",
        "org": state.get("org_name", ""),
        "subject_types": len(state.get("subject_types_json", [])),
        "programs": len(state.get("programs_json", [])),
        "encounter_types": len(state.get("encounter_types_json", [])),
        "main_forms": main_forms,
        "cancellation_forms": cancel_count,
        "concepts": len(state.get("concepts_json", [])),
        "form_mappings": len(state.get("form_mappings_json", [])),
        "zip_path": state.get("zip_path", ""),
        "applied_changes": state.get("applied_changes", []),
        "enrich_warnings": state.get("enrich_warnings", []),
        "parse_warnings": state.get("parse_warnings", []),
        "errors": state.get("errors", []),
    }


def _run_with_interrupt_handling(input_or_command, config: dict) -> dict:
    """Invoke the pipeline; if it interrupts, return the pending payload.

    The pipeline graph uses LangGraph's `interrupt()` in
    `apply_user_decisions`. On invoke that returns a dict whose
    `__interrupt__` key carries the interrupt info, the caller (the chat
    agent) presents it to the user. Once the user responds, the agent
    calls `resume_bundle` with the same thread_id and the pipeline
    resumes from the interrupt point.
    """
    result = _pipeline_graph.invoke(input_or_command, config=config)
    interrupts = result.get("__interrupt__")
    if interrupts:
        first = interrupts[0]
        payload = getattr(first, "value", None)
        if payload is None and isinstance(first, dict):
            payload = first.get("value")
        return {
            "status": "needs_confirmation",
            "thread_id": config["configurable"]["thread_id"],
            "payload": payload or {},
        }
    return _summarize(result)


# ── Tools ────────────────────────────────────────────────────────────────────


@tool
def generate_bundle(org: str, user_instructions: str | None = None) -> dict:
    """Start an Avni bundle generation for a specific org.

    Reads all .xlsx files in resources/input/<org>/ and produces
    resources/output/<org>/<Org>.zip. The deterministic parser runs first,
    then Claude (Haiku) enriches each form's spec — fixing option splits,
    inferring missing data types, predicting min/max bounds, etc.

    If any refinement requires user confirmation (long-name shortening,
    duplicate-field disambiguation, min/max bounds, user-driven add/remove),
    the pipeline pauses and this tool returns a `needs_confirmation` payload.
    The agent should present the proposed changes to the user, gather their
    answers as a `{change_id: "yes"|"no"|"edit:<value>"}` dict, then call
    `resume_bundle(thread_id, resolutions)` to finish the run.

    Args:
        org: Org subfolder name, case-insensitive (e.g. 'srijan').
        user_instructions: Optional natural-language instruction passed to
            the LLM enrichment step (e.g. "also add a Sponsor field to
            Pregnancy Enrolment").
    """
    org = org.strip().lower()
    input_dir = os.path.join(settings.input_root, org)
    output_dir = os.path.join(settings.output_root, org)
    if not os.path.isdir(input_dir):
        return {
            "status": "error",
            "error": f"Input dir not found: {input_dir}",
        }

    thread_id = f"bundle-{org}-{int(time.time())}"
    config = {"configurable": {"thread_id": thread_id}}
    initial = initial_state(org, input_dir, output_dir, user_instructions)
    return _run_with_interrupt_handling(initial, config)


@tool
def resume_bundle(thread_id: str, resolutions: dict[str, str]) -> dict:
    """Resume a paused bundle run after the user confirms pending changes.

    Args:
        thread_id: The id returned in the prior `generate_bundle` /
            `resume_bundle` `needs_confirmation` response.
        resolutions: A dict mapping each `change_id` to one of:
            "yes"               — apply Claude's proposed `after`
            "no"                — skip this change
            "edit:<new_value>"  — apply with a user-provided override
    """
    config = {"configurable": {"thread_id": thread_id}}
    return _run_with_interrupt_handling(Command(resume=resolutions), config)


@tool
def list_bundle_fields(bundle_path: str) -> dict:
    """Inspect an existing bundle (ZIP or unpacked directory) and return a
    compact summary of every form, section, and field.

    Use this BEFORE calling `edit_bundle_fields` so your edit operations
    reference real names (case and punctuation matter — match is exact).

    Args:
        bundle_path: Path to a bundle ZIP file or an unpacked bundle directory
            (e.g. 'resources/output/ekam/Ekam.zip' or 'resources/output/ekam').
    """
    try:
        return {"status": "done", "result": _list_bundle_fields(bundle_path)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}


@tool
def edit_bundle_fields(bundle_path: str, operations: list[dict]) -> dict:
    """Add, rename, or remove fields in an already-generated bundle ZIP.

    Each operation has shape:
      {
        "op_id":  "<your-id>",
        "kind":   "field.add" | "field.rename" | "field.remove",
        "target": { "form": "...", "section": "...", "field": "..."? },
        "payload": { ... }     # kind-specific
      }

    field.add payload: name (required), dataType (default 'Text'),
      mandatory?, options? (required for Coded), selectionType?, unit?, min?,
      max?.
    field.rename payload: new_name (required).
    field.remove payload: empty.

    Matching is case-folded + whitespace-stripped EXACT (no fuzzy). On a
    mismatch the op is rejected with kind 'not_found' / 'ambiguous_target' /
    'duplicate_name' / 'schema'.

    Removes set `voided: true` on the form element so the server soft-deletes
    the corresponding record on re-upload. Re-adding the same field name later
    reinstates the original element (UUID preserved).

    Args:
        bundle_path: Bundle ZIP or unpacked directory.
        operations: List of edit operation dicts (see above).
    """
    try:
        result = apply_field_edits(bundle_path, operations)
        return {"status": "done", "result": result.model_dump()}
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}


TOOLS = [generate_bundle, resume_bundle, list_bundle_fields, edit_bundle_fields]

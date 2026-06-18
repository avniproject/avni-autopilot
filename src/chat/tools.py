"""
Agent tools exposed to the chat ReAct agent.

Each `@tool` returns a JSON-serialisable dict that the agent can read into
its messages. Tool I/O is the only bridge between the chat agent and the
bundle pipeline (or the bundle editor).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Literal

from langchain_core.tools import tool
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

log = logging.getLogger(__name__)

from config import settings
from domain.bundle_editor import (
    apply_field_edits,
    list_bundle_fields as _list_bundle_fields,
    load_form_rule_context,
    write_form_rule,
)
from domain.rules.generator import RuleGenerator
from domain.rules.rule_spec import RuleKind, RuleSpec
from domain.rules.validator import validate_and_decide as _validate_and_decide
from pipeline import build_graph, initial_state

# ── Pipeline graph (compiled once, shared by every tool call) ────────────────
#
# The graph's checkpointer keeps per-thread state, so a paused run can be
# resumed later by passing the same `thread_id` back via `resume_bundle`.

_pipeline_graph = build_graph()
_rule_generator = RuleGenerator()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _summarize(state: dict) -> dict:
    """Build the success-summary dict the chat agent reports."""
    cancel_count = sum(
        1 for f in state.get("mapping_specs", [])
        if "Cancellation" in f["name"]
    )
    main_forms = len(state.get("forms_json", [])) - cancel_count
    summary = {
        "status": "done",
        "mode": state.get("mode", "generate"),
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
    # edit_from_spec adds a diff summary
    if state.get("mode") == "edit_from_spec":
        summary["edit_result"] = state.get("edit_result", {})
    return summary


def _run_with_interrupt_handling(input_or_command, config: dict) -> dict:
    """Invoke the pipeline; if it pauses on interrupt(), return needs_confirmation.

    The graph's checkpointer (SqliteSaver, see `pipeline.graph._default_checkpointer`)
    persists the suspended state across the GraphInterrupt unwind, so
    `get_state(config).next` is the authoritative paused signal here.
    Resume happens via `resume_bundle` → `Command(resume=...)`.
    """
    thread_id = config["configurable"]["thread_id"]
    result: dict | None = None
    caught: str = "none"
    try:
        result = _pipeline_graph.invoke(input_or_command, config=config)
        log.info(
            "invoke returned thread_id=%s type=%s keys=%s",
            thread_id,
            type(result).__name__,
            list(result.keys())[:12] if isinstance(result, dict) else None,
        )
    except GraphInterrupt as exc:
        caught = "GraphInterrupt"
        log.info("invoke raised GraphInterrupt thread_id=%s args_len=%d", thread_id, len(exc.args or ()))
    except BaseException as exc:  # noqa: BLE001 — diagnostic: see what's really being raised
        caught = type(exc).__name__
        log.info("invoke raised %s thread_id=%s msg=%s", caught, thread_id, str(exc)[:200])
        raise

    snapshot = _pipeline_graph.get_state(config)
    is_paused = bool(snapshot and snapshot.next)
    log.info(
        "post-invoke thread_id=%s caught=%s is_paused=%s tasks=%d snapshot_values_keys=%s",
        thread_id, caught, is_paused, len(getattr(snapshot, "tasks", []) or []),
        list((getattr(snapshot, "values", {}) or {}).keys())[:12],
    )

    if is_paused:
        payload: dict = {}
        for task in (snapshot.tasks or []):
            for itr in (getattr(task, "interrupts", None) or []):
                value = getattr(itr, "value", None)
                if value is None and isinstance(itr, dict):
                    value = itr.get("value")
                if value is not None:
                    payload = value if isinstance(value, dict) else {"value": value}
                    break
            if payload:
                break
        return {
            "status": "needs_confirmation",
            "thread_id": thread_id,
            "payload": payload,
        }

    if result is not None:
        return _summarize(result)

    return {
        "status": "error",
        "code": "E_INTERRUPT_NOT_PERSISTED",
        "error": (
            f"Build failed (thread_id={thread_id!r}). Tell the user the "
            "build failed and ask whether they want to retry — do NOT "
            "claim you can resume."
        ),
    }


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
def edit_bundle_from_spec(org: str, user_instructions: str | None = None) -> dict:
    """Update an already-generated bundle from the current source .xlsx (the spec).

    Reads resources/input/<org>/*.xlsx, re-runs the deterministic parser and
    the LLM enrichment pass (may pause for user confirmation of long-name
    shortenings / duplicate-field disambiguations — same as generate_bundle),
    then diffs the regenerated desired bundle against the existing
    resources/output/<org>/<Org>.zip and applies the resulting field-level
    edits (add / remove / reorder) atomically.

    Field UUIDs are preserved across the edit. Removed fields are voided so
    Avni soft-deletes the records on re-upload; surviving fields keep their
    UUIDs and observation history.

    If LLM enrichment proposes any changes, the pipeline pauses and this
    tool returns `needs_confirmation`. Resume with the existing
    `resume_bundle(thread_id, resolutions)` — same as for generate_bundle.

    Args:
        org: Org subfolder name, case-insensitive (e.g. 'srijan').
        user_instructions: Optional natural-language instruction passed to
            the LLM enrichment step.
    """
    org = org.strip().lower()
    input_dir = os.path.join(settings.input_root, org)
    bundle_path = os.path.join(settings.output_root, org, f"{org.capitalize()}.zip")
    if not os.path.isdir(input_dir):
        return {"status": "error", "error": f"Input dir not found: {input_dir}"}
    if not os.path.exists(bundle_path):
        return {
            "status": "error",
            "error": (f"Bundle not found: {bundle_path}. "
                      f"Use generate_bundle('{org}') first to create one."),
        }

    output_dir = os.path.join(settings.output_root, org)
    thread_id = f"bundle-{org}-{int(time.time())}"
    config = {"configurable": {"thread_id": thread_id}}
    initial = initial_state(
        org, input_dir, output_dir, user_instructions,
        mode="edit_from_spec", bundle_path=bundle_path,
    )
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
    snapshot = _pipeline_graph.get_state(config)
    has_checkpoint = bool(snapshot and (snapshot.next or snapshot.values))
    log.info(
        "resume_bundle thread_id=%s has_checkpoint=%s n_resolutions=%d",
        thread_id, has_checkpoint, len(resolutions or {}),
    )
    if not has_checkpoint:
        return {
            "status": "error",
            "code": "E_NO_CHECKPOINT",
            "error": (
                f"The paused bundle run for thread_id={thread_id!r} is gone "
                "(likely a service restart). The pending changes the user "
                "decided on cannot be applied — the run that produced them "
                "no longer exists. Tell the user the previous run was lost "
                "and ask whether they want to start a NEW run. Do NOT call "
                "generate_bundle without the user's explicit confirmation, "
                "and do NOT claim you can carry their previous decisions "
                "forward — you cannot."
            ),
        }
    return _run_with_interrupt_handling(Command(resume=resolutions), config)


@tool
def list_bundle_fields(bundle_path: str) -> dict:
    """Inspect an existing bundle (ZIP or unpacked directory) and return a
    compact summary of every form, section, and field. Coded fields include
    their `answers` list.

    Use this BEFORE any tool that takes bundle names as arguments
    (`edit_bundle_fields`, `set_visit_schedule_rule`, …) so your call uses
    real names from the bundle. The user often types informally — "supporting
    family", "marital", "baseline form" — but the bundle stores exact
    phrasings — "can support my family", "Marital status", "Baseline for
    Women". Match is EXACT downstream (case + punctuation), so ground your
    args against this tool's output first, then echo the resolved names back
    to the user when confirming what you're about to do.

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


@tool
def set_form_rule(
    bundle_path: str,
    form_name: str,
    rule_kind: Literal[
        "visitScheduleRule",
        "validationRule",
        "editFormRule",
        "decisionRule",
    ],
    intent: str,
) -> dict:
    """Generate JS for a form-level rule and write it into the bundle.

    `rule_kind` picks which Avni rule field on the form JSON is updated:

      - "visitScheduleRule"  → when the next visit should be scheduled
      - "validationRule"     → block-save messages when the form has bad data
      - "editFormRule"       → who/when the form can be edited (eligibility)
      - "decisionRule"       → values to write into concepts at submit time

    Loads the bundle, builds a RuleSpec from the form's context (formType,
    subject/program/encounter associations, available concepts, encounter
    types, and coded-concept answers across the target + registration +
    enrolment forms), calls the rule generator, validates the produced JS,
    and on success writes it into the form JSON before re-zipping atomically.

    Before calling this, ground the user's intent against the bundle. The
    user usually phrases informally — short form names, paraphrased field
    names, casual answer wording. Call `list_bundle_fields` first to discover
    exact form names, field/concept names, encounter type names, and
    coded-answer strings. Echo the resolved names back to the user when
    confirming so they catch any mis-mapping before the rule is written.

    On validation failure (parse error, off-bundle concept, encounter type,
    or answer) nothing is written and the warnings are returned for the user
    to act on.

    Args:
        bundle_path: Path to a bundle ZIP file or an unpacked bundle directory.
        form_name: Exact name of the form (matches `form.name` in the bundle).
        rule_kind: One of "visitScheduleRule", "validationRule",
            "editFormRule", "decisionRule".
        intent: Natural-language description of what the rule should DO, not
            how. e.g. "block saving when age < 18 or > 60" for a validation
            rule, "only the user who created the form can edit it" for an
            edit-form rule.
    """
    try:
        kind = RuleKind(rule_kind)
    except ValueError:
        return {
            "status": "error",
            "error": (
                f"unknown rule_kind {rule_kind!r}; expected one of: "
                f"{[k.value for k in RuleKind]}"
            ),
        }

    try:
        context = load_form_rule_context(bundle_path, form_name)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}

    if context is None:
        return {
            "status": "error",
            "error": f"form not found in bundle: {form_name!r}",
        }

    spec = RuleSpec(
        rule_kind=kind,
        intent=intent,
        form_name=form_name,
        form_type=context["form_type"],
        subject_type=context["subject_type"],
        program=context["program"],
        encounter_type=context["encounter_type"],
        available_concepts=context["available_concepts"],
        available_encounter_types=context["available_encounter_types"],
        available_programs=context["available_programs"],
        concept_answers=context.get("concept_answers", {}),
    )

    result = _rule_generator.generate(spec)
    ok, warnings = _validate_and_decide(result, spec)

    if not ok:
        return {
            "status": "rejected",
            "form_name": form_name,
            "rule_kind": kind.value,
            "confidence": result.confidence,
            "warnings": warnings,
        }

    if not write_form_rule(bundle_path, form_name, kind.value, result.js):
        return {
            "status": "error",
            "error": f"form was found earlier but write_form_rule could not relocate it: {form_name!r}",
        }

    return {
        "status": "done",
        "form_name": form_name,
        "rule_kind": kind.value,
        "confidence": result.confidence,
        "used_helpers": result.used_helpers,
        "referenced_concepts": result.referenced_concepts,
        "referenced_encounter_types": result.referenced_encounter_types,
        "warnings": warnings,
        "js_preview": result.js if len(result.js) <= 400 else result.js[:400] + "...",
    }


TOOLS = [
    generate_bundle,
    edit_bundle_from_spec,
    resume_bundle,
    list_bundle_fields,
    edit_bundle_fields,
    set_form_rule,
]

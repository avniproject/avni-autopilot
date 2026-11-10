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
from pathlib import Path
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import Command

log = logging.getLogger(__name__)

from config import settings
from domain.bundle_editor import (
    AmbiguousFormError,
    BundleOrigin,
    apply_field_edits,
    classify_bundle,
    list_bundle_fields as _list_bundle_fields,
    load_form_rule_context,
)
from domain.docs.knowledge_base import DocsKnowledgeBase
from domain.generators import unlinked_form_names
from domain.rules.generator import RuleGenerator
from domain.rules.rule_spec import RuleKind, RuleSpec
from domain.rules.validator import validate_and_decide as _validate_and_decide
from domain.spreadsheets import find_spreadsheets
from pipeline import initial_state
from pipeline.graph import build_phase1_graph, build_phase2_graph

# ── Pipeline graphs (compiled once, shared by every tool call) ───────────────
#
# Two-phase split avoids LangGraph 1.x's broken HITL machinery (dynamic
# `interrupt()` raised without persisting; `interrupt_before` hung inside
# ainvoke). Phase 1 parses + enriches and ends; if `pending_changes` is
# non-empty the tool stashes the state in `_saved_states` and returns
# `needs_confirmation`. On resume, the saved state is reloaded,
# `user_resolutions` is set, and Phase 2 finishes the build.
#
# In-process dict — state is lost on autopilot restart. Same v1 limitation
# documented in `AVNI_WEBAPP_INTEGRATION_SDD §8.2`. Cross-restart durability
# can be added later by pickling the dict to disk under autopilot_session_dir.

_phase1_graph = build_phase1_graph()
_phase2_graph = build_phase2_graph()
_rule_generator = RuleGenerator()
_docs_kb = DocsKnowledgeBase()
_saved_states: dict[str, dict] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────


_NO_SPREADSHEET_ERROR = (
    "No spreadsheet has been uploaded. Ask the user to upload their "
    "scoping / modelling .xlsx files first."
)


def _session_of(config: RunnableConfig | None):
    """The webapp `ChatSession` for this tool call, or None in the REPL."""
    return (config or {}).get("configurable", {}).get("session")


_NO_BUNDLE_ERROR = (
    "No bundle available — the organisation's current setup is not loaded "
    "in this chat and none was built here."
)


def _resolve_bundle_path(bundle_path: str | None, config: RunnableConfig | None) -> str | None:
    """The webapp session's bundle, or the explicit `bundle_path` in the REPL.

    A webapp session only ever reads or edits its own bundle; a path the
    agent passes there is ignored.
    """
    session = _session_of(config)
    if session is not None:
        return str(session.bundle_path) if session.bundle_path else None
    return bundle_path or None


_SETUP_LOADING_ERROR = (
    "The organisation's current setup is still loading. Ask the user to try "
    "again in a moment."
)

_SEPARATE_BUILD_NOT_CONFIRMED_ERROR = (
    "Not built. The organisation's current setup could not be loaded, or its "
    "forms were set up outside Autopilot, so a build makes a separate new set "
    "of forms that uploading would add next to the existing ones (possible "
    "duplicate forms and questions). Tell the user this in plain words; call "
    "again with confirmed_separate_build=true only after they say to go ahead."
)

_UNUPDATABLE_BUNDLE_ERRORS = {
    BundleOrigin.NO_FORMS: (
        "The organisation's bundle has no forms yet, so there is nothing to "
        "update. Build the app from the spreadsheets instead."
    ),
    BundleOrigin.OTHER: (
        "The organisation's forms were set up outside Autopilot, so they "
        "cannot be updated from spreadsheets. Field changes can still be made "
        "by name; a build from the spreadsheets would make a separate new set "
        "of forms."
    ),
}


_UNREADABLE_BUNDLE_ERROR = (
    "The bundle could not be read; it is not a bundle ZIP file or folder."
)


def _has_spreadsheets(input_dir: str) -> bool:
    return bool(find_spreadsheets(input_dir))


def _unreadable_bundle(exc: FileNotFoundError) -> dict:
    """Error result for a bundle that cannot be opened; the path goes to the log only."""
    log.warning(f"unreadable bundle: {exc}")
    return {"status": "error", "error": _UNREADABLE_BUNDLE_ERROR}


def _report_bundle(result: dict, config: RunnableConfig | None) -> dict:
    """Keep server paths out of what the agent reads in a webapp session.

    The session takes the changed bundle's path and the result carries only
    `bundle_updated`; the chat service shows the bundle card from it. In the
    REPL the result keeps `zip_path` for the person at the terminal.
    """
    session = _session_of(config)
    if session is None:
        return result
    zip_path = result.pop("zip_path", "")
    if zip_path:
        session.bundle_path = Path(zip_path)
        result["bundle_updated"] = True
    return result



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
    if state.get("mode") == "edit_from_spec":
        _shape_edit_from_spec_summary(summary, state)
    elif summary["zip_path"]:
        _shape_generate_summary(summary, state)
    return summary


_NO_FORM_LINKED_ERROR = (
    "None of the forms could be linked to a subject type, program or visit "
    "type, so the app would have no screens to open them and the bundle is "
    "not offered. Try building again; if it fails again, ask the Avni team."
)


def _shape_generate_summary(summary: dict, state: dict) -> None:
    """Name forms the build could not link, and withhold a bundle that links none.

    Linking forms is done by the model alone; when that call is skipped or
    fails every form is left unlinked, and such a bundle must not be shown
    as ready.
    """
    forms = state.get("forms_json", [])
    unlinked = unlinked_form_names(forms, state.get("form_mappings_json", []))
    if not unlinked:
        return
    summary["unlinked_forms"] = unlinked
    if len(unlinked) == len(forms):
        log.warning(f"build withheld: none of {len(forms)} form(s) linked")
        summary["status"] = "error"
        summary["error"] = _NO_FORM_LINKED_ERROR
        summary["zip_path"] = ""
        for key in _SPEC_COUNT_KEYS:
            summary.pop(key, None)
        return
    summary["errors"] = [
        *summary["errors"],
        "These forms could not be linked to a subject type, program or visit "
        "type, so no screen in the app opens them: " + ", ".join(unlinked) + ".",
    ]


# Counts of the bundle built from the spreadsheets. An update applies only
# the diff, and a withheld build offers no bundle, so neither reports them.
_SPEC_COUNT_KEYS = (
    "subject_types", "programs", "encounter_types", "main_forms",
    "cancellation_forms", "concepts", "form_mappings",
)


def _shape_edit_from_spec_summary(summary: dict, state: dict) -> None:
    """Report what an update applied, what it could not, and whether it changed anything.

    `zip_path` is set only when the bundle changed, so the bundle card is
    shown only then.
    """
    for key in _SPEC_COUNT_KEYS:
        summary.pop(key, None)
    summary["edit_result"] = state.get("edit_result", {})
    unmatched = state.get("unmatched_forms") or []
    summary["unmatched_forms"] = unmatched
    errors = list(summary["errors"])
    if unmatched:
        errors.append(
            "These spreadsheet forms are not in the current bundle, and an "
            "update cannot add new forms, so they were left out: "
            + ", ".join(unmatched) + "."
        )
    summary["errors"] = errors
    if summary["zip_path"]:
        return
    if errors:
        summary["status"] = "error"
        summary["error"] = "Nothing in the bundle was changed. " + " ".join(errors)
    else:
        summary["no_changes"] = True
        summary["message"] = "The bundle already matches the spreadsheets; nothing was changed."


async def _run_phase1_then_pause_or_finish(initial: dict, thread_id: str) -> dict:
    """Run phase 1; if `pending_changes` is non-empty, save state and return
    needs_confirmation. Otherwise run phase 2 to completion and summarize.

    `ainvoke` (not `invoke`) so the call stays in the chat agent's event loop.
    With sync `invoke` LangChain wraps sync tools in a thread executor, where
    LangGraph 1.x's internal awaits would have no event loop.
    """
    phase1_state = await _phase1_graph.ainvoke(initial)
    if phase1_state.get("entity_spec") is None:
        errors = phase1_state.get("errors") or ["The spreadsheets could not be read."]
        log.info(f"phase1-aborted thread_id={thread_id} errors={len(errors)}")
        return {"status": "error", "error": " ".join(errors)}
    pending = phase1_state.get("pending_changes") or []
    log.info(
        "phase1-done thread_id=%s pending=%d errors=%d",
        thread_id, len(pending), len(phase1_state.get("errors") or []),
    )

    if pending:
        _saved_states[thread_id] = phase1_state
        return {
            "status": "needs_confirmation",
            "thread_id": thread_id,
            "payload": {
                "kind": "confirm_changes",
                "org": phase1_state.get("org_name", ""),
                "changes": pending,
            },
        }

    # No HITL required — fast-path straight to phase 2.
    return await _run_phase2(phase1_state, thread_id)


async def _run_phase2(phase1_state: dict, thread_id: str) -> dict:
    log.info(
        "phase2-start thread_id=%s resolutions=%d",
        thread_id, len(phase1_state.get("user_resolutions") or {}),
    )
    final_state = await _phase2_graph.ainvoke(phase1_state)
    log.info(
        "phase2-done thread_id=%s zip=%s errors=%d",
        thread_id,
        bool(final_state.get("zip_path")),
        len(final_state.get("errors") or []),
    )
    return _summarize(final_state)


# ── Tools ────────────────────────────────────────────────────────────────────


@tool
async def generate_bundle(
    org: str,
    user_instructions: str | None = None,
    confirmed_separate_build: bool = False,
    config: RunnableConfig = None,
) -> dict:
    """Start an Avni bundle generation for a specific org.

    Reads the org's uploaded .xlsx files and builds a new bundle with new
    UUIDs. The deterministic parser runs first,
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
        confirmed_separate_build: Set to true only after the user has heard
            that this build is separate from the organisation's existing
            forms and still wants it. Required when the organisation's setup
            could not be loaded or its forms were set up outside Autopilot.
    """
    org = org.strip().lower()
    input_dir = os.path.join(settings.input_root, org)
    output_dir = os.path.join(settings.output_root, org)
    if not _has_spreadsheets(input_dir):
        return {"status": "error", "error": _NO_SPREADSHEET_ERROR}
    session = _session_of(config)
    if session is not None:
        if session.org_setup_loading:
            return {"status": "error", "error": _SETUP_LOADING_ERROR}
        if session.build_needs_confirmation and not confirmed_separate_build:
            return {"status": "error", "error": _SEPARATE_BUILD_NOT_CONFIRMED_ERROR}

    thread_id = f"bundle-{org}-{int(time.time())}"
    initial = initial_state(org, input_dir, output_dir, user_instructions)
    return _report_bundle(await _run_phase1_then_pause_or_finish(initial, thread_id), config)


@tool
async def edit_bundle_from_spec(
    org: str,
    user_instructions: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Update the org's existing bundle from the current source .xlsx (the spec).

    Reads the org's uploaded .xlsx files, re-runs the deterministic parser and
    the LLM enrichment pass (may pause for user confirmation of long-name
    shortenings / duplicate-field disambiguations — same as generate_bundle),
    then diffs the regenerated desired bundle against the existing bundle and
    applies the resulting field-level edits (add / remove / reorder, and
    single/multi-select changes on existing fields)
    atomically. In a webapp session the existing bundle is the org's current
    setup loaded at the start of the chat, or the bundle built in this chat.

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
    session = _session_of(config)
    if session is not None:
        bundle_path = str(session.bundle_path) if session.bundle_path else ""
    else:
        bundle_path = os.path.join(settings.output_root, org, f"{org.capitalize()}.zip")
    if not _has_spreadsheets(input_dir):
        return {"status": "error", "error": _NO_SPREADSHEET_ERROR}
    if session is not None and session.org_setup_loading:
        return {"status": "error", "error": _SETUP_LOADING_ERROR}
    if not bundle_path or not os.path.exists(bundle_path):
        return {
            "status": "error",
            "error": (
                "The organisation has no bundle to update — its current setup "
                "is not loaded in this chat and none was built here. Do not "
                "build a new one without telling the user why and asking."
            ),
        }
    try:
        origin = classify_bundle(bundle_path)
    except FileNotFoundError as exc:
        return _unreadable_bundle(exc)
    if origin is not BundleOrigin.AUTOPILOT:
        return {"status": "error", "error": _UNUPDATABLE_BUNDLE_ERRORS[origin]}

    output_dir = os.path.join(settings.output_root, org)
    thread_id = f"bundle-{org}-{int(time.time())}"
    initial = initial_state(
        org, input_dir, output_dir, user_instructions,
        mode="edit_from_spec", bundle_path=bundle_path,
    )
    return _report_bundle(await _run_phase1_then_pause_or_finish(initial, thread_id), config)


@tool
async def resume_bundle(
    thread_id: str,
    resolutions: dict[str, str],
    config: RunnableConfig = None,
) -> dict:
    """Resume a paused bundle run after the user confirms pending changes.

    Args:
        thread_id: The id returned in the prior `generate_bundle` /
            `resume_bundle` `needs_confirmation` response.
        resolutions: A dict mapping each `change_id` to one of:
            "yes"               — apply Claude's proposed `after`
            "no"                — skip this change
            "edit:<new_value>"  — apply with a user-provided override
    """
    saved = _saved_states.pop(thread_id, None)
    log.info(
        "resume_bundle thread_id=%s found_saved=%s n_resolutions=%d",
        thread_id, saved is not None, len(resolutions or {}),
    )
    if saved is None:
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
    saved["user_resolutions"] = resolutions or {}
    return _report_bundle(await _run_phase2(saved, thread_id), config)


@tool
def list_bundle_fields(
    bundle_path: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Inspect a bundle (ZIP or unpacked directory) and return a compact
    summary of every form, section, and field. Coded fields include their
    `answers` list.

    Each form has its `uuid` and `usedIn`: one entry per form mapping with
    the `subjectType`, `program` and `encounterType` the form is used for
    (keys absent when they do not apply). Each field has its form element
    `uuid` and its `conceptUuid`. Use these to answer which subject type,
    program or visit type a form belongs to, to match an ID the user pastes
    to its field, and to fill in IDs yourself — never ask the user for an
    ID or a mapping that is in this output.

    Use this BEFORE any tool that takes bundle names as arguments so your
    call uses real names from the bundle. The user often types informally —
    "supporting family", "marital", "baseline form" — but the bundle stores
    exact phrasings. Match is EXACT downstream (case + punctuation), so
    ground your args against this tool's output first.

    In a webapp session omit `bundle_path` — the session's bundle (the org's
    current setup, or the bundle built in this chat) is always used.

    Args:
        bundle_path: Path to a bundle ZIP file or an unpacked bundle directory.
            Ignored in a webapp session; required in the REPL.
    """
    resolved = _resolve_bundle_path(bundle_path, config)
    if not resolved:
        return {"status": "error", "error": _NO_BUNDLE_ERROR}
    try:
        fields = _list_bundle_fields(resolved)
    except FileNotFoundError as exc:
        return _unreadable_bundle(exc)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)}
    fields.pop("bundle_path", None)
    return {"status": "done", "result": fields}


@tool
def edit_bundle_fields(
    operations: list[dict],
    bundle_path: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Add, rename, or remove fields, or switch single/multi-select, in an already-generated bundle ZIP.

    Each operation has shape:
      {
        "op_id":  "<your-id>",
        "kind":   "field.add" | "field.rename" | "field.remove"
                  | "field.set_selection_type",
        "target": { "form": "...", "section": "...", "field": "..."? },
        "payload": { ... }     # kind-specific
      }

    field.add payload: name (required), dataType (default 'Text'),
      mandatory?, options? (required for Coded), selectionType?, unit?, min?,
      max?.
    field.rename payload: new_name (required).
    field.remove payload: empty.
    field.set_selection_type payload: selectionType ('SingleSelect' or
      'MultiSelect') — whether the field takes one answer or several.

    Matching is case-folded + whitespace-stripped EXACT (no fuzzy). On a
    mismatch the op is rejected with kind 'not_found' / 'ambiguous_target' /
    'duplicate_name' / 'schema'.

    Removes set `voided: true` on the form element so the server soft-deletes
    the corresponding record on re-upload. Re-adding the same field name later
    reinstates the original element (UUID preserved).

    When at least one form or concept was changed, the chat shows the bundle
    card with its Download button (the result carries `bundle_updated` in a
    webapp session, or the edited bundle's `zip_path` in the REPL).

    In a webapp session omit `bundle_path` — the session's bundle (the org's
    current setup, or the bundle built in this chat) is always used.

    Args:
        operations: List of edit operation dicts (see above).
        bundle_path: Bundle ZIP or unpacked directory. Ignored in a webapp
            session; required in the REPL.
    """
    resolved = _resolve_bundle_path(bundle_path, config)
    if not resolved:
        return {"status": "error", "error": _NO_BUNDLE_ERROR}
    try:
        result = apply_field_edits(resolved, operations)
    except FileNotFoundError as exc:
        return _unreadable_bundle(exc)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}
    response = {"status": "done", "result": result.model_dump(exclude={"bundle_path"})}
    if result.forms_modified or result.concepts_appended:
        response["zip_path"] = resolved
    return _report_bundle(response, config)


@tool
def suggest_form_rule(
    form_name: str,
    rule_kind: Literal[
        "visitScheduleRule",
        "validationRule",
        "editFormRule",
        "decisionRule",
    ],
    intent: str,
    form_uuid: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Generate a form-level rule JS from a natural-language intent.

    Does NOT write to the bundle. On `status: "suggested"` the checked JS is in
    `js`; render it to the user as a fenced ```javascript code block. On
    `status: "rejected"` no JS is returned — relay the `warnings` instead.

    `rule_kind` picks which Avni rule field on the form JSON is targeted:

      - "visitScheduleRule"  → when the next visit should be scheduled
      - "validationRule"     → block-save messages when the form has bad data
      - "editFormRule"       → who/when an already-saved form can be edited
      - "decisionRule"       → values to write into concepts at submit time

    Eligibility rules (programme, visit, member addition) are not form rules
    and no tool covers them; do not map them to any of these kinds.

    Before calling this, ground the user's intent against the bundle. The
    user usually phrases informally — short form names, paraphrased field
    names, casual answer wording. Call `list_bundle_fields` first to discover
    exact form names, concept names, encounter type names, and coded-answer
    strings. Echo the resolved names back to the user when confirming so they
    catch any mis-mapping before acting on the suggestion.
    When the user asks to send the request straight to this tool, skip
    `list_bundle_fields` and call it with their names. An unknown form
    returns an error, and a rule that reads a field not on the form is
    rejected. When several forms share the name, the one used in the app
    (mapped to a subject type, program or visit type) is picked; if more
    than one is used, an error lists each copy's `uuid` and `usedIn` —
    ask the user which one is meant and call again with its `form_uuid`.

    Args:
        form_name: Exact name of the form (matches `form.name` in the bundle).
        form_uuid: The form's `uuid` from `list_bundle_fields`, only when
            several forms share `form_name`; otherwise leave it out.
        rule_kind: One of "visitScheduleRule", "validationRule",
            "editFormRule", "decisionRule".
        intent: Natural-language description of what the rule should DO, not
            how. e.g. "block saving when age < 18 or > 60" for a validation
            rule, "only the user who created the form can edit it" for an
            edit-form rule.
    """
    session = (config or {}).get("configurable", {}).get("session") if config else None
    bundle_path = str(session.bundle_path) if session and session.bundle_path else None
    if not bundle_path:
        return {"status": "error", "error": "No bundle available for this session."}

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
        context = load_form_rule_context(bundle_path, form_name, form_uuid)
    except AmbiguousFormError as exc:
        return {"status": "error", "error": str(exc), "forms": exc.candidates}
    except FileNotFoundError as exc:
        return _unreadable_bundle(exc)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}

    if context is None:
        missing = f"{form_name!r} with uuid {form_uuid!r}" if form_uuid else repr(form_name)
        return {"status": "error", "error": f"form not found in bundle: {missing}"}

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

    return {
        "status": "suggested",
        "form_name": form_name,
        "rule_kind": kind.value,
        "js": result.js,
        "confidence": result.confidence,
        "used_helpers": result.used_helpers,
        "referenced_concepts": result.referenced_concepts,
        "referenced_encounter_types": result.referenced_encounter_types,
        "warnings": warnings,
    }


@tool
def suggest_form_element_rule(
    form_name: str,
    page_name: str,
    field_name: str,
    intent: str,
    form_uuid: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Generate a field-level rule JS from a natural-language intent.

    Does NOT write to the bundle. On `status: "suggested"` the checked JS is in
    `js`; render it to the user as a fenced ```javascript code block. On
    `status: "rejected"` no JS is returned — relay the `warnings` instead.

    Targets `form.formElementGroups[<page>].formElements[<field>].rule`.
    The behaviour mix (visibility / value / validation / answer-filter) is
    dictated by the intent text — examples:

      - "show this only when 'Consent given' is Yes"
      - "pre-fill with the value of 'Mobile number' from registration"
      - "block save when the date is in the past"
      - "only allow 'C-section' and 'Assisted' when 'Place of delivery' is Hospital"

    Before calling this, ground the user's intent against the bundle. Short
    form names, paraphrased field/page names, and casual answer wording are
    common — e.g. the user may say "yes" when the bundle answer is "Yes", or
    "place of birth" when the field is "Place of delivery". Call
    `list_bundle_fields` first to discover exact form, page, field, and
    coded-answer names. Echo the resolved names back to the user when
    confirming so any mis-mapping is caught before acting on the suggestion.
    When the user asks to send the request straight to this tool, skip
    `list_bundle_fields` and call it with their names. An unknown form
    returns an error, and a rule that reads a field not on the form is
    rejected. When several forms share the name, the one used in the app
    (mapped to a subject type, program or visit type) is picked; if more
    than one is used, an error lists each copy's `uuid` and `usedIn` —
    ask the user which one is meant and call again with its `form_uuid`.

    Args:
        form_name: Exact name of the form (matches `form.name` in the bundle).
        form_uuid: The form's `uuid` from `list_bundle_fields`, only when
            several forms share `form_name`; otherwise leave it out.
        page_name: Exact name of the page (matches `formElementGroups[i].name`).
        field_name: Exact name of the field (matches
            `formElementGroups[i].formElements[j].name`).
        intent: Natural-language description of what the rule should DO,
            not how. Any concept, field, or coded-answer names referenced
            in the intent must match the bundle EXACTLY — quote them as
            they appear in `list_bundle_fields` output (e.g. use 'Yes'
            not 'yes', 'Place of delivery' not 'where delivery happened').
            Mixing behaviours is fine: a single intent can ask for
            visibility AND a default value AND validation — they all land
            in the same `FormElementStatus` return.
            Examples:
              - "show only when 'Consent given' is Yes"
              - "pre-fill with the value of 'Mobile number' from registration"
              - "block save when the date is before today"
              - "only allow 'C-section' and 'Assisted delivery' when
                 'Place of delivery' is Hospital"
    """
    session = (config or {}).get("configurable", {}).get("session") if config else None
    bundle_path = str(session.bundle_path) if session and session.bundle_path else None
    if not bundle_path:
        return {"status": "error", "error": "No bundle available for this session."}

    try:
        context = load_form_rule_context(bundle_path, form_name, form_uuid)
    except AmbiguousFormError as exc:
        return {"status": "error", "error": str(exc), "forms": exc.candidates}
    except FileNotFoundError as exc:
        return _unreadable_bundle(exc)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}

    if context is None:
        missing = f"{form_name!r} with uuid {form_uuid!r}" if form_uuid else repr(form_name)
        return {"status": "error", "error": f"form not found in bundle: {missing}"}

    spec = RuleSpec(
        rule_kind=RuleKind.FORM_ELEMENT,
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
            "page_name": page_name,
            "field_name": field_name,
            "rule_kind": RuleKind.FORM_ELEMENT.value,
            "confidence": result.confidence,
            "warnings": warnings,
        }

    return {
        "status": "suggested",
        "form_name": form_name,
        "page_name": page_name,
        "field_name": field_name,
        "rule_kind": RuleKind.FORM_ELEMENT.value,
        "js": result.js,
        "confidence": result.confidence,
        "used_helpers": result.used_helpers,
        "referenced_concepts": result.referenced_concepts,
        "referenced_encounter_types": result.referenced_encounter_types,
        "warnings": warnings,
    }


_DOCS_UNAVAILABLE_NOTE = (
    "The Avni docs could not be searched just now. Tell the user so and do "
    "not answer the question from memory."
)

_DOCS_NO_MATCH_NOTE = (
    "No docs entry matches this question. Tell the user the Avni docs do not "
    "cover it; do not answer it from memory."
)

_DOCS_EXCERPTS_NOTE = (
    "Answer only with what these excerpts say and name the title of each "
    "entry used. If none of them answers the question, tell the user the "
    "Avni docs do not cover it; do not fill the gap from memory."
)


@tool
def answer_avni_question(question: str) -> dict:
    """Search the Avni docs for excerpts that answer a question about Avni.

    Returns up to five entries (`title` + `content`) ranked by similarity and
    a `note` on how to use them. Entries too far from the question are left
    out; an empty `excerpts` list means the docs were not searched or no
    entry is close enough. A returned entry may still not answer the
    question, so read it before relying on it.

    Args:
        question: A natural-language question about Avni.
    """
    if not os.environ.get("VOYAGE_API_KEY"):
        log.warning("Docs KB search skipped: VOYAGE_API_KEY not configured")
        return {"excerpts": [], "note": _DOCS_UNAVAILABLE_NOTE}
    try:
        entries = _docs_kb.retrieve(
            question, top_k=5, min_relevance=settings.docs_min_relevance,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(f"Docs KB retrieve failed: {exc}")
        return {"excerpts": [], "note": _DOCS_UNAVAILABLE_NOTE}

    if not entries:
        return {"excerpts": [], "note": _DOCS_NO_MATCH_NOTE}

    return {
        "excerpts": [{"title": e.title, "content": e.body} for e in entries],
        "note": _DOCS_EXCERPTS_NOTE,
    }


TOOLS = [
    generate_bundle,
    edit_bundle_from_spec,
    resume_bundle,
    list_bundle_fields,
    edit_bundle_fields,
    suggest_form_rule,
    suggest_form_element_rule,
    answer_avni_question,
]

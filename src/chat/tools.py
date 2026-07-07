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

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import Command

log = logging.getLogger(__name__)

from config import settings
from domain.bundle_editor import (
    apply_field_edits,
    list_bundle_fields as _list_bundle_fields,
    load_form_rule_context,
)
from domain.docs.knowledge_base import DocsKnowledgeBase
from domain.rules.generator import RuleGenerator
from domain.rules.rule_spec import RuleKind, RuleSpec
from domain.rules.validator import validate_and_decide as _validate_and_decide
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


async def _run_phase1_then_pause_or_finish(initial: dict, thread_id: str) -> dict:
    """Run phase 1; if `pending_changes` is non-empty, save state and return
    needs_confirmation. Otherwise run phase 2 to completion and summarize.

    `ainvoke` (not `invoke`) so the call stays in the chat agent's event loop.
    With sync `invoke` LangChain wraps sync tools in a thread executor, where
    LangGraph 1.x's internal awaits would have no event loop.
    """
    phase1_state = await _phase1_graph.ainvoke(initial)
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
async def generate_bundle(org: str, user_instructions: str | None = None) -> dict:
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
    initial = initial_state(org, input_dir, output_dir, user_instructions)
    return await _run_phase1_then_pause_or_finish(initial, thread_id)


@tool
async def edit_bundle_from_spec(org: str, user_instructions: str | None = None) -> dict:
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
    initial = initial_state(
        org, input_dir, output_dir, user_instructions,
        mode="edit_from_spec", bundle_path=bundle_path,
    )
    return await _run_phase1_then_pause_or_finish(initial, thread_id)


@tool
async def resume_bundle(thread_id: str, resolutions: dict[str, str]) -> dict:
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
    return await _run_phase2(saved, thread_id)


@tool
def list_bundle_fields(
    bundle_path: str | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Inspect a bundle (ZIP or unpacked directory) and return a compact
    summary of every form, section, and field. Coded fields include their
    `answers` list.

    Use this BEFORE any tool that takes bundle names as arguments so your
    call uses real names from the bundle. The user often types informally —
    "supporting family", "marital", "baseline form" — but the bundle stores
    exact phrasings. Match is EXACT downstream (case + punctuation), so
    ground your args against this tool's output first.

    In a webapp session `bundle_path` may be omitted — the session's
    downloaded org bundle is used automatically.

    Args:
        bundle_path: Path to a bundle ZIP file or an unpacked bundle directory.
            Optional in a webapp session; required in the REPL.
    """
    session = (config or {}).get("configurable", {}).get("session") if config else None
    resolved = bundle_path or (str(session.bundle_path) if session and session.bundle_path else None)
    if not resolved:
        return {"status": "error", "error": "No bundle available. Provide bundle_path or generate a bundle first."}
    try:
        return {"status": "done", "result": _list_bundle_fields(resolved)}
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
def suggest_form_rule(
    form_name: str,
    rule_kind: Literal[
        "visitScheduleRule",
        "validationRule",
        "editFormRule",
        "decisionRule",
    ],
    intent: str,
    config: RunnableConfig = None,
) -> dict:
    """Generate a form-level rule JS from a natural-language intent.

    Does NOT write to the bundle. Returns the full suggested JS in `js` —
    always render it to the user as a fenced ```javascript code block.

    `rule_kind` picks which Avni rule field on the form JSON is targeted:

      - "visitScheduleRule"  → when the next visit should be scheduled
      - "validationRule"     → block-save messages when the form has bad data
      - "editFormRule"       → who/when the form can be edited (eligibility)
      - "decisionRule"       → values to write into concepts at submit time

    Before calling this, ground the user's intent against the bundle. The
    user usually phrases informally — short form names, paraphrased field
    names, casual answer wording. Call `list_bundle_fields` first to discover
    exact form names, concept names, encounter type names, and coded-answer
    strings. Echo the resolved names back to the user when confirming so they
    catch any mis-mapping before acting on the suggestion.

    Args:
        form_name: Exact name of the form (matches `form.name` in the bundle).
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
        context = load_form_rule_context(bundle_path, form_name)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}

    if context is None:
        return {"status": "error", "error": f"form not found in bundle: {form_name!r}"}

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
            "js": result.js,
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
    config: RunnableConfig = None,
) -> dict:
    """Generate a field-level rule JS from a natural-language intent.

    Does NOT write to the bundle. Returns the full suggested JS in `js` —
    always render it to the user as a fenced ```javascript code block.

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

    Args:
        form_name: Exact name of the form (matches `form.name` in the bundle).
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
        context = load_form_rule_context(bundle_path, form_name)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"unexpected: {exc}"}

    if context is None:
        return {"status": "error", "error": f"form not found in bundle: {form_name!r}"}

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
            "js": result.js,
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


@tool
def answer_avni_question(question: str) -> dict:
    """Retrieve relevant Avni knowledge base excerpts for a question.

    Returns the most relevant documentation excerpts so the agent can answer
    questions about Avni concepts, form configuration, JavaScript rules,
    troubleshooting, and implementation patterns. The agent should use these
    excerpts as its primary source and answer directly — do not call this tool
    and then restate what it returns.

    Args:
        question: A natural-language question about Avni.
    """
    if not os.environ.get("VOYAGE_API_KEY"):
        return {"excerpts": [], "note": "VOYAGE_API_KEY not configured — answer from your own knowledge."}
    try:
        entries = _docs_kb.retrieve(question, top_k=5)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"Docs KB retrieve failed: {exc}")
        return {"excerpts": [], "note": f"Knowledge base unavailable: {exc}"}

    if not entries:
        return {"excerpts": [], "note": "No relevant excerpts found — answer from your own knowledge."}

    return {
        "excerpts": [{"title": e.title, "content": e.body} for e in entries],
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

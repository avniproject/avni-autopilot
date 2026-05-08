"""
enricher — orchestrates the LLM enrichment pass for one form.

Workflow per form:
  1. Skip-when-clean gate — if the form has no long names (>255 chars) and
     no duplicate field names, skip the LLM entirely.
  2. Call LLMHelper.enrich(form, df, user_instructions). The LLM is
     instructed to return the parser's FormSpec UNCHANGED and only emit
     Change records, so we ignore `enriched.form` and keep using the
     parser's form.
  3. Drop unjustified changes via `_drop_unjustified_changes` (long_name
     for short fields, duplicate_field for non-duplicate names).
  4. Namespace change_ids with the form name so confirmations from two
     forms don't collide in the resolutions dict.
  5. Surface every surviving Change as `pending_changes`; the caller
     (`pipeline.apply_user_decisions`) presents them via `interrupt()` for
     the user to confirm/edit/reject.

Public surface:
    refined, applied, pending, warnings = enrich_form(form, df, user_instructions, llm_helper)
    refined, applied, pending, warnings = enrich_forms(forms, sheets, user_instructions, llm_helper)

`applied` is always [] — kept in the return shape so callers don't need to
change. Every refinement now requires explicit user confirmation.
"""

from __future__ import annotations

import logging

import pandas as pd

from llm_helper import LLMHelper
from models import Change, FieldSpec, FormSpec

log = logging.getLogger(__name__)

NAME_LIMIT = 255


# ── Skip gate ─────────────────────────────────────────────────────────────────


def _form_is_clean(form: FormSpec, user_instructions: str | None) -> bool:
    """True iff the parser's FormSpec has no long names and no duplicate names.

    These are the only two issues the LLM is currently allowed to refine
    (long_name, duplicate_field), so any form without them can skip the call.
    """
    field_names_lower: set[str] = set()
    for field in _all_fields(form):
        if len(field.name) > NAME_LIMIT:
            return False
        key = field.name.strip().lower()
        if key in field_names_lower:
            return False
        field_names_lower.add(key)
    return True


def _all_fields(form: FormSpec) -> list[FieldSpec]:
    """Flatten every section's fields into a single list."""
    out: list[FieldSpec] = []
    for section in form.sections or []:
        out.extend(section.fields)
    return out


# ── Objective change-justification gate ──────────────────────────────────────
#
# Haiku will sometimes propose long_name shortenings for fields that are well
# under the 255-char limit, or duplicate_field renames for names that only
# appear once. The thresholds are objective, so drop unjustified changes here
# rather than asking the user to confirm noise.


def _drop_unjustified_changes(
    changes: list[Change], parser_form: FormSpec, warnings: list[str]
) -> list[Change]:
    # Strict: keep only changes that match a real condition in the parser form.
    # long_name:        change.field must name a field with length > 255.
    # duplicate_field:  change.field must name a field that appears > 1 time.
    long_field_names_lower = {
        f.name.strip().lower()
        for f in _all_fields(parser_form)
        if len(f.name) > NAME_LIMIT
    }
    name_counts: dict[str, int] = {}
    for f in _all_fields(parser_form):
        key = f.name.strip().lower()
        name_counts[key] = name_counts.get(key, 0) + 1

    kept: list[Change] = []
    for ch in changes:
        key = (ch.field or "").strip().lower()
        if ch.kind == "long_name":
            if key not in long_field_names_lower:
                warnings.append(
                    f"Dropped long_name {ch.change_id} — '{ch.field[:80]}' is "
                    f"not a >{NAME_LIMIT}-char field in form '{parser_form.name}'"
                )
                continue
        elif ch.kind == "duplicate_field":
            if name_counts.get(key, 0) <= 1:
                warnings.append(
                    f"Dropped duplicate_field {ch.change_id} on '{ch.field}' — "
                    f"name appears only once in form"
                )
                continue
        kept.append(ch)
    return kept


# ── Public entry points ───────────────────────────────────────────────────────


def enrich_form(
    parser_form: FormSpec,
    df: pd.DataFrame,
    user_instructions: str | None,
    llm_helper: LLMHelper,
) -> tuple[FormSpec, list[Change], list[Change], list[str]]:
    """
    Returns (refined_form, auto_applied, pending_changes, warnings).

    `refined_form` is always the parser's form — the LLM is instructed to
    return the FormSpec unchanged, so we don't trust its copy. `auto_applied`
    is always [] — every Change is surfaced to the user for confirmation.
    The tuple shape is preserved so callers don't change.
    """
    if _form_is_clean(parser_form, user_instructions) or not llm_helper.is_available():
        return parser_form, [], [], []

    log.info("[enrich] LLM call for form: %s", parser_form.name)
    try:
        enriched = llm_helper.enrich(parser_form, df, user_instructions)
    except Exception as exc:  # noqa: BLE001
        log.warning("[enrich] LLM call failed for '%s': %s", parser_form.name, exc)
        return parser_form, [], [], [
            f"LLM enrichment failed for form '{parser_form.name}': {exc}"
        ]

    warnings: list[str] = []
    justified = _drop_unjustified_changes(enriched.changes, parser_form, warnings)

    # Namespace change_ids with the form name. Haiku's prompt only requires
    # uniqueness *within* a form, so two different forms can both emit
    # "field-1/long_name". The chat agent serializes resolutions as a single
    # dict keyed by change_id; identical keys would collide and silently drop
    # one confirmation.
    justified = [
        ch.model_copy(update={"change_id": f"{parser_form.name}::{ch.change_id}"})
        for ch in justified
    ]

    log.info(
        "[enrich] %s: %d pending, %d warnings",
        parser_form.name, len(justified), len(warnings),
    )
    return parser_form, [], justified, warnings


def enrich_forms(
    forms: list[FormSpec],
    sheets: dict[str, pd.DataFrame],
    user_instructions: str | None,
    llm_helper: LLMHelper,
) -> tuple[list[FormSpec], list[Change], list[Change], list[str]]:
    """
    Run enrichment across every form.

    Returns:
        refined_forms     — list of FormSpec, same length/order as input
        applied_changes   — always empty (kept for return-shape stability)
        pending_changes   — every Change awaiting user confirmation
        warnings          — any validation/skip warnings worth surfacing
    """
    refined: list[FormSpec] = []
    applied: list[Change] = []
    pending: list[Change] = []
    warnings: list[str] = []

    for form in forms:
        df = sheets.get(form.name)
        if df is None:
            # No source sheet found — pass through unchanged.
            refined.append(form)
            continue
        new_form, form_applied, form_pending, form_warnings = enrich_form(
            form, df, user_instructions, llm_helper
        )
        refined.append(new_form)
        applied.extend(form_applied)
        pending.extend(form_pending)
        warnings.extend(form_warnings)
    return refined, applied, pending, warnings

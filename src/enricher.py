"""
enricher — orchestrates the LLM enrichment pass for one form.

Workflow per form:
  1. Skip-when-clean gate — if the form has no long names (>255 chars) and
     no duplicate field names, skip the LLM entirely.
  2. Call LLMHelper.enrich(form, df, user_instructions).
  3. Surface every Change as `pending_changes`; the caller
     (`pipeline.enrich_with_llm`) presents them via `interrupt()` for the
     user to confirm/edit/reject.

Public surface:
    refined, applied, pending, warnings = enrich_form(form, df, user_instructions, llm_helper)
    refined, applied, pending, warnings = enrich_forms(forms, sheets, user_instructions, llm_helper)

`applied` is always [] — kept in the return shape so callers don't need to
change. Every refinement now requires explicit user confirmation.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from rapidfuzz import fuzz

from llm_helper import LLMHelper
from models import Change, EnrichedFormSpec, FieldSpec, FormSpec

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85   # partial_ratio cutoff for "this text is in the source"
NAME_LIMIT = 255


# ── Skip gates (SDD §4.4) ─────────────────────────────────────────────────────


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
    """Flatten fields from `fields` + every section's fields."""
    out: list[FieldSpec] = list(form.fields or [])
    for section in form.sections or []:
        out.extend(section.fields)
    return out


# ── Anti-hallucination validation (SDD §4.3) ──────────────────────────────────


def _df_text_blob(df: pd.DataFrame) -> str:
    """Concatenate every non-null cell into one searchable string (lowercased)."""
    parts: list[str] = []
    for col in df.columns:
        for val in df[col]:
            if val is None:
                continue
            s = str(val).strip()
            if s and s.lower() != "nan":
                parts.append(s)
    return "\n".join(parts).lower()


def _fuzzy_in(needle: str, haystack: str) -> bool:
    """True iff `needle` fuzzy-matches some substring of `haystack` at threshold."""
    if not needle:
        return False
    score = fuzz.partial_ratio(needle.lower(), haystack)
    return score >= FUZZY_THRESHOLD


def _validate_enriched_form(
    enriched: EnrichedFormSpec,
    parser_form: FormSpec,
    df: pd.DataFrame,
    user_instructions: str | None,
) -> tuple[EnrichedFormSpec, list[str]]:
    """Drop hallucinated fields/options. Returns (cleaned_enriched, warnings)."""
    warnings: list[str] = []
    sheet_text = _df_text_blob(df)
    parser_field_names_lower = {f.name.strip().lower() for f in _all_fields(parser_form)}
    user_requested_add = (
        bool(user_instructions) and "add" in user_instructions.lower()
    )

    cleaned_form = enriched.form.model_copy(deep=True)

    def _filter_fields(fields: list[FieldSpec]) -> list[FieldSpec]:
        kept: list[FieldSpec] = []
        for f in fields:
            name_l = f.name.strip().lower()
            # Field must either already exist (parser's output) or fuzzy-match
            # the source DataFrame. add_field is allowed only when user asked.
            if name_l in parser_field_names_lower or _fuzzy_in(f.name, sheet_text):
                kept.append(_filter_options(f, sheet_text, warnings))
            elif user_requested_add:
                kept.append(_filter_options(f, sheet_text, warnings))
            else:
                warnings.append(
                    f"Dropped LLM-invented field '{f.name}' "
                    f"(not in source sheet for form '{parser_form.name}')"
                )
        return kept

    cleaned_form.fields = _filter_fields(cleaned_form.fields or [])
    if cleaned_form.sections:
        for section in cleaned_form.sections:
            section.fields = _filter_fields(section.fields)
        # Sections are the source of truth for field membership. Drop the
        # parallel `fields` list so a downstream rename doesn't have to
        # mutate two duplicated FieldSpec instances (msgpack checkpointing
        # turns shared references into separate objects).
        cleaned_form.fields = []

    # Drop Change records whose target field no longer exists post-filter.
    surviving_field_names = {
        f.name.strip().lower() for f in _all_fields(cleaned_form)
    }
    cleaned_changes: list[Change] = []
    for ch in enriched.changes:
        if ch.field == "":
            cleaned_changes.append(ch)
            continue
        if ch.field.strip().lower() in surviving_field_names:
            cleaned_changes.append(ch)
        else:
            warnings.append(
                f"Dropped Change {ch.change_id} (its target field was filtered out)"
            )

    enriched_clean = EnrichedFormSpec(form=cleaned_form, changes=cleaned_changes)
    return enriched_clean, warnings


def _filter_options(f: FieldSpec, sheet_text: str, warnings: list[str]) -> FieldSpec:
    """For Coded fields, drop options that don't fuzzy-match the source."""
    if f.dataType != "Coded" or not f.options:
        return f
    kept: list[str] = []
    for opt in f.options:
        if _fuzzy_in(opt, sheet_text):
            kept.append(opt)
        else:
            warnings.append(
                f"Dropped LLM-invented option '{opt}' on field '{f.name}'"
            )
    if kept != f.options:
        f = f.model_copy(update={"options": kept})
    return f




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

    `auto_applied` is always empty — every Change is surfaced to the user
    for confirmation. The tuple shape is preserved so callers don't change.
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

    enriched, warnings = _validate_enriched_form(
        enriched, parser_form, df, user_instructions
    )
    justified = _drop_unjustified_changes(enriched.changes, parser_form, warnings)

    # Namespace change_ids with the form name. Haiku's prompt only requires
    # uniqueness *within* a form, so two different forms can both emit
    # "field-1/long_name". The chat agent serializes resolutions as a single
    # dict keyed by change_id, and identical keys collide — silently dropping
    # one confirmation.
    justified = [
        ch.model_copy(update={"change_id": f"{parser_form.name}::{ch.change_id}"})
        for ch in justified
    ]

    log.info(
        "[enrich] %s: %d pending, %d warnings",
        parser_form.name, len(justified), len(warnings),
    )
    return enriched.form, [], justified, warnings


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
        applied_changes   — every auto-apply Change across all forms
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

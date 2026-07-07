"""enricher — orchestrates the LLM enrichment pass across all forms.

Workflow:
  1. Detect — scan all forms deterministically for long_name (>255 chars) and
     duplicate_field issues. No LLM involved in detection.
  2. Skip gate — if no problems are found, return immediately without a LLM call.
  3. Suggest — send the full problem list to the LLM in ONE call. The LLM only
     proposes replacement names; it does not scan forms or detect issues.
  4. Assemble — build Change records from the LLM suggestions, matched back by
     (form_name, section_name, field_name).
  5. Surface — return every Change as pending_changes for user confirmation.

Public surface:
    refined, applied, pending, warnings = enrich_forms(forms, user_instructions, llm_helper)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from domain.llm import LLMHelper
from models import Change, FormSpec

log = logging.getLogger(__name__)

NAME_LIMIT = 255


# ── Problem detection ─────────────────────────────────────────────────────────


@dataclass
class _FieldProblem:
    kind: Literal["long_name", "duplicate_field", "conflicting_concept"]
    form_name: str
    field_name: str
    section_name: str
    occurrence: int                         # 1-based; 1 for long_name, 1..N for duplicates
    context_sections: dict[str, list[str]]  # section_name → field names in that section
    # conflicting_concept only: this form's answers and the other conflicting forms
    own_options: list[str] = field(default_factory=list)
    conflicting_forms: list[tuple[str, list[str]]] = field(default_factory=list)  # [(form_name, options)]


def _detect_cross_form_conflicts(forms: list[FormSpec]) -> list[_FieldProblem]:
    """Scan across all forms for Coded fields reused with different answer lists.

    Avni enforces globally unique concept names, so the same concept name with
    different answers in different forms is a data modelling error that must be
    resolved by renaming.
    """
    # concept_name (normalised) → list of (form, section_name, field)
    occurrences: dict[str, list[tuple[FormSpec, str, FormSpec]]] = defaultdict(list)
    for form in forms:
        for section in (form.sections or []):
            for fld in (section.fields or []):
                if fld.dataType != "Coded" or not fld.options:
                    continue
                occurrences[fld.name.strip().lower()].append((form, section.name, fld))

    problems: list[_FieldProblem] = []
    for instances in occurrences.values():
        if len(instances) <= 1:
            continue
        option_sets = [frozenset(f.options) for _, _, f in instances]
        if len(set(option_sets)) <= 1:
            continue  # identical answers everywhere — not a conflict

        for form, section_name, fld in instances:
            others = [
                (other_form.name, list(other_fld.options))
                for other_form, _, other_fld in instances
                if other_form.name != form.name
            ]
            context_sections = {
                sec.name: [f.name for f in sec.fields]
                for sec in (form.sections or [])
                if sec.name == section_name
            }
            problems.append(_FieldProblem(
                kind="conflicting_concept",
                form_name=form.name,
                field_name=fld.name,
                section_name=section_name,
                occurrence=1,
                context_sections=context_sections,
                own_options=list(fld.options),
                conflicting_forms=others,
            ))

    return problems


def _detect_problems(forms: list[FormSpec]) -> list[_FieldProblem]:
    """Scan all forms for long_name and duplicate_field problems."""
    problems: list[_FieldProblem] = []

    for form in forms:
        # ── long_name ─────────────────────────────────────────────────────────
        for section in (form.sections or []):
            for fld in (section.fields or []):
                if len(fld.name) > NAME_LIMIT:
                    problems.append(_FieldProblem(
                        kind="long_name",
                        form_name=form.name,
                        field_name=fld.name,
                        section_name=section.name,
                        occurrence=1,
                        context_sections={section.name: [f.name for f in section.fields]},
                    ))

        # ── duplicate_field ───────────────────────────────────────────────────
        name_groups: dict[str, list[tuple[str, str]]] = {}
        for section in (form.sections or []):
            for fld in (section.fields or []):
                key = fld.name.strip().lower()
                name_groups.setdefault(key, []).append((section.name, fld.name))

        for occurrences in name_groups.values():
            if len(occurrences) <= 1:
                continue
            dup_section_names = {sec for sec, _ in occurrences}
            context_sections: dict[str, list[str]] = {
                section.name: [f.name for f in section.fields]
                for section in (form.sections or [])
                if section.name in dup_section_names
            }
            for occ_idx, (sec_name, field_name) in enumerate(occurrences, 1):
                problems.append(_FieldProblem(
                    kind="duplicate_field",
                    form_name=form.name,
                    field_name=field_name,
                    section_name=sec_name,
                    occurrence=occ_idx,
                    context_sections=context_sections,
                ))

    return problems


# ── Public entry point ────────────────────────────────────────────────────────


def enrich_forms(
    forms: list[FormSpec],
    user_instructions: str | None,
    llm_helper: LLMHelper,
) -> tuple[list[FormSpec], list[Change], list[Change], list[str]]:
    """Run enrichment across every form.

    Returns:
        refined_forms   — list of FormSpec, same length/order as input (unchanged)
        applied_changes — always empty (kept for return-shape stability)
        pending_changes — every Change awaiting user confirmation
        warnings        — any warnings worth surfacing
    """
    problems = _detect_cross_form_conflicts(forms) + _detect_problems(forms)

    if not problems:
        return forms, [], [], []

    if not llm_helper.is_available():
        log.info(
            "[enrich] %d problem(s) detected but LLM unavailable; skipping.", len(problems)
        )
        return forms, [], [], []

    log.info(
        "[enrich] %d problem(s) detected across %d form(s); calling LLM.",
        len(problems), len({p.form_name for p in problems}),
    )

    try:
        output = llm_helper.suggest_names(problems)
    except Exception as exc:  # noqa: BLE001
        log.warning("[enrich] LLM call failed: %s", exc)
        return forms, [], [], [f"LLM enrichment failed: {exc}"]

    suggestion_map = {
        (s.form_name, s.section_name, s.field_name): s.suggested_name
        for s in output.suggestions
    }

    changes: list[Change] = []
    seen_ids: dict[str, int] = {}
    for problem in problems:
        key = (problem.form_name, problem.section_name, problem.field_name)
        suggested = suggestion_map.get(key)
        if suggested is None:
            log.warning(
                "[enrich] no suggestion returned for %s / %s / %s",
                problem.form_name, problem.section_name, problem.field_name,
            )
            continue
        base_id = (
            f"{problem.form_name}::{problem.section_name}"
            f":{problem.field_name}/{problem.kind}"
        )
        count = seen_ids.get(base_id, 0)
        seen_ids[base_id] = count + 1
        change_id = base_id if count == 0 else f"{base_id}#{count}"
        changes.append(Change(
            change_id=change_id,
            form=problem.form_name,
            field=problem.field_name,
            kind=problem.kind,
            before={"name": problem.field_name, "section": problem.section_name},
            after={"name": suggested},
            reason="auto-detected",
        ))

    log.info("[enrich] %d change(s) pending user confirmation.", len(changes))
    return forms, [], changes, []

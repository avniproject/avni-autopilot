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
from dataclasses import dataclass
from typing import Literal

from domain.llm import LLMHelper
from models import Change, FormSpec

log = logging.getLogger(__name__)

NAME_LIMIT = 255


# ── Problem detection ─────────────────────────────────────────────────────────


@dataclass
class _FieldProblem:
    kind: Literal["long_name", "duplicate_field"]
    form_name: str
    field_name: str
    section_name: str
    occurrence: int                         # 1-based; 1 for long_name, 1..N for duplicates
    context_sections: dict[str, list[str]]  # section_name → field names in that section


def _detect_problems(forms: list[FormSpec]) -> list[_FieldProblem]:
    """Scan all forms and return every long_name and duplicate_field problem."""
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
    problems = _detect_problems(forms)

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
    for problem in problems:
        key = (problem.form_name, problem.section_name, problem.field_name)
        suggested = suggestion_map.get(key)
        if suggested is None:
            log.warning(
                "[enrich] no suggestion returned for %s / %s / %s",
                problem.form_name, problem.section_name, problem.field_name,
            )
            continue
        change_id = (
            f"{problem.form_name}::{problem.section_name}"
            f":{problem.field_name}/{problem.kind}"
        )
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

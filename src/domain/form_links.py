"""Form-to-entity linking via LLM classification.

Replaces the keyword-based ladder in `parser._resolve_form_subject_types`
with a single Claude Haiku pass that matches every form sheet to its
declared entity in the modelling doc.

Public surface:

  classify_forms(forms, entity_spec, sheet_names, llm_helper)
      Returns (link_results, warnings). Run after `parse_documents`.

  apply_form_link_results(forms, link_results)
      Returns (linked_forms, dropped_sheets, warnings). Stamps formType /
      subjectType / program / encounterType onto each FormSpec; drops
      sheets the LLM classified as junk; warns on sheets the LLM did not
      classify at all.

  build_entity_catalog(entity_spec)
      Pure conversion from `EntitySpec` to the structured dict that feeds
      both the prompt and the validator. Lifted out so tests can stub
      the catalog independently of any xlsx.

  validate_result(result, catalog)
      Per-row validation: confidence enum, pool membership, formType
      consistency. Returns (ok, error_message).

This phase covers the pure-function layer; the Claude Haiku call lives in
`classify_forms`'s `llm_helper.link_forms(...)` call, wired in phase 3.
See specs/FORM_ENTITY_LINKING_SDD.md.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from models import EntitySpec, FormSpec

log = logging.getLogger(__name__)


# A sheet name is "form-like" (and so warrants HITL review when the LLM
# classifies it as junk) when it has more than 15 characters and is not of
# the form `Sheet\d+` (Excel's auto-generated default). Tunable via the
# SDD §5.6 false-positive guard.
_GENERIC_SHEET_NAME_RE = re.compile(r"^Sheet\d+$", re.IGNORECASE)


def is_form_like_sheet_name(name: str) -> bool:
    """Return True when the sheet name looks like a real form (not a default).

    Used to surface a HITL card when the LLM marks a long, named sheet as
    junk — those false positives are the easiest for an operator to spot.
    """
    s = (name or "").strip()
    if len(s) <= 15:
        return False
    return _GENERIC_SHEET_NAME_RE.match(s) is None


# ── Result model ──────────────────────────────────────────────────────────────


FormTypeLiteral = Literal[
    "IndividualProfile",
    "ProgramEnrolment",
    "ProgramExit",
    "ProgramEncounter",
    "ProgramEncounterCancellation",
    "Encounter",
    "IndividualEncounterCancellation",
]


class FormLinkResult(BaseModel):
    """One row of the LLM's classification output."""

    sheet_name: str
    form_type: FormTypeLiteral | None = None
    subject_type: str | None = None
    program: str | None = None
    encounter_type: str | None = None
    confidence: Literal["high", "medium", "low"] = "low"
    reasoning: str = ""


class FormLinkResults(BaseModel):
    """Container for the LLM's batched classification output.

    `with_structured_output` binds to a single model; wrapping the list in a
    container model lets the Haiku call return all sheet classifications in
    one shot.
    """

    results: list[FormLinkResult]


# ── Form-type consistency tables ──────────────────────────────────────────────


# For each formType, which (subject_type, program, encounter_type) references
# must be non-null. `False` means the field must be null. `True` means non-null.
_REQUIRED_REFS: dict[FormTypeLiteral, tuple[bool, bool, bool]] = {
    "IndividualProfile":              (True,  False, False),
    "ProgramEnrolment":                (True,  True,  False),
    "ProgramExit":                     (True,  True,  False),
    "ProgramEncounter":                (True,  True,  True),
    "ProgramEncounterCancellation":    (True,  True,  True),
    "Encounter":                       (True,  False, True),
    "IndividualEncounterCancellation": (True,  False, True),
}


# ── Catalog assembly ──────────────────────────────────────────────────────────


class EntityCatalog(BaseModel):
    """Structured pools fed to the prompt and the validator.

    Each pool is a list of `(canonical_name, hint_dict)` tuples. The
    canonical name is what the LLM must return verbatim; the hint dict
    carries the raw form-source columns + cross-references so the model
    has the operator's wording as context without changing the pool keys.
    """

    subject_types: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)
    programs: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)
    program_encounters: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)
    encounters: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)


def build_entity_catalog(entity_spec: EntitySpec) -> EntityCatalog:
    """Convert an `EntitySpec` into the catalog the LLM and validator share."""
    cat = EntityCatalog()
    for st in entity_spec.subject_types:
        cat.subject_types.append((
            st.name,
            {
                "type": st.type,
                "registration_form_source": st.registration_form_source,
            },
        ))
    for p in entity_spec.programs:
        cat.programs.append((
            p.name,
            {
                "target_subject_type": p.target_subject_type or None,
                "enrolment_form_source": p.enrolment_form_source,
                "exit_form_source": p.exit_form_source,
            },
        ))
    for e in entity_spec.encounter_types:
        entry = (
            e.name,
            {
                "program": e.program_name or None,
                "subject_type": e.subject_type or None,
                "form_source": e.form_source,
                "cancellation_form_source": e.cancellation_form_source,
            },
        )
        if e.is_program_encounter:
            cat.program_encounters.append(entry)
        else:
            cat.encounters.append(entry)
    return cat


# ── Pool membership helpers ───────────────────────────────────────────────────


def _norm(s: str | None) -> str:
    """Case-insensitive, whitespace-stripped key for pool membership."""
    return (s or "").strip().lower()


def _in_pool(name: str | None, pool: list[tuple[str, dict]]) -> bool:
    if name is None:
        return True
    key = _norm(name)
    return any(_norm(canonical) == key for canonical, _ in pool)


def _canonical(name: str | None, pool: list[tuple[str, dict]]) -> str | None:
    """Resolve a (possibly differently-cased) LLM-returned name to the pool's
    canonical spelling. Returns the input unchanged on miss — caller is
    responsible for rejecting via `_in_pool` first."""
    if name is None:
        return None
    key = _norm(name)
    for canonical, _ in pool:
        if _norm(canonical) == key:
            return canonical
    return name


# ── Validator ─────────────────────────────────────────────────────────────────


def validate_result(
    result: FormLinkResult, catalog: EntityCatalog,
) -> tuple[bool, str]:
    """Return (ok, error_message). Error is empty when ok."""
    # Junk classification — every entity field must also be null.
    if result.form_type is None:
        if result.subject_type or result.program or result.encounter_type:
            return False, (
                "form_type is null but at least one entity reference is set; "
                "junk classifications must have all entity fields null"
            )
        return True, ""

    # Pool membership.
    if not _in_pool(result.subject_type, catalog.subject_types):
        return False, f"subject_type {result.subject_type!r} not in subject pool"
    encounter_pool = catalog.program_encounters + catalog.encounters
    if not _in_pool(result.encounter_type, encounter_pool):
        return False, f"encounter_type {result.encounter_type!r} not in encounter pool"
    if not _in_pool(result.program, catalog.programs):
        return False, f"program {result.program!r} not in program pool"

    # formType consistency: required fields must match (True ↔ non-null).
    needs_subject, needs_program, needs_encounter = _REQUIRED_REFS[result.form_type]
    actual_subject = result.subject_type is not None
    actual_program = result.program is not None
    actual_encounter = result.encounter_type is not None
    if needs_subject != actual_subject:
        return False, (
            f"{result.form_type}: subject_type "
            f"{'required' if needs_subject else 'must be null'}"
        )
    if needs_program != actual_program:
        return False, (
            f"{result.form_type}: program "
            f"{'required' if needs_program else 'must be null'}"
        )
    if needs_encounter != actual_encounter:
        return False, (
            f"{result.form_type}: encounter_type "
            f"{'required' if needs_encounter else 'must be null'}"
        )
    return True, ""


# ── Application ───────────────────────────────────────────────────────────────


def _stamp_form(
    form: FormSpec, result: FormLinkResult, catalog: EntityCatalog,
) -> FormSpec:
    """Return a FormSpec copy with linkage fields stamped from a result."""
    return form.model_copy(update={
        "formType": result.form_type,
        "subjectType": _canonical(result.subject_type, catalog.subject_types) or "",
        "program": _canonical(result.program, catalog.programs) or "",
        "encounterType": _canonical(
            result.encounter_type,
            catalog.program_encounters + catalog.encounters,
        ) or "",
    })


def apply_form_link_results(
    forms: list[FormSpec],
    link_results: list[FormLinkResult],
    catalog: EntityCatalog,
    *,
    auto_apply_only_high_confidence: bool = False,
) -> tuple[list[FormSpec], list[str], list[str], list[FormLinkResult]]:
    """Stamp validated link results onto FormSpec list.

    Returns:
        linked_forms: forms that survived (junk-classified forms dropped when
            auto-applied; junk forms held for review stay in the list).
        dropped_sheet_names: sheet names auto-applied as junk.
        warnings: rejected validations + sheets the LLM didn't classify.
        deferred_results: results held back for HITL review when
            `auto_apply_only_high_confidence=True`. Empty otherwise.

    The function is idempotent — running it twice on the same inputs yields
    the same outputs.
    """
    by_sheet: dict[str, FormLinkResult] = {
        _norm(r.sheet_name): r for r in link_results
    }

    linked_forms: list[FormSpec] = []
    dropped: list[str] = []
    warnings: list[str] = []
    deferred: list[FormLinkResult] = []

    for form in forms:
        key = _norm(form.name)
        result = by_sheet.get(key)

        if result is None:
            warnings.append(
                f"form sheet {form.name!r} not classified by LLM; "
                f"keeping parser defaults"
            )
            linked_forms.append(form)
            continue

        ok, err = validate_result(result, catalog)
        if not ok:
            warnings.append(
                f"form sheet {form.name!r}: classification rejected ({err}); "
                f"keeping parser defaults"
            )
            linked_forms.append(form)
            continue

        # HITL gate: review-needed results are deferred — the form keeps its
        # parser default until the user resolves the card.
        needs_review = auto_apply_only_high_confidence and (
            result.confidence != "high"
            or (result.form_type is None and is_form_like_sheet_name(form.name))
        )
        if needs_review:
            deferred.append(result)
            linked_forms.append(form)
            continue

        if result.form_type is None:
            dropped.append(form.name)
            continue

        linked_forms.append(_stamp_form(form, result, catalog))

    return linked_forms, dropped, warnings, deferred


def apply_review_decisions(
    forms: list[FormSpec],
    deferred: list[FormLinkResult],
    catalog: EntityCatalog,
    resolutions: dict[str, str],
) -> tuple[list[FormSpec], list[str], list[str]]:
    """Apply HITL resolutions to deferred form-link results.

    Each resolution keyed by sheet name (case-insensitive) is one of:
        "accept"        — apply the LLM's classification as proposed
        "skip"          — drop the form sheet from the bundle (treat as junk)
        "keep_default"  — leave the form with parser defaults

    Anything else is treated as "keep_default" with a warning.

    Returns (linked_forms, dropped_sheet_names, warnings).
    """
    by_sheet: dict[str, FormLinkResult] = {
        _norm(r.sheet_name): r for r in deferred
    }
    normalized_resolutions: dict[str, str] = {
        _norm(k): (v or "").strip().lower() for k, v in resolutions.items()
    }

    linked_forms: list[FormSpec] = []
    dropped: list[str] = []
    warnings: list[str] = []

    for form in forms:
        key = _norm(form.name)
        result = by_sheet.get(key)
        if result is None:
            linked_forms.append(form)
            continue

        decision = normalized_resolutions.get(key, "keep_default")

        if decision == "accept":
            if result.form_type is None:
                dropped.append(form.name)
                continue
            linked_forms.append(_stamp_form(form, result, catalog))
            continue

        if decision == "skip":
            dropped.append(form.name)
            continue

        if decision != "keep_default":
            warnings.append(
                f"form sheet {form.name!r}: unknown resolution "
                f"{decision!r}; keeping parser defaults"
            )
        linked_forms.append(form)

    return linked_forms, dropped, warnings


def build_pending_cards(
    deferred: list[FormLinkResult], orphans: list[str],
) -> list[dict]:
    """Turn deferred results + orphan strings into HITL card dicts.

    The shape is intentionally simple so the chat / web client can render
    them without a typed schema dependency.
    """
    cards: list[dict] = []
    for r in deferred:
        kind = (
            "form_link_junk_review"
            if r.form_type is None
            else "form_link_low_confidence"
        )
        cards.append({
            "card_id": f"form-link/{r.sheet_name}",
            "kind": kind,
            "sheet_name": r.sheet_name,
            "proposed": {
                "form_type": r.form_type,
                "subject_type": r.subject_type,
                "program": r.program,
                "encounter_type": r.encounter_type,
            },
            "confidence": r.confidence,
            "reasoning": r.reasoning,
        })
    for line in orphans:
        cards.append({
            "card_id": f"form-link/orphan/{line}",
            "kind": "form_link_orphan",
            "message": line,
        })
    return cards


# ── Orphan detection ──────────────────────────────────────────────────────────


def orphan_entities(
    catalog: EntityCatalog, link_results: list[FormLinkResult],
) -> list[str]:
    """Entities declared in the modelling doc that no link result claims.

    The output is a list of human-readable lines, one per orphan; intended
    for the HITL pause surface in phase 6.
    """
    claimed_subjects: set[str] = set()
    claimed_programs_for_enrolment: set[str] = set()
    claimed_programs_for_exit: set[str] = set()
    claimed_program_encounters: set[tuple[str, str]] = set()
    claimed_encounters: set[str] = set()

    for r in link_results:
        if r.form_type == "IndividualProfile" and r.subject_type:
            claimed_subjects.add(_norm(r.subject_type))
        elif r.form_type == "ProgramEnrolment" and r.program:
            claimed_programs_for_enrolment.add(_norm(r.program))
        elif r.form_type == "ProgramExit" and r.program:
            claimed_programs_for_exit.add(_norm(r.program))
        elif r.form_type == "ProgramEncounter" and r.encounter_type and r.program:
            claimed_program_encounters.add((_norm(r.encounter_type), _norm(r.program)))
        elif r.form_type == "Encounter" and r.encounter_type:
            claimed_encounters.add(_norm(r.encounter_type))

    orphans: list[str] = []
    for name, _ in catalog.subject_types:
        if _norm(name) not in claimed_subjects:
            orphans.append(f"Subject Type {name!r} has no registration form")
    for name, hints in catalog.programs:
        if _norm(name) not in claimed_programs_for_enrolment:
            orphans.append(f"Program {name!r} has no enrolment form")
        if hints.get("exit_form_source") and _norm(name) not in claimed_programs_for_exit:
            orphans.append(f"Program {name!r} has no exit form")
    for name, hints in catalog.program_encounters:
        prog = hints.get("program")
        if (
            prog
            and (_norm(name), _norm(prog)) not in claimed_program_encounters
        ):
            orphans.append(f"Program Encounter {name!r} (under {prog!r}) has no form")
    for name, _ in catalog.encounters:
        if _norm(name) not in claimed_encounters:
            orphans.append(f"Encounter {name!r} has no form")
    return orphans


# ── Prompt builder ────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You match form sheets to entity declarations in an Avni bundle. The
modelling document declares which subjects, programs, and encounters exist;
the forms document contains the sheets to classify. For each sheet name,
decide which declared entity it represents, or return null entity fields
if the sheet is not a form (junk, scratch, metadata).

Rules:
- The entity must appear in the catalog. Never invent entries.
- Determine formType from which tab the entity was declared in:
    Subject Type tab + registration form → IndividualProfile
    Program tab + enrolment form         → ProgramEnrolment
    Program tab + exit form              → ProgramExit
    Program Encounters tab + main form   → ProgramEncounter
    Program Encounters tab + cancellation → ProgramEncounterCancellation
                                            (subject_type + program + encounter_type
                                             inherited from the parent encounter row)
    Encounters tab + main form           → Encounter
    Encounters tab + cancellation        → IndividualEncounterCancellation
                                            (subject_type + encounter_type inherited from
                                             the parent row; program is null)
  Cancellation sheets are identified by a sheet name ending in "Cancellation"
  / "cancellation" or by matching a non-empty "Cancellation Form" column on
  the parent row.
- Use the form-source column verbatim only as a hint — operators sometimes
  name a file there ("Forms EKAM 31st July 2025") and sometimes a specific
  sheet ("ANC Cancellation"). The sheet name itself is the authoritative target.
- Confidence:
    high   — sheet name matches a declared entity unambiguously
    medium — match required handling spacing/typo/truncation, but the intent
             is clear from context
    low    — multiple plausible entities or no clear match
"""


def build_prompt(catalog: EntityCatalog, sheet_names: list[str]) -> tuple[str, str]:
    """Render (system_prompt, user_prompt) for the classification call."""
    lines: list[str] = ["ENTITIES (from modelling doc):", ""]

    lines.append("  Subject Types:")
    for name, hints in catalog.subject_types:
        src = hints.get("registration_form_source")
        suffix = f' (registration form source: "{src}")' if src else ""
        type_hint = f" [{hints.get('type')}]" if hints.get("type") else ""
        lines.append(f"    - {name}{type_hint}{suffix}")
    lines.append("")

    lines.append("  Programs:")
    for name, hints in catalog.programs:
        bits = []
        if hints.get("target_subject_type"):
            bits.append(f"target: {hints['target_subject_type']}")
        if hints.get("enrolment_form_source"):
            bits.append(f'enrolment: "{hints["enrolment_form_source"]}"')
        if hints.get("exit_form_source"):
            bits.append(f'exit: "{hints["exit_form_source"]}"')
        suffix = f"   ({'   '.join(bits)})" if bits else ""
        lines.append(f"    - {name}{suffix}")
    lines.append("")

    lines.append("  Program Encounters:")
    for name, hints in catalog.program_encounters:
        bits = []
        if hints.get("program"):
            bits.append(f"under {hints['program']}")
        if hints.get("form_source"):
            bits.append(f'form: "{hints["form_source"]}"')
        if hints.get("cancellation_form_source"):
            bits.append(f'cancellation: "{hints["cancellation_form_source"]}"')
        suffix = f"   ({'   '.join(bits)})" if bits else ""
        lines.append(f"    - {name}{suffix}")
    lines.append("")

    lines.append("  Encounters (standalone):")
    for name, hints in catalog.encounters:
        bits = []
        if hints.get("subject_type"):
            bits.append(f"under {hints['subject_type']}")
        if hints.get("form_source"):
            bits.append(f'form: "{hints["form_source"]}"')
        if hints.get("cancellation_form_source"):
            bits.append(f'cancellation: "{hints["cancellation_form_source"]}"')
        suffix = f"   ({'   '.join(bits)})" if bits else ""
        lines.append(f"    - {name}{suffix}")
    lines.append("")

    lines.append("SHEET NAMES (from forms doc):")
    for s in sheet_names:
        lines.append(f"  - {s!r}")
    lines.append("")
    lines.append(
        "Return a JSON array, one object per sheet, in the input order above."
    )

    return _SYSTEM_PROMPT, "\n".join(lines)


# ── Orchestrator (LLM call is plugged in phase 3) ─────────────────────────────


def classify_forms(
    forms: list[FormSpec],
    entity_spec: EntitySpec,
    sheet_names: list[str],
    llm_helper,
) -> tuple[list[FormLinkResult], list[str]]:
    """Return (link_results, warnings).

    Falls through to an empty result list (no-op) when the LLM helper is
    unavailable or raises — caller treats that as "keep parser defaults".
    """
    if not forms or not sheet_names:
        return [], []
    if not llm_helper.is_available():
        return [], ["LLM helper unavailable; skipping form-link classification"]

    catalog = build_entity_catalog(entity_spec)
    system, user = build_prompt(catalog, sheet_names)

    log.info("[link] classifying %d sheets against catalog "
             "(%d subjects, %d programs, %d prog-encs, %d encs)",
             len(sheet_names), len(catalog.subject_types),
             len(catalog.programs), len(catalog.program_encounters),
             len(catalog.encounters))
    try:
        response = llm_helper.classify(system, user, FormLinkResults)
    except Exception as exc:  # noqa: BLE001
        log.warning("[link] LLM call failed: %s", exc)
        return [], [f"LLM form-link classification failed: {exc}"]

    log.info("[link] received %d classifications", len(response.results))
    return response.results, []

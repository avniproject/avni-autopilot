"""Form-to-entity linking via LLM classification.

A single Claude Haiku pass matches every form sheet in the parser's output
to its declared entity in the modelling doc (subject types, programs,
program encounters, encounters). Replaces the deterministic keyword
ladder formerly in `parser._resolve_form_subject_types`.

Validated classifications auto-apply unconditionally. Sheets the LLM marks
as junk (`form_type=None`) are dropped from the bundle. Off-pool or shape-
invalid results fall back to parser defaults with a warning. Declared
entities that no sheet claims surface as orphan warnings on the run output.

Public surface:

  classify_forms(forms, entity_spec, sheet_names, llm_helper)
      Returns (link_results, warnings).

  apply_form_link_results(forms, link_results, catalog)
      Returns a `LinkApplyOutcome` (linked_forms, dropped, warnings).

  build_entity_catalog(entity_spec)
      Pure conversion from EntitySpec → EntityCatalog (prompt + validator).

  validate_result(result, catalog)
      Per-row validation: pool membership + formType reference consistency.

  orphan_entities(catalog, link_results)
      Catalog entities no result claims, as human-readable lines.

See specs/FORM_ENTITY_LINKING_SDD.md.
"""

from __future__ import annotations

import logging
from itertools import chain
from typing import Callable, Iterable, Iterator, Literal, NamedTuple

from pydantic import BaseModel, Field

from models import EntitySpec, FormSpec

log = logging.getLogger(__name__)


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
    sheet_name: str
    form_type: FormTypeLiteral | None = None
    subject_type: str | None = None
    program: str | None = None
    encounter_type: str | None = None
    confidence: Literal["high", "medium", "low"] = "low"
    reasoning: str = ""


class FormLinkResults(BaseModel):
    """Container for the batched classification output.

    `with_structured_output` binds to a single model — wrapping the list in
    this container lets one Haiku call return all sheet classifications.
    """
    results: list[FormLinkResult]


class LinkApplyOutcome(NamedTuple):
    linked_forms: list[FormSpec]
    dropped: list[str]
    warnings: list[str]


# ── Form-type consistency table ───────────────────────────────────────────────


# Required-reference signature per formType: which of
# (subject_type, program, encounter_type) must be non-null. A table-driven
# check keeps the seven shapes uniform — single tuple comparison covers all.
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

    Each pool entry is `(canonical_name, hint_dict)`. The hint dict carries
    operator wording (raw form-source columns, cross-refs) without polluting
    the pool keys the LLM must return verbatim.
    """

    subject_types: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)
    programs: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)
    program_encounters: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)
    encounters: list[tuple[str, dict[str, str | None]]] = Field(default_factory=list)

    def all_encounters(self) -> Iterator[tuple[str, dict[str, str | None]]]:
        return chain(self.program_encounters, self.encounters)


def build_entity_catalog(entity_spec: EntitySpec) -> EntityCatalog:
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


# ── Pool lookup ───────────────────────────────────────────────────────────────


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _lookup(
    name: str | None, pool: Iterable[tuple[str, dict]],
) -> str | None:
    """Canonical spelling for `name` in `pool`, or None on miss.

    `None` input returns `None` (absent-is-allowed). `None` from a non-`None`
    input means off-pool — validator and stamp both treat that as a failure.
    """
    if name is None:
        return None
    key = _norm(name)
    for canonical, _ in pool:
        if _norm(canonical) == key:
            return canonical
    return None


# ── Validator ─────────────────────────────────────────────────────────────────


def validate_result(
    result: FormLinkResult, catalog: EntityCatalog,
) -> tuple[bool, str]:
    """Return (ok, error_message). Error is empty when ok."""
    if result.form_type is None:
        if result.subject_type or result.program or result.encounter_type:
            return False, (
                "form_type is null but at least one entity reference is set; "
                "junk classifications must have all entity fields null"
            )
        return True, ""

    for field, value, pool in (
        ("subject_type", result.subject_type, catalog.subject_types),
        ("encounter_type", result.encounter_type, catalog.all_encounters()),
        ("program", result.program, catalog.programs),
    ):
        if value is not None and _lookup(value, pool) is None:
            label = field.replace("_", " ") + " pool"
            return False, f"{field} {value!r} not in {label.replace(' pool', '')} pool"

    needs = _REQUIRED_REFS[result.form_type]
    actuals = (
        result.subject_type is not None,
        result.program is not None,
        result.encounter_type is not None,
    )
    for field, need, actual in zip(
        ("subject_type", "program", "encounter_type"), needs, actuals,
    ):
        if need != actual:
            return False, (
                f"{result.form_type}: {field} "
                f"{'required' if need else 'must be null'}"
            )
    return True, ""


# ── Application ───────────────────────────────────────────────────────────────


def _stamp_form(
    form: FormSpec, result: FormLinkResult, catalog: EntityCatalog,
) -> FormSpec:
    return form.model_copy(update={
        "formType": result.form_type,
        "subjectType": _lookup(result.subject_type, catalog.subject_types) or "",
        "program": _lookup(result.program, catalog.programs) or "",
        "encounterType": _lookup(result.encounter_type, catalog.all_encounters()) or "",
    })


def apply_form_link_results(
    forms: list[FormSpec],
    link_results: list[FormLinkResult],
    catalog: EntityCatalog,
) -> LinkApplyOutcome:
    """Stamp every validated link result onto the FormSpec list.

    Idempotent. Junk classifications drop the sheet. Validation failures
    leave the form at parser defaults with a warning.
    """
    by_sheet = {_norm(r.sheet_name): r for r in link_results}

    linked: list[FormSpec] = []
    dropped: list[str] = []
    warnings: list[str] = []

    for form in forms:
        result = by_sheet.get(_norm(form.name))

        if result is None:
            warnings.append(
                f"form sheet {form.name!r} not classified by LLM; "
                f"keeping parser defaults"
            )
            linked.append(form)
            continue

        ok, err = validate_result(result, catalog)
        if not ok:
            warnings.append(
                f"form sheet {form.name!r}: classification rejected ({err}); "
                f"keeping parser defaults"
            )
            linked.append(form)
            continue

        if result.form_type is None:
            dropped.append(form.name)
            continue

        linked.append(_stamp_form(form, result, catalog))

    return LinkApplyOutcome(linked, dropped, warnings)


# ── Orphan detection ──────────────────────────────────────────────────────────


# How each form_type claims an entity from the catalog. The key function
# returns the catalog-side identity that line consumes (a name, or a
# (name, program) tuple for program encounters); `None` means "this result
# doesn't claim anything for the given kind."
_CLAIM_BY_FORM_TYPE: dict[str, tuple[str, Callable[[FormLinkResult], object | None]]] = {
    "IndividualProfile":
        ("subject", lambda r: _norm(r.subject_type) if r.subject_type else None),
    "ProgramEnrolment":
        ("program_enrolment", lambda r: _norm(r.program) if r.program else None),
    "ProgramExit":
        ("program_exit", lambda r: _norm(r.program) if r.program else None),
    "ProgramEncounter":
        ("program_encounter",
         lambda r: (_norm(r.encounter_type), _norm(r.program))
                   if r.encounter_type and r.program else None),
    "Encounter":
        ("encounter", lambda r: _norm(r.encounter_type) if r.encounter_type else None),
}


def orphan_entities(
    catalog: EntityCatalog, link_results: list[FormLinkResult],
) -> list[str]:
    """Catalog entities that no link result claims, as human-readable lines."""
    claimed: dict[str, set] = {
        "subject": set(),
        "program_enrolment": set(),
        "program_exit": set(),
        "program_encounter": set(),
        "encounter": set(),
    }
    for r in link_results:
        spec = _CLAIM_BY_FORM_TYPE.get(r.form_type or "")
        if spec is None:
            continue
        bucket, key_fn = spec
        key = key_fn(r)
        if key is not None:
            claimed[bucket].add(key)

    orphans: list[str] = []
    for name, _ in catalog.subject_types:
        if _norm(name) not in claimed["subject"]:
            orphans.append(f"Subject Type {name!r} has no registration form")
    for name, hints in catalog.programs:
        if _norm(name) not in claimed["program_enrolment"]:
            orphans.append(f"Program {name!r} has no enrolment form")
        if hints.get("exit_form_source") and _norm(name) not in claimed["program_exit"]:
            orphans.append(f"Program {name!r} has no exit form")
    for name, hints in catalog.program_encounters:
        prog = hints.get("program")
        if prog and (_norm(name), _norm(prog)) not in claimed["program_encounter"]:
            orphans.append(f"Program Encounter {name!r} (under {prog!r}) has no form")
    for name, _ in catalog.encounters:
        if _norm(name) not in claimed["encounter"]:
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


# Per-section formatters: each tuple is (hint_key, label-format-string).
# The format string takes a single positional arg — the hint value. Sections
# render any subset of their formatters that have non-empty hint values.
_SECTION_FORMATTERS: dict[str, list[tuple[str, str]]] = {
    "Programs": [
        ("target_subject_type", "target: {}"),
        ("enrolment_form_source", 'enrolment: "{}"'),
        ("exit_form_source", 'exit: "{}"'),
    ],
    "Program Encounters": [
        ("program", "under {}"),
        ("form_source", 'form: "{}"'),
        ("cancellation_form_source", 'cancellation: "{}"'),
    ],
    "Encounters (standalone)": [
        ("subject_type", "under {}"),
        ("form_source", 'form: "{}"'),
        ("cancellation_form_source", 'cancellation: "{}"'),
    ],
}


def _render_subject_types(
    items: list[tuple[str, dict[str, str | None]]],
) -> list[str]:
    """Subject Types has a one-off `[type]` suffix the generic renderer lacks."""
    out: list[str] = ["  Subject Types:"]
    for name, hints in items:
        type_hint = f" [{hints.get('type')}]" if hints.get("type") else ""
        src = hints.get("registration_form_source")
        suffix = f' (registration form source: "{src}")' if src else ""
        out.append(f"    - {name}{type_hint}{suffix}")
    out.append("")
    return out


def _render_section(
    label: str,
    items: list[tuple[str, dict[str, str | None]]],
    formatters: list[tuple[str, str]],
) -> list[str]:
    out: list[str] = [f"  {label}:"]
    for name, hints in items:
        bits = [fmt.format(hints[key]) for key, fmt in formatters if hints.get(key)]
        suffix = f"   ({'   '.join(bits)})" if bits else ""
        out.append(f"    - {name}{suffix}")
    out.append("")
    return out


def build_prompt(catalog: EntityCatalog, sheet_names: list[str]) -> tuple[str, str]:
    """Render (system_prompt, user_prompt) for the classification call."""
    lines: list[str] = ["ENTITIES (from modelling doc):", ""]
    lines.extend(_render_subject_types(catalog.subject_types))
    lines.extend(_render_section("Programs", catalog.programs,
                                 _SECTION_FORMATTERS["Programs"]))
    lines.extend(_render_section("Program Encounters", catalog.program_encounters,
                                 _SECTION_FORMATTERS["Program Encounters"]))
    lines.extend(_render_section("Encounters (standalone)", catalog.encounters,
                                 _SECTION_FORMATTERS["Encounters (standalone)"]))

    lines.append("SHEET NAMES (from forms doc):")
    for s in sheet_names:
        lines.append(f"  - {s!r}")
    lines.append("")
    lines.append(
        "Return a JSON array, one object per sheet, in the input order above."
    )
    return _SYSTEM_PROMPT, "\n".join(lines)


# ── Orchestrator ──────────────────────────────────────────────────────────────


def classify_forms(
    forms: list[FormSpec],
    entity_spec: EntitySpec,
    sheet_names: list[str],
    llm_helper,
) -> tuple[list[FormLinkResult], list[str]]:
    """Run one Haiku call to classify every form sheet. Returns (results, warnings).

    No-op on empty inputs or when the helper is unavailable; the caller
    treats that as "keep parser defaults".
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

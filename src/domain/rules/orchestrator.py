"""Bundle-wide rule generation: collect → generate concurrently → apply.

Bridges the pipeline node and the single-call `RuleGenerator`:

- `collect_pending_rules` walks an `EntitySpec` and builds one `RuleSpec`
  per form-level intent and per field-level intent.
- `generate_all` retrieves KB context for every spec in one batch, then
  runs every generation call in a bounded thread pool. Worker tasks only
  call the generator (`ChatAnthropic.invoke` is a thread-safe blocking
  HTTP call) and return results; nothing shared is mutated across
  threads.
- `apply_results` validates each result and writes accepted JS into the
  bundle's form JSON, sequentially and in collection order, so warnings
  and writes stay deterministic.

Warnings are namespaced `rules.<kind>.<form>[.<section>.<field>]: <reason>`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from domain.rules.concept_answers import merge_answer_scopes
from domain.rules.generator import RuleGenerator
from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec
from domain.rules.validator import validate_and_decide

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingFormRule:
    """One form-level rule intent awaiting generation."""

    form_spec: Any
    kind_value: str
    rule_spec: RuleSpec

    @property
    def namespace(self) -> str:
        return f"rules.{self.kind_value}.{self.form_spec.name}"


@dataclass(frozen=True)
class PendingFieldRule:
    """One field-level rule intent awaiting generation."""

    form_spec: Any
    section_name: str
    field_name: str
    rule_spec: RuleSpec

    @property
    def namespace(self) -> str:
        return (
            f"rules.{RuleKind.FORM_ELEMENT.value}."
            f"{self.form_spec.name}.{self.section_name}.{self.field_name}"
        )


@dataclass
class GenerationOutcome:
    """Results of `generate_all`, aligned index-for-index with the pending lists.

    A `None` result means that entry's worker failed; the failure is
    already recorded in `warnings`.
    """

    form_results: list[RuleResult | None]
    field_results: list[RuleResult | None]
    warnings: list[str] = field(default_factory=list)


def collect_pending_rules(
    entity_spec: Any,
    available_encounter_types: list[str],
    available_programs: list[str],
) -> tuple[list[PendingFormRule], list[PendingFieldRule], list[str]]:
    """Build a `RuleSpec` for every rule intent in the bundle.

    Unknown form-level kind values are skipped with a warning.
    """
    warnings: list[str] = []
    pending_form: list[PendingFormRule] = []
    pending_field: list[PendingFieldRule] = []

    for form_spec in entity_spec.forms:
        for kind_value, intent in (form_spec.rule_intents or {}).items():
            try:
                kind = RuleKind(kind_value)
            except ValueError:
                warnings.append(
                    f"rules.{kind_value}.{form_spec.name}: "
                    f"unknown rule kind, skipped"
                )
                continue
            rule_spec = _build_rule_spec(
                form_spec, kind, intent,
                available_encounter_types, available_programs,
                all_forms=entity_spec.forms,
            )
            pending_form.append(PendingFormRule(form_spec, kind_value, rule_spec))

    for form_spec in entity_spec.forms:
        for section in (form_spec.sections or []):
            for form_field in (section.fields or []):
                if not form_field.rule_intent:
                    continue
                rule_spec = _build_rule_spec(
                    form_spec, RuleKind.FORM_ELEMENT, form_field.rule_intent,
                    available_encounter_types, available_programs,
                    all_forms=entity_spec.forms,
                )
                pending_field.append(PendingFieldRule(
                    form_spec, section.name, form_field.name, rule_spec,
                ))

    return pending_form, pending_field, warnings


def generate_all(
    generator: RuleGenerator,
    pending_form: list[PendingFormRule],
    pending_field: list[PendingFieldRule],
    max_workers: int,
) -> GenerationOutcome:
    """Generate every pending rule concurrently.

    KB retrieval for all specs happens in ONE Voyage request first —
    critical for free-tier users (3 RPM) so N rules don't burn N
    rate-limit slots. Form-level rules are one call each (each kind has
    its own system prompt); field-level rules are one batched call per
    form. A worker failure yields a `None` result and a namespaced
    warning for the affected entries only.
    """
    outcome = GenerationOutcome(
        form_results=[None] * len(pending_form),
        field_results=[None] * len(pending_field),
    )

    all_specs = (
        [p.rule_spec for p in pending_form]
        + [p.rule_spec for p in pending_field]
    )
    try:
        contexts = generator.kb.retrieve_batch(all_specs)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"KB batch retrieve failed: {exc}")
        outcome.warnings.append(f"rules: batch retrieval failed ({exc})")
        contexts = [None] * len(all_specs)

    form_contexts = contexts[: len(pending_form)]
    field_contexts = contexts[len(pending_form):]

    field_indices_by_form: dict[str, list[int]] = {}
    for i, pending in enumerate(pending_field):
        field_indices_by_form.setdefault(pending.form_spec.name, []).append(i)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        form_futures = {
            pool.submit(generator.generate, p.rule_spec, context=ctx): i
            for i, (p, ctx) in enumerate(zip(pending_form, form_contexts))
        }
        field_futures = {}
        for form_name, indices in field_indices_by_form.items():
            batch_input = [
                (pending_field[i].field_name, pending_field[i].section_name,
                 pending_field[i].rule_spec, field_contexts[i])
                for i in indices
            ]
            field_futures[
                pool.submit(generator.generate_field_batch, batch_input)
            ] = indices

        for future, i in form_futures.items():
            pending = pending_form[i]
            try:
                outcome.form_results[i] = future.result()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    f"Rule generation worker failed for form "
                    f"{pending.form_spec.name!r} kind {pending.kind_value!r}: {exc}"
                )
                outcome.warnings.append(
                    f"{pending.namespace}: generation worker failed: {exc}"
                )
        for future, indices in field_futures.items():
            try:
                results = future.result()
            except Exception as exc:  # noqa: BLE001
                form_name = pending_field[indices[0]].form_spec.name
                log.warning(
                    f"Field batch worker failed for form {form_name!r}: {exc}"
                )
                outcome.warnings.extend(
                    f"{pending_field[i].namespace}: generation worker failed: {exc}"
                    for i in indices
                )
                continue
            for i, result in zip(indices, results):
                outcome.field_results[i] = result

    return outcome


def apply_results(
    pending_form: list[PendingFormRule],
    pending_field: list[PendingFieldRule],
    outcome: GenerationOutcome,
    forms_json: list[dict],
) -> tuple[int, int, list[str]]:
    """Validate every result and write accepted JS into the form JSON.

    Returns (form-level written, field-level written, warnings). A `None`
    result is skipped — its worker failure is already in the outcome's
    warnings.
    """
    warnings: list[str] = []
    forms_by_name: dict[str, dict] = {
        entry["content"]["name"]: entry for entry in forms_json
    }
    written_form = 0
    written_field = 0

    for pending, result in zip(pending_form, outcome.form_results):
        if result is None:
            continue
        ok, raw_warnings = validate_and_decide(result, pending.rule_spec)
        log.info(
            "Rule path=single form=%s kind=%s written=%s",
            pending.form_spec.name, pending.kind_value, ok,
        )
        warnings.extend(f"{pending.namespace}: {w}" for w in raw_warnings)
        if not ok:
            continue

        target = forms_by_name.get(pending.form_spec.name)
        if target is None:
            warnings.append(
                f"{pending.namespace}: form JSON not found in state; cannot write"
            )
            continue
        target["content"][pending.kind_value] = result.js
        written_form += 1

    for pending, result in zip(pending_field, outcome.field_results):
        if result is None:
            continue
        ok, raw_warnings = validate_and_decide(result, pending.rule_spec)
        log.info(
            "Rule path=field_batch form=%s field=%s written=%s",
            pending.form_spec.name, pending.field_name, ok,
        )
        warnings.extend(f"{pending.namespace}: {w}" for w in raw_warnings)
        if not ok:
            continue

        target = forms_by_name.get(pending.form_spec.name)
        if target is None:
            warnings.append(
                f"{pending.namespace}: form JSON not found in state; cannot write"
            )
            continue
        form_element = _find_form_element_in_json(
            target["content"], pending.section_name, pending.field_name,
        )
        if form_element is None:
            warnings.append(
                f"{pending.namespace}: matching form element not found in form "
                f"JSON; the field may have been deduped or renamed by the "
                f"generator"
            )
            continue
        form_element["rule"] = result.js
        written_field += 1

    return written_form, written_field, warnings


def _find_form_element_in_json(
    form_content: dict, section_name: str, field_name: str,
) -> dict | None:
    """Locate one ``formElement`` dict by (section, field) names.

    Returns None when no matching section or field is present.
    """
    for group in form_content.get("formElementGroups") or []:
        if group.get("name") != section_name:
            continue
        for element in group.get("formElements") or []:
            if element.get("name") == field_name:
                return element
    return None


def _build_rule_spec(
    form_spec: Any,
    kind: RuleKind,
    intent: str,
    available_encounter_types: list[str],
    available_programs: list[str],
    all_forms: list[Any] | None = None,
) -> RuleSpec:
    """Compose a RuleSpec from a FormSpec and the bundle's vocabulary.

    `all_forms` is the full list of `FormSpec` in the bundle. Both
    `available_concepts` and `concept_answers` span the in-scope forms —
    target plus its registration and enrolment siblings — so a rule on a
    program encounter (e.g. ANC) can reference enrolment fields like
    `Last Menstrual Period (LMP)` reachable at runtime via the entity
    chain. Mirrors CONCEPT_ANSWER_GROUNDING_SDD.md §5.
    """
    in_scope_forms = _forms_in_scope_for(form_spec, all_forms or [form_spec])
    available_concepts = sorted({
        form_field.name
        for form in in_scope_forms
        for section in (form.sections or [])
        for form_field in (section.fields or [])
        if form_field.name
    })
    concept_answers = _collect_concept_answers(in_scope_forms)
    return RuleSpec(
        rule_kind=kind,
        intent=intent,
        form_name=form_spec.name,
        form_type=form_spec.formType,
        subject_type=form_spec.subjectType,
        program=form_spec.program,
        encounter_type=form_spec.encounterType,
        available_concepts=available_concepts,
        available_encounter_types=available_encounter_types,
        available_programs=available_programs,
        concept_answers=concept_answers,
    )


# Form types that participate in a program (so an enrolment form is in
# scope for cross-form answer grounding). Mirrors generators._PROGRAM_FORM_TYPES.
_PROGRAM_FORM_TYPES_FOR_GROUNDING: frozenset[str] = frozenset({
    "ProgramEnrolment", "ProgramExit",
    "ProgramEncounter", "ProgramEncounterCancellation",
})


def _forms_in_scope_for(target: Any, all_forms: list[Any]) -> list[Any]:
    """Return target + its registration form + its enrolment form (when applicable).

    Matches the resolution table in CONCEPT_ANSWER_GROUNDING_SDD.md §5: a rule
    can ground its answer literals against any coded concept the rule can
    actually reach at runtime via `programEncounter.programEnrolment.individual`.
    """
    in_scope: list[Any] = [target]
    target_type = getattr(target, "formType", "")

    if target_type in _PROGRAM_FORM_TYPES_FOR_GROUNDING and target.program:
        enrolment = next(
            (f for f in all_forms
             if getattr(f, "formType", "") == "ProgramEnrolment"
             and getattr(f, "program", None) == target.program
             and f is not target),
            None,
        )
        if enrolment is not None:
            in_scope.append(enrolment)

    if target_type != "IndividualProfile" and target.subjectType:
        registration = next(
            (f for f in all_forms
             if getattr(f, "formType", "") == "IndividualProfile"
             and getattr(f, "subjectType", None) == target.subjectType
             and f is not target),
            None,
        )
        if registration is not None:
            in_scope.append(registration)

    return in_scope


def _collect_concept_answers(forms: list[Any]) -> dict[str, list[str]]:
    """Collect coded-field answer lists from the target form and its siblings.

    Rule-generation context only — `forms[0]` is the target; the rest are
    its registration/enrolment siblings, included so cross-form
    references are groundable. Traversal of `FormSpec`s only; combining
    lives in `concept_answers.merge_answer_scopes`.
    """
    scopes: list[dict[str, list[str]]] = []
    for form in forms:
        scope: dict[str, list[str]] = {}
        for section in (form.sections or []):
            for form_field in (section.fields or []):
                if getattr(form_field, "dataType", None) != "Coded":
                    continue
                if form_field.name in scope:
                    continue
                options = list(getattr(form_field, "options", None) or [])
                if options:
                    scope[form_field.name] = options
        scopes.append(scope)
    return merge_answer_scopes(scopes)

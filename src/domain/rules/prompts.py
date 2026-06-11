"""Prompt templates for rule generation.

`build_system_prompt(rule_kind, entity_param)` returns the cacheable system
prompt that constrains the JS IIFE shape and the symbol allowlist. The
`build_user_prompt(spec, helpers_text, examples_text)` builds the per-call user
message with intent, bundle context, and the retrieved helpers/examples.

See specs/VISIT_SCHEDULE_RULE_SDD.md §7.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.rules.rule_spec import RuleKind, RuleSpec


@dataclass(frozen=True)
class RuleKindMeta:
    """Per-`RuleKind` properties driving the prompt and return-type rubric."""

    return_type_description: str


_KIND_META: dict[RuleKind, RuleKindMeta] = {
    RuleKind.VISIT_SCHEDULE: RuleKindMeta(
        return_type_description=(
            "an array of VisitSchedule objects produced by "
            "`scheduleBuilder.getAll()`"
        ),
    ),
    RuleKind.VALIDATION: RuleKindMeta(
        return_type_description=(
            "an array of validation error objects produced by "
            "`imports.common.createValidationError(messageKey)` — single arg. "
            "`messageKey` is either a literal error message (prototype use) or "
            "an i18n key registered in avni-webapp's translations. Return an "
            "empty array when nothing is wrong."
        ),
    ),
    RuleKind.EDIT_FORM: RuleKindMeta(
        return_type_description=(
            "an object `{ eligible: { value: boolean, message?: string } }`. "
            "`eligible.value: false` blocks editing; `message` is the reason "
            "shown to the user. A legacy `{ editable: boolean }` form is still "
            "accepted by Avni but should NOT be emitted — always produce the "
            "nested `eligible` object."
        ),
    ),
    RuleKind.DECISION: RuleKindMeta(
        return_type_description=(
            "an object `{ encounterDecisions, registrationDecisions, "
            "enrolmentDecisions }`, each a (possibly empty) array of "
            "`{ name, value }` decision entries. Most rules only touch one of "
            "the three arrays — leave the others empty."
        ),
    ),
}


# Maps the form's `formType` to the entity-param the rule's `params.entity`
# will hold at runtime. Avni's rules-config builds the function with this
# correspondence: subject-level forms receive `individual`, enrolment forms
# receive `programEnrolment`, etc.
_FORM_TYPE_TO_ENTITY_PARAM: dict[str, str] = {
    "IndividualProfile": "individual",
    "ProgramEnrolment": "programEnrolment",
    "ProgramExit": "programEnrolment",
    "ProgramEncounter": "programEncounter",
    "ProgramEncounterCancellation": "programEncounter",
    "Encounter": "encounter",
    "IndividualEncounterCancellation": "encounter",
}


def entity_param_for_form_type(form_type: str) -> str:
    """The variable name the rule reads via `params.entity` for this formType."""
    return _FORM_TYPE_TO_ENTITY_PARAM.get(form_type, "individual")


_SYSTEM_PROMPT_TEMPLATE = """\
You generate Avni rule functions written in JavaScript.

Output EXACTLY one JS IIFE matching this shape (the "use strict" directive is required):

"use strict";
({{ params, imports }}) => {{
    const {entity_param} = params.entity;
    // ... rule body ...
    return {return_expression};
}};

Hard constraints:
  1. The function MUST read `params.entity` as `{entity_param}` for this rule kind.
  2. The function MUST return {return_type_description}.
  3. Reference ONLY the helper methods listed under HELPERS in the user message.
     Do not invent helpers, methods, or namespaces.
  4. Reference ONLY the concept names listed under AVAILABLE_CONCEPTS. Concept
     names appear as the first string argument to accessors like
     `getObservationReadableValue('<concept>')` or as observation lookups.
  5. Reference ONLY the encounter type names listed under AVAILABLE_ENCOUNTER_TYPES.
     These appear as `encounterType: '<name>'` property values in
     `scheduleBuilder.add({{...}})` calls and as the first string argument to
     methods like `hasEncounterOfType('<name>')`.
  6. When using `containsAnswerConceptName(...)` or
     `containsAnyAnswerConceptName(...)`, the string argument MUST match an
     exact entry under the relevant concept in CONCEPT_ANSWERS. Never pass
     the user's informal phrasing verbatim — look up the right answer
     string. Example: if the user says "supporting family" and the concept's
     answers include "can support my family", use "can support my family".
  7. If the intent cannot be expressed within these constraints, return an
     empty `js` string and set `confidence` to "low".

Self-report `confidence` ("high" / "medium" / "low"):
  - high — intent maps cleanly to one of the EXAMPLES; every symbol used is on
    the allowlist; the return type matches.
  - medium — intent partially matches; a date offset or filter was inferred
    rather than copied from an example.
  - low — no matching example, or unable to ground every symbol.

Confidence is telemetry only. It does not gate writes; a separate validator
will reject any output that fails parsing, IIFE shape, or symbol grounding.
"""


_RETURN_EXPRESSION_BY_KIND: dict[RuleKind, str] = {
    RuleKind.VISIT_SCHEDULE: "scheduleBuilder.getAll()",
    RuleKind.VALIDATION:     "validationResults",
    RuleKind.EDIT_FORM:      "{ eligible: { value: true } }",
    RuleKind.DECISION:       "decisions",
}


def build_system_prompt(rule_kind: RuleKind, entity_param: str) -> str:
    """Render the cacheable system prompt for a rule kind + entity-param pair."""
    meta = _KIND_META[rule_kind]
    return _SYSTEM_PROMPT_TEMPLATE.format(
        entity_param=entity_param,
        return_type_description=meta.return_type_description,
        return_expression=_RETURN_EXPRESSION_BY_KIND[rule_kind],
    )


def build_user_prompt(
    spec: RuleSpec,
    helpers_text: str,
    examples_text: str,
) -> str:
    """Render the per-call user message.

    `helpers_text` and `examples_text` are pre-formatted blocks supplied by the
    knowledge base; this function just stitches them into the prompt with the
    intent + symbol allowlists. Form context (formType / subjectType / program /
    encounterType) is not re-stated here — retrieval has already scored
    examples + helpers against it via the query embedding, and `entity_param`
    in the system prompt encodes the form-type signal.
    """
    return _USER_PROMPT_TEMPLATE.format(
        intent=spec.intent,
        rule_kind=spec.rule_kind.value,
        available_concepts=_format_list(spec.available_concepts),
        available_encounter_types=_format_list(spec.available_encounter_types),
        available_programs=_format_list(spec.available_programs),
        concept_answers=_format_concept_answers(spec.concept_answers),
        helpers_text=helpers_text or "(none retrieved)",
        examples_text=examples_text or "(none retrieved)",
    )


_USER_PROMPT_TEMPLATE = """\
INTENT
{intent}

RULE_KIND
{rule_kind}

AVAILABLE_CONCEPTS
{available_concepts}

AVAILABLE_ENCOUNTER_TYPES
{available_encounter_types}

AVAILABLE_PROGRAMS
{available_programs}

CONCEPT_ANSWERS
{concept_answers}

HELPERS
{helpers_text}

EXAMPLES
{examples_text}

Generate the rule. Return a RuleResult with the JS in `js`, your self-reported
`confidence`, and the helpers / concepts / encounter types you actually used.
"""


def _format_list(items: list[str]) -> str:
    if not items:
        return "(empty)"
    return "\n".join(f"- {item}" for item in items)


def _format_concept_answers(answers: dict[str, list[str]]) -> str:
    """Render the per-concept answer allowlist for the user prompt.

    Empty when no coded concepts are in scope — the LLM then knows it
    shouldn't emit `containsAnswerConceptName(...)` at all.
    """
    if not answers:
        return "(no coded concepts on this form or its registration/enrolment)"
    lines: list[str] = []
    for concept, options in answers.items():
        lines.append(f"- {concept}")
        for opt in options:
            lines.append(f'    * "{opt}"')
    return "\n".join(lines)

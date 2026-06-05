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
  6. If the intent cannot be expressed within these constraints, return an
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
    bundle context.
    """
    return _USER_PROMPT_TEMPLATE.format(
        intent=spec.intent,
        rule_kind=spec.rule_kind.value,
        form_name=spec.form_name,
        form_type=spec.form_type,
        subject_type=spec.subject_type or "-",
        program=spec.program or "-",
        encounter_type=spec.encounter_type or "-",
        available_concepts=_format_list(spec.available_concepts),
        available_encounter_types=_format_list(spec.available_encounter_types),
        available_programs=_format_list(spec.available_programs),
        helpers_text=helpers_text or "(none retrieved)",
        examples_text=examples_text or "(none retrieved)",
    )


_USER_PROMPT_TEMPLATE = """\
INTENT
{intent}

RULE_KIND
{rule_kind}

FORM
name: {form_name}
formType: {form_type}
subjectType: {subject_type}
program: {program}
encounterType: {encounter_type}

AVAILABLE_CONCEPTS
{available_concepts}

AVAILABLE_ENCOUNTER_TYPES
{available_encounter_types}

AVAILABLE_PROGRAMS
{available_programs}

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

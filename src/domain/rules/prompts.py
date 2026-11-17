"""Prompt templates for rule generation.

`build_system_prompt(rule_kind, form_type)` returns the cacheable system
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
    RuleKind.FORM_ELEMENT: RuleKindMeta(
        return_type_description=(
            "a single `FormElementStatus` instance controlling this field's "
            "visibility, default / computed value, validation errors, and "
            "coded-answer filtering. Any subset of those behaviours may be "
            "populated in the same body — the intent text dictates which. "
            "Construct via `new imports.rulesConfig.FormElementStatus(uuid, "
            "visibility, value, answersToSkip, validationErrors, "
            "answersToShow, resetValueIfNull)` (last arg defaults to false) "
            "or via `statusBuilder.show().when.<…>.build()` "
            "(StatusBuilder also exposes `.value(...)`, `.validationError(...)`, "
            "`.skipAnswers(...)`, `.showAnswers(...)` for composition). "
            "The first arg is always `params.formElement.uuid`."
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


ENTITY_PARAMS: frozenset[str] = frozenset(_FORM_TYPE_TO_ENTITY_PARAM.values())


def entity_param_for_form_type(form_type: str) -> str:
    """The variable name the rule reads via `params.entity` for this formType."""
    return _FORM_TYPE_TO_ENTITY_PARAM.get(form_type, "individual")


# The RuleCondition method that reads an answer given on the form being filled
# in. rules-config's valueInEncounter reads `encounter`/`programEncounter`
# observations only, so on a registration, enrolment or exit form it finds
# nothing and every condition on it is false; a cancellation form's answers
# are the visit's cancel observations.
_FORM_TYPE_TO_CURRENT_FORM_VALUE_METHOD: dict[str, str] = {
    "IndividualProfile": "valueInRegistration",
    "ProgramEnrolment": "valueInEnrolment",
    "ProgramExit": "valueInExit",
    "ProgramEncounter": "valueInEncounter",
    "Encounter": "valueInEncounter",
    "ProgramEncounterCancellation": "valueInCancelEncounter",
    "IndividualEncounterCancellation": "valueInCancelEncounter",
}

# Of those, the ones that take a question group's concept as a second argument.
_VALUE_METHODS_TAKING_A_GROUP: frozenset[str] = frozenset({
    "valueInRegistration", "valueInEnrolment", "valueInEncounter",
})


def current_form_value_method(form_type: str) -> str:
    """The RuleCondition method that reads this form's own answers."""
    return _FORM_TYPE_TO_CURRENT_FORM_VALUE_METHOD.get(form_type, "valueInRegistration")


def _rule_condition_read(form_type: str, concept: str, group: str | None = None) -> str | None:
    """`<method>(concept[, group])` reading this form's answer in a RuleCondition,
    or None when the method cannot read a question inside a group."""
    method = current_form_value_method(form_type)
    if group is None:
        return f"{method}({concept})"
    if method not in _VALUE_METHODS_TAKING_A_GROUP:
        return None
    return f"{method}({concept}, {group})"


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
  1. The function MUST read `params.entity` as `{entity_param}` for this rule kind,
     and MUST pass it to every `imports.rulesConfig` builder and `RuleCondition`
     under the key `{entity_param}`, e.g.
     `new imports.rulesConfig.FormElementStatusBuilder({{{entity_param}, formElement}})`
     or `new imports.rulesConfig.RuleCondition({{{entity_param}}})`. EXAMPLES may
     come from other form types and use another key (`programEncounter`,
     `encounter`, `programEnrolment`, `individual`); never copy that key.
     In a RuleCondition or StatusBuilder chain, read an answer given on this
     form with `.when.{value_method}('<concept>')`. EXAMPLES may read with
     another `valueIn...` method; never copy it for this form's answers: on
     another form type it reads nothing and the condition is always false.
  2. The function MUST return {return_type_description}.
  3. Reference ONLY the helper methods listed under HELPERS in the user message,
     and `{value_method}` from constraint 1.
     Do not invent helpers, methods, or namespaces.
  4. Reference ONLY the concept names listed under AVAILABLE_CONCEPTS, or under
     QUESTION_GROUPS read with their group as the second argument, copied
     exactly, including case: Avni matches concept names exactly. Concept
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
     When comparing a coded answer as text (`=== '<answer>'`), read it with
     `getObservationReadableValue(...)` (`getObservationValue` returns the
     answer's UUID) and use the exact answer name.
  7. A RuleCondition or StatusBuilder chain is evaluated from the right:
     each `.and` / `.or` / `.not` applies to the whole rest of the chain, so
     `A.and.B.or.C.and.D` means A && (B || (C && D)). Never mix `.and` and
     `.or` in one chain. When the intent groups conditions with both AND and
     OR, build one condition per group, call `.matches()` on each, and
     combine the booleans with && / || exactly as the intent groups them.
  8. Express each condition exactly as the intent states it; never swap it
     for one that only seems the same. Every coded-answer check is false while
     the question is unanswered and `.not` turns that into true, so "hide
     when X is No" is `show().when.{value_method}('X').not.containsAnswerConceptName('No')`
     (visible until No is chosen), not `containsAnswerConceptName('Yes')`
     or `containsAnswerConceptNameOtherThan('No')` (both hidden while X
     is unanswered).
  9. If the intent cannot be expressed within these constraints, return an
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
    RuleKind.FORM_ELEMENT:   "statusBuilder.build()",
}


def build_system_prompt(rule_kind: RuleKind, form_type: str) -> str:
    """Render the cacheable system prompt for a rule kind + form type."""
    meta = _KIND_META[rule_kind]
    return _SYSTEM_PROMPT_TEMPLATE.format(
        entity_param=entity_param_for_form_type(form_type),
        value_method=current_form_value_method(form_type),
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
        target_field=_format_target_field(spec),
        rule_kind=spec.rule_kind.value,
        available_concepts=_format_available_concepts(spec),
        available_encounter_types=_format_list(spec.available_encounter_types),
        available_programs=_format_list(spec.available_programs),
        concept_answers=_format_concept_answers(spec.concept_answers),
        question_groups=_format_question_groups(spec),
        helpers_text=helpers_text or "(none retrieved)",
        examples_text=examples_text or "(none retrieved)",
    )


_USER_PROMPT_TEMPLATE = """\
INTENT
{intent}
{target_field}
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
{question_groups}
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


def _format_question_groups(spec: RuleSpec) -> str:
    """QUESTION_GROUPS block, or an empty string when the form has none."""
    if not spec.question_group_children and not spec.repeatable_question_groups:
        return ""
    entity_param = entity_param_for_form_type(spec.form_type)
    record_read = f"{entity_param}.getObservationReadableValue('<question concept>', '<group concept>')"
    condition_read = _rule_condition_read(spec.form_type, "'<question concept>'", "'<group concept>'")
    lines = [
        "",
        "QUESTION_GROUPS",
        "Questions inside a question group, by group concept name. Read each one with its group's",
        f"concept name as well: {record_read}"
        + (f",\nor in a RuleCondition {condition_read}." if condition_read else "."),
        "A read without the group, or a loop over observations comparing concept names, finds nothing.",
    ]
    for group, children in spec.question_group_children.items():
        lines.append(f"- {group}:")
        for child in children:
            details = [f'question on the form: "{q}"' for q in spec.question_names_by_concept.get(child, [])]
            if spec.question_group_answers.get(child):
                details.append(f"answers: {', '.join(spec.question_group_answers[child])}")
            lines.append(f"    - {child}" + (f"  ({'; '.join(details)})" if details else ""))
    if spec.repeatable_question_groups:
        lines.append(
            "Repeatable groups (their questions cannot be read by name; if the intent needs one, return an "
            "empty `js` and say why): " + ", ".join(spec.repeatable_question_groups)
        )
    return "\n".join(lines) + "\n"


def _format_target_field(spec: RuleSpec) -> str:
    """TARGET_FIELD block naming the field a form-element rule is set on, or
    an empty string when it is not known."""
    if not spec.target_field_concept:
        return ""
    entity_param = entity_param_for_form_type(spec.form_type)
    concept = spec.target_field_concept
    question = spec.target_field_question or concept
    lines = [
        "",
        "TARGET_FIELD",
        f'The rule is set on the question "{question}" (concept {concept!r}). "This field" in the intent means',
        "this question; use no other question for it.",
    ]
    group = spec.target_question_group
    record_read = f"{entity_param}.getObservationReadableValue({concept!r}" + (f", {group!r})" if group else ")")
    condition_read = _rule_condition_read(spec.form_type, repr(concept), repr(group) if group else None)
    reads = record_read + (f" or {condition_read}" if condition_read else "")
    if group:
        lines.append(f"It is inside the question group {group!r}: read its value as {reads}.")
    else:
        lines.append(f"Read its value as {reads}.")
    return "\n".join(lines) + "\n"


def _format_available_concepts(spec: RuleSpec) -> str:
    """List the concept names a rule may read, naming the question on the
    form wherever it differs, so an intent that quotes the question maps to
    the concept name the rule must use."""
    if not spec.available_concepts:
        return "(empty)"
    lines = []
    for concept in spec.available_concepts:
        questions = spec.question_names_by_concept.get(concept)
        if questions:
            asked_as = ", ".join(f'"{question}"' for question in questions)
            lines.append(f'- {concept}  (question on the form: {asked_as}; the rule must use "{concept}")')
        else:
            lines.append(f"- {concept}")
    return "\n".join(lines)


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


# ── Field-batch prompt (one Sonnet call per form) ────────────────────────────

_FIELD_BATCH_SYSTEM_PROMPT_TEMPLATE = """\
You generate Avni form-element rule functions in JavaScript.

For EACH field listed in the user message, produce a self-contained JS IIFE:

"use strict";
({{ params, imports }}) => {{
    const {entity_param} = params.entity;
    // rule body
    return statusBuilder.build();
}};

Return one item per field in `rules`, with `field_name` matching the field name exactly.

Hard constraints:
  1. `params.entity` MUST be read as `{entity_param}` in every IIFE and passed
     to every `imports.rulesConfig` builder and `RuleCondition` under the key
     `{entity_param}`, e.g.
     `new imports.rulesConfig.FormElementStatusBuilder({{{entity_param}, formElement}})`.
     EXAMPLES may come from other form types and use another key; never copy it.
     In a RuleCondition or StatusBuilder chain, read an answer given on this
     form with `.when.{value_method}('<concept>')`. EXAMPLES may read with
     another `valueIn...` method; never copy it for this form's answers: on
     another form type it reads nothing and the condition is always false.
  2. Each IIFE MUST return a `FormElementStatus` instance.
  3. Reference ONLY the helper methods listed under HELPERS, and `{value_method}`
     from constraint 1.
  4. Reference ONLY the concept names listed under AVAILABLE_CONCEPTS, copied
     exactly, including case: Avni matches concept names exactly.
  5. Reference ONLY the encounter-type names listed under AVAILABLE_ENCOUNTER_TYPES.
  6. When using `containsAnswerConceptName(...)` the string MUST match an exact
     entry under the relevant concept in CONCEPT_ANSWERS. Never use the user's
     informal phrasing — look up the right answer string.
  7. Express each condition exactly as the intent states it. Every coded-answer
     check is false while the question is unanswered and `.not` turns that
     into true, so "hide when X is No" is
     `show().when.{value_method}('X').not.containsAnswerConceptName('No')`,
     not `containsAnswerConceptName('Yes')` (hidden while X is unanswered).
  8. If a field's intent is empty, a dash, or cannot be expressed within these
     constraints, return empty `js` and set `confidence` to "low".

Self-report `confidence` per field ("high" / "medium" / "low"):
  - high   — intent maps cleanly to a known example; every symbol is grounded.
  - medium — intent partially matches; some inference was required.
  - low    — no matching example, or unable to ground every symbol.
"""

_FIELD_BATCH_USER_PROMPT_TEMPLATE = """\
FIELDS
{fields_block}

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

Generate one JS IIFE per field. Return a `rules` list with one entry per field,
`field_name` matching each field label above exactly.
"""


def build_field_batch_system_prompt(form_type: str) -> str:
    """Render the cacheable system prompt for a field-batch call."""
    return _FIELD_BATCH_SYSTEM_PROMPT_TEMPLATE.format(
        entity_param=entity_param_for_form_type(form_type),
        value_method=current_form_value_method(form_type),
    )


def build_field_batch_user_prompt(
    field_entries: list[tuple[str, str, RuleSpec]],
    helpers_text: str,
    examples_text: str,
) -> str:
    """Render the user message for a per-form field batch call.

    field_entries: list of (field_name, section_name, rule_spec). All specs
    share the same form context (concepts, encounter types, programs).
    """
    shared_spec = field_entries[0][2]
    lines: list[str] = []
    for i, (field_name, section_name, rule_spec) in enumerate(field_entries, 1):
        lines.append(f"[{i}] {field_name} (section: {section_name})")
        lines.append(f"    INTENT: {rule_spec.intent or '—'}")
        lines.append("")
    fields_block = "\n".join(lines).rstrip()

    return _FIELD_BATCH_USER_PROMPT_TEMPLATE.format(
        fields_block=fields_block,
        available_concepts=_format_available_concepts(shared_spec),
        available_encounter_types=_format_list(shared_spec.available_encounter_types),
        available_programs=_format_list(shared_spec.available_programs),
        concept_answers=_format_concept_answers(shared_spec.concept_answers),
        helpers_text=helpers_text or "(none retrieved)",
        examples_text=examples_text or "(none retrieved)",
    )

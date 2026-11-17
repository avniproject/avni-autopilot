"""JS validation for generated rule functions.

`check(result, spec)` parses the generated JS with `esprima`, verifies the IIFE
shape Avni expects, and walks the AST to confirm every concept-name and
encounter-type string the rule references is in `RuleSpec.available_*`. The
validator never executes the JS — runtime-equivalence is out of scope (see
specs/VISIT_SCHEDULE_RULE_SDD.md §2, §7.3).
"""

from __future__ import annotations

import contextvars
import logging
import re
from typing import Any

import esprima
from esprima.error_handler import Error as EsprimaError

from domain.rules.accessors import CONCEPT_ACCESSORS, ENCOUNTER_TYPE_ACCESSORS
from domain.rules.prompts import ENTITY_PARAMS, current_form_value_method, entity_param_for_form_type
from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec

log = logging.getLogger(__name__)

# Leading words of hard warnings that callers branch on.
OFF_BUNDLE_WARNING = "off-bundle"
WRONG_FORM_TYPE_WARNING = "wrong form type"
QUESTION_GROUP_WARNING = "question group read"


# ── Property keys whose Literal value is an encounter-type name ───────────────
_ENCOUNTER_TYPE_PROPERTY_KEYS: frozenset[str] = frozenset({
    "encounterType",
})

# RuleCondition chain methods used for answer grounding (§7 of
# CONCEPT_ANSWER_GROUNDING_SDD.md).
_VALUE_IN_METHODS: frozenset[str] = frozenset({
    "valueInEncounter",
    "valueInRegistration",
    "valueInEnrolment",
    "valueInEntireEnrolment",
    "valueInLastEncounter",
    "valueInExit",
    "valueInCancelEncounter",
})
# The RuleCondition method that reads a field of the form being filled in
# (prompts.current_form_value_method for the form's type). The bundle context
# lists that form's fields, so its names are grounded too; the other
# valueIn... methods read other forms and are not checked. Set by `check`.
_CURRENT_FORM_VALUE_METHOD: contextvars.ContextVar[str] = contextvars.ContextVar("_CURRENT_FORM_VALUE_METHOD")
# Methods that read the visit being filled in. A form whose own read is not
# one of these (registration, enrolment, exit) has no visit: rules-config
# finds no answer, so a condition on it never matches (valueInCancelEncounter
# throws instead).
_VISIT_VALUE_METHODS: frozenset[str] = frozenset({
    "valueInEncounter", "valueInCancelEncounter",
})
_FORM_KIND_IN_WORDS: dict[str, str] = {
    "IndividualProfile": "registration",
    "ProgramEnrolment": "programme enrolment",
    "ProgramExit": "programme exit",
}
# Accessors that read a question inside a question group when given the
# group's concept name, mapped to that argument's position (openchs-models
# AbstractEncounter, Individual, ProgramEnrolment; rules-config RuleCondition
# passes it on to findObservation).
_GROUPED_READ_PARENT_ARGUMENT: dict[str, int] = {
    "findObservation": 1,
    "getObservationValue": 1,
    "getObservationReadableValue": 1,
    "findLatestObservationFromPreviousEncounters": 2,
    "findObservationInLastEncounter": 2,
    "findObservationInEntireEnrolment": 3,
    "findLatestPreviousEncounterWithObservationForConcept": 2,
    "valueInEncounter": 1,
    "valueInRegistration": 1,
    "valueInEnrolment": 1,
    "valueInLastEncounter": 2,
}
# Of those, the ones only ProgramEnrolment's version takes the group for;
# Individual's version ignores it.
_ENROLMENT_ONLY_GROUP_METHODS: frozenset[str] = frozenset({
    "findLatestObservationFromPreviousEncounters",
    "findObservationInLastEncounter",
})
# Accessors whose concept name is not argument 0.
_CONCEPT_ARGUMENT_POSITION: dict[str, int] = {
    "findLatestPreviousEncounterWithValueForConcept": 1,
    "findLatestPreviousEncounterWithObservationForConcept": 1,
}
# Arguments that take the current encounter (or, for
# findObservationValueInEntireEnrolment, a flag), never a name; a string there
# breaks the read. RuleCondition.valueInEntireEnrolment(concept, x) passes x on
# as findObservationInEntireEnrolment's current encounter.
_NON_NAME_ARGUMENT: dict[str, tuple[int, str]] = {
    "valueInEntireEnrolment": (1, "the current encounter"),
    "getObservationReadableValueInEntireEnrolment": (1, "the current encounter"),
    "findLatestObservationInEntireEnrolment": (1, "the current encounter"),
    "findObservationInEntireEnrolment": (1, "the current encounter"),
    "findLatestObservationFromPreviousEncounters": (1, "the current encounter"),
    "findObservationInLastEncounter": (1, "the current encounter"),
    "findLatestPreviousEncounterWithValueForConcept": (0, "the current encounter"),
    "findLatestPreviousEncounterWithObservationForConcept": (0, "the current encounter"),
    "findObservationValueInEntireEnrolment": (1, "whether to look in the enrolment form too (true / false)"),
}
# Methods that exist only on ProgramEnrolment, and only on Individual.
_ENROLMENT_ONLY_METHODS: frozenset[str] = frozenset({
    "findLatestObservationInEntireEnrolment",
    "findLatestPreviousEncounterWithObservationForConcept",
    "findLatestPreviousEncounterWithValueForConcept",
    "findObservationInEntireEnrolment",
    "findObservationValueInEntireEnrolment",
    "getObservationReadableValueInEntireEnrolment",
    "getObservationsForConceptName",
})
_INDIVIDUAL_ONLY_METHODS: frozenset[str] = frozenset({
    "findObservationAcrossAllEnrolments",
})
# Concept-name reads checked against the bundle: the catalogued accessors plus
# reads that return an Observation.
_OBSERVATION_RETURNING_METHODS: frozenset[str] = frozenset({
    "findObservation",
    "findObservationInEntireEnrolment",
    "findObservationInLastEncounter",
    "findLatestObservationFromPreviousEncounters",
    "findLatestObservationInEntireEnrolment",
})
_CONCEPT_READS: frozenset[str] = CONCEPT_ACCESSORS | _OBSERVATION_RETURNING_METHODS
# Reads of the form being filled in, whose question groups the context lists.
_CURRENT_FORM_RECORD_READS: frozenset[str] = frozenset({
    "findObservation", "getObservationValue", "getObservationReadableValue",
})
# Collections a `.concept.name` comparison can scan: the record's top-level
# observations, or a question group's observations.
_TOP_LEVEL_OBSERVATIONS = "observations"
_GROUP_OBSERVATIONS = "groupObservations"

_FORM, _INDIVIDUAL, _ENROLMENT = "form", "individual", "enrolment"
# Receiver kinds for the rule being checked: variable name -> kinds, and the
# kinds of `params.entity`. Set by `check` for its helpers.
_RECEIVERS: contextvars.ContextVar[tuple[dict[str, frozenset[str]], frozenset[str]]] = contextvars.ContextVar(
    "_RECEIVERS", default=({}, frozenset({_FORM})),
)
# Accessors returning a coded answer's name, ones returning the stored value
# (a coded answer's UUID) — openchs-models Observation.getReadableValue vs
# getValue — and ones returning `{value, date}` with the readable value.
_READABLE_VALUE_METHODS: frozenset[str] = frozenset({
    "getObservationReadableValue",
    "getObservationReadableValueInEntireEnrolment",
    "findCancelEncounterObservationReadableValue",
})
_RAW_VALUE_METHODS: frozenset[str] = frozenset({
    "getObservationValue",
})
_VALUE_AND_DATE_METHODS: frozenset[str] = frozenset({
    "findObservationValueInEntireEnrolment",
})
_EQUALITY_OPERATORS: frozenset[str] = frozenset({"===", "==", "!==", "!="})
_CONTAINS_ANSWER_METHODS: frozenset[str] = frozenset({
    "containsAnswerConceptName",
    "containsAnyAnswerConceptName",
})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def validate_and_decide(
    result: RuleResult, spec: RuleSpec,
) -> tuple[bool, list[str]]:
    """Run `check` and decide whether the rule should be written.

    Combines LLM-reported warnings with validator findings and gates the
    write on validator approval AND a non-empty `result.js`. Single shared
    decision point for the pipeline (`generate_rules`) and the chat tool
    (`set_visit_schedule_rule`) — see specs/VISIT_SCHEDULE_RULE_SDD.md §7.3.

    Returns:
        (ok, warnings). When False, the caller must skip the write.
    """
    if not result.js and result.warnings:
        # The generator already said why no rule was written; "empty JS"
        # would only repeat it in technical terms.
        return False, list(result.warnings)
    check_ok, validator_warnings = check(result, spec)
    warnings = [*result.warnings, *validator_warnings]
    return (check_ok and bool(result.js)), warnings


def check(result: RuleResult, spec: RuleSpec) -> tuple[bool, list[str]]:
    """Validate a generated rule against the bundle's symbol surface.

    Returns:
        (ok, warnings). When `ok` is False, `warnings` lists the reasons (one
        per failure). When True, `warnings` is empty.
    """
    if not result.js:
        return False, ["empty JS"]

    try:
        tree = esprima.parseScript(result.js)
    except EsprimaError as exc:
        return False, [f"JS parse error: {exc}"]
    except Exception as exc:  # noqa: BLE001
        # esprima can raise non-Error exceptions on malformed input.
        log.debug(f"esprima raised non-Error exception: {exc!r}")
        return False, [f"JS parse error: {exc}"]

    arrow = _find_iife(tree)
    if arrow is None:
        return False, [
            'IIFE shape mismatch: expected `({ params, imports }) => { ... }`'
        ]

    token = _RECEIVERS.set(_receiver_kinds(tree, spec))
    method_token = _CURRENT_FORM_VALUE_METHOD.set(current_form_value_method(spec.form_type))
    try:
        return _check_parsed(tree, arrow, spec)
    finally:
        _CURRENT_FORM_VALUE_METHOD.reset(method_token)
        _RECEIVERS.reset(token)


def _check_parsed(tree: Any, arrow: Any, spec: RuleSpec) -> tuple[bool, list[str]]:
    referenced_concepts: set[str] = set()
    referenced_encounter_types: set[str] = set()
    _walk(tree, referenced_concepts, referenced_encounter_types)

    available_concepts = {name for name in spec.available_concepts if name}
    available_encounters = _lowered(spec.available_encounter_types)
    group_warnings, group_names = _check_grouped_reads(tree, spec)
    miscased = {
        name: spellings for name, spellings in _miscased_concept_names(tree, spec).items()
        if name not in group_names
    }

    bad_concepts = sorted(
        name for name in referenced_concepts
        if name not in available_concepts and name not in miscased and name not in group_names
    )
    bad_encounters = sorted(
        name for name in referenced_encounter_types
        if name.casefold() not in available_encounters
    )

    # Hard warnings (grounding failures) — these block the write.
    hard_warnings: list[str] = [
        f"{OFF_BUNDLE_WARNING} concept name {name!r}: Avni matches names exactly, "
        f"the bundle has {' or '.join(repr(actual) for actual in actuals)}"
        for name, actuals in sorted(miscased.items())
    ]
    if bad_concepts:
        hard_warnings.append(f"{OFF_BUNDLE_WARNING} concept names: {bad_concepts}")
    if bad_encounters:
        hard_warnings.append(f"{OFF_BUNDLE_WARNING} encounter types: {bad_encounters}")
    hard_warnings.extend(group_warnings)
    hard_warnings.extend(_check_receivers_and_arguments(tree, spec))
    hard_warnings.extend(_check_text_comparisons(tree, spec))
    hard_warnings.extend(_check_answer_grounding(tree, spec))
    hard_warnings.extend(_check_builder_entity_key(arrow, spec))
    hard_warnings.extend(_check_visit_reads_on_a_form_without_a_visit(tree, spec))

    # Soft warnings (return-shape mismatches) — surface to the caller but
    # do not block writes. See SDD §5.3.
    soft_warnings = _check_return_shape(tree, spec)

    return (not hard_warnings), hard_warnings + soft_warnings


def _lowered(names: list[str]) -> set[str]:
    return {n.casefold() for n in names if n}


def _concept_argument(call: Any) -> Any | None:
    """The argument holding the concept name read by an accessor call."""
    args = getattr(call, "arguments", None) or []
    position = _CONCEPT_ARGUMENT_POSITION.get(_method_name(call), 0)
    return args[position] if len(args) > position else None


def _group_argument_position(call: Any) -> int | None:
    """Position of the group argument this call honours, if any."""
    method = _method_name(call)
    if method in _ENROLMENT_ONLY_GROUP_METHODS and _is_individual(_call_receiver_kinds(call)):
        return None
    return _GROUPED_READ_PARENT_ARGUMENT.get(method)


def _receiver_kinds(tree: Any, spec: RuleSpec) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    """Which records variables in the rule hold: the form's own record
    (`params.entity`), an Individual, a ProgramEnrolment — following aliases."""
    entity_kinds = {_FORM}
    if spec.form_type == "IndividualProfile":
        entity_kinds.add(_INDIVIDUAL)
    if spec.form_type in ("ProgramEnrolment", "ProgramExit"):
        entity_kinds.add(_ENROLMENT)
    entity = frozenset(entity_kinds)
    names: dict[str, frozenset[str]] = {}
    declarators = [node for node in _iter_nodes(tree) if getattr(node, "type", None) == "VariableDeclarator"]
    for _ in range(3):
        for node in declarators:
            name = getattr(getattr(node, "id", None), "name", None)
            kinds = _expression_kinds(getattr(node, "init", None), names, entity)
            if name and kinds:
                names[name] = kinds
    return names, entity


def _expression_kinds(node: Any, names: dict[str, frozenset[str]], entity: frozenset[str]) -> frozenset[str]:
    node_type = getattr(node, "type", None)
    if node_type == "Identifier":
        name = getattr(node, "name", None)
        if name in names:
            return names[name]
        return {"individual": frozenset({_INDIVIDUAL}), "programEnrolment": frozenset({_ENROLMENT})}.get(name, frozenset())
    if node_type != "MemberExpression" or getattr(node, "computed", False):
        return frozenset()
    prop = getattr(getattr(node, "property", None), "name", None)
    owner = getattr(node, "object", None)
    if prop == "entity" and getattr(owner, "type", None) == "Identifier" and getattr(owner, "name", None) == "params":
        return entity
    return {"individual": frozenset({_INDIVIDUAL}), "programEnrolment": frozenset({_ENROLMENT})}.get(prop, frozenset())


def _call_receiver_kinds(call: Any) -> frozenset[str]:
    names, entity = _RECEIVERS.get()
    return _expression_kinds(getattr(getattr(call, "callee", None), "object", None), names, entity)


def _is_individual(kinds: frozenset[str]) -> bool:
    return _INDIVIDUAL in kinds and _ENROLMENT not in kinds


def _is_enrolment(kinds: frozenset[str]) -> bool:
    return _ENROLMENT in kinds and _INDIVIDUAL not in kinds


def _reads_current_form(call: Any) -> bool:
    """A read of the form being filled in, whose question groups are known."""
    method = _method_name(call)
    if method == _CURRENT_FORM_VALUE_METHOD.get():
        return True
    return method in _CURRENT_FORM_RECORD_READS and _FORM in _call_receiver_kinds(call)


def _check_receivers_and_arguments(tree: Any, spec: RuleSpec) -> list[str]:
    """Hard checks that a method exists on the record it is called on, takes
    the arguments given, and that a group argument is not silently ignored."""
    warnings: list[str] = []
    for call in _iter_call_expressions(tree):
        method = _method_name(call)
        args = getattr(call, "arguments", None) or []
        kinds = _call_receiver_kinds(call)
        if _is_individual(kinds) and method in _ENROLMENT_ONLY_METHODS:
            warnings.append(f"{OFF_BUNDLE_WARNING} method: {method} exists only on the programme enrolment, not on the individual")
        elif _is_enrolment(kinds) and method in _INDIVIDUAL_ONLY_METHODS:
            warnings.append(f"{OFF_BUNDLE_WARNING} method: {method} exists only on the individual, not on the programme enrolment")
        elif _is_individual(kinds) and method in _ENROLMENT_ONLY_GROUP_METHODS and len(args) > 2:
            warnings.append(
                f"{QUESTION_GROUP_WARNING}: individual.{method} ignores a question group argument; read the "
                f"question on the programme enrolment instead"
            )
        non_name = _NON_NAME_ARGUMENT.get(method)
        if non_name and len(args) > non_name[0] and _literal_string(args[non_name[0]]) is not None:
            warnings.append(
                f"{QUESTION_GROUP_WARNING}: argument {non_name[0] + 1} of {method} is {non_name[1]}, not a name, "
                f"so {_literal_string(args[non_name[0]])!r} there makes the read fail"
            )
        if method == "findLatestPreviousEncounterWithValueForConcept" and len(args) > 2:
            warnings.extend(_answer_argument_warnings(_literal_string(args[1]), _literal_string(args[2]), spec))
    return warnings


def _answer_argument_warnings(concept: str | None, answer: str | None, spec: RuleSpec) -> list[str]:
    if not concept or not answer or _UUID_RE.match(answer):
        return []
    options = _answers_by_concept(spec).get(concept)
    if options is None or answer in options:
        return []
    return [
        f"{OFF_BUNDLE_WARNING} answer(s) [{answer!r}] for concept {concept!r} — expected one of: "
        f"{options}{_case_hint(answer, options)}"
    ]


def _grouped_read_names(call: Any) -> tuple[str, str] | None:
    """(question, group) for a grouped read naming both as literals, else None."""
    parent_position = _group_argument_position(call)
    if parent_position is None:
        return None
    args = getattr(call, "arguments", None) or []
    if len(args) <= parent_position:
        return None
    question = _literal_string(_concept_argument(call))
    group = _literal_string(args[parent_position])
    if question is None or group is None:
        return None
    return question, group


def _case_hint(name: str, candidates: list[str]) -> str:
    matches = [candidate for candidate in candidates if candidate.casefold() == name.casefold()]
    return f"; Avni matches names exactly, the bundle has {' or '.join(repr(m) for m in matches)}" if matches else ""


def _literal_string(node: Any) -> str | None:
    value = getattr(node, "value", None) if getattr(node, "type", None) == "Literal" else None
    return value if isinstance(value, str) else None


def _group_of(question: str, groups: dict[str, list[str]]) -> tuple[str, str] | None:
    """(group, bundle spelling of the question) when `question` is a question
    of one of `groups`, matching case-insensitively."""
    for group, children in groups.items():
        for child in children:
            if child.casefold() == question.casefold():
                return group, child
    return None


def _check_grouped_reads(tree: Any, spec: RuleSpec) -> tuple[list[str], set[str]]:
    """Hard checks on reads of questions inside question groups.

    - A read naming a group must name a non-repeatable group on the form, and
      a literal question must be one of that group's questions.
    - A question of a group read without its group finds nothing in Avni.
    - Questions of a repeatable group cannot be read by name.
    - When the rule is set on a field inside a group, a read of a name worked
      out at runtime must pass that same group.

    Only reads of the form being filled in are checked: the context knows
    that form's question groups, not those of other forms.

    Returns the warnings and the concept names they cover, so the plain
    off-bundle check does not report them again.
    """
    warnings: list[str] = []
    handled: set[str] = set()
    target = spec.target_question_group
    for call in _iter_call_expressions(tree):
        method = _method_name(call)
        if not _reads_current_form(call):
            continue
        args = getattr(call, "arguments", None) or []
        concept_node = _concept_argument(call)
        if concept_node is None:
            continue
        question = _literal_string(concept_node)
        if question is not None and _UUID_RE.match(question):
            continue

        parent_position = _group_argument_position(call)
        has_group = parent_position is not None and len(args) > parent_position
        group = _literal_string(args[parent_position]) if has_group else None

        if has_group and group is None:
            if question is not None and (
                _group_of(question, spec.question_group_children) or _group_of(question, spec.repeatable_question_groups)
            ):
                handled.add(question)
            continue

        if has_group and not _UUID_RE.match(group):
            if question is not None:
                handled.add(question)
            if group in spec.repeatable_question_groups:
                warnings.append(repeatable_group_warning(group))
                continue
            children = spec.question_group_children.get(group)
            if children is None:
                warnings.append(
                    f"{OFF_BUNDLE_WARNING} question group {group!r}: no question group with that concept name "
                    f"on this form{_case_hint(group, [*spec.question_group_children, *spec.repeatable_question_groups])}"
                )
            elif question is not None and question not in children:
                warnings.append(
                    f"{OFF_BUNDLE_WARNING} concept name {question!r} in question group {group!r}: "
                    f"the group holds {children}{_case_hint(question, children)}"
                )
            elif question is None and target and group != target:
                warnings.append(
                    f"{QUESTION_GROUP_WARNING}: {method}(...) reads this field with the group {group!r}, "
                    f"but the field is inside {target!r}"
                )
            continue
        if has_group:
            continue

        if question is not None:
            repeatable = _group_of(question, spec.repeatable_question_groups)
            grouped = _group_of(question, spec.question_group_children)
            if repeatable is not None:
                handled.add(question)
                warnings.append(repeatable_group_warning(repeatable[0]))
            elif grouped is not None and question not in spec.available_concepts:
                handled.add(question)
                group_name, spelling = grouped
                warnings.append(
                    f"{QUESTION_GROUP_WARNING}: {question!r} is a question inside the question group "
                    f"{group_name!r}; read it with the group, e.g. {_grouped_read_example(method, spelling, group_name)}"
                )
            continue

        if target:
            warnings.append(
                f"{QUESTION_GROUP_WARNING}: {method}(...) reads a name worked out when the rule runs without the "
                f"question group; this field is inside {target!r}, so read it with "
                f"getObservationReadableValue(<name>, {target!r}) on the form's record"
            )
    return warnings, handled


def _grouped_read_example(method: str, question: str, group: str) -> str:
    """A working grouped read: the same method when its group is the second
    argument, else getObservationReadableValue."""
    if _GROUPED_READ_PARENT_ARGUMENT.get(method) == 1:
        return f"{method}({question!r}, {group!r})"
    return f"getObservationReadableValue({question!r}, {group!r})"


def _check_text_comparisons(tree: Any, spec: RuleSpec) -> list[str]:
    """Hard checks on names compared as text with `===`, `==`, `!==`, `!=`.

    - `<x>.concept.name === '<name>'`: the name must be a question or answer
      in the bundle, spelled exactly. Scanning a record's top-level
      `observations` never finds a question inside a question group; scanning
      a group's `groupObservations` does.
    - A read of a coded question (directly, or through a variable holding it)
      compared to text must use the answer's exact name, and must read the
      readable value: the stored value of a coded answer is its UUID.
    """
    answers = _answers_by_concept(spec)
    answers_casefold = {concept.casefold(): concept for concept in answers}
    group_children = {child: group for group, children in spec.question_group_children.items() for child in children}
    repeatable_children = {child: group for group, children in spec.repeatable_question_groups.items() for child in children}
    answer_names = {answer for options in answers.values() for answer in options}
    top_level = set(spec.available_concepts) | set(spec.concept_answers) | answer_names
    known = top_level | set(group_children) | set(repeatable_children)
    bindings = _value_read_bindings(tree)

    warnings: list[str] = []
    for node, scan in _comparisons(tree, None):
        for read_side, text_side in ((node.left, node.right), (node.right, node.left)):
            text = _literal_string(text_side)
            if not text or _UUID_RE.match(text):
                continue
            if _is_concept_name(read_side):
                if scan == _TOP_LEVEL_OBSERVATIONS and text in repeatable_children:
                    warnings.append(repeatable_group_warning(repeatable_children[text]))
                    continue
                warnings.extend(_concept_name_comparison_warnings(text, top_level, known, group_children, scan))
                continue
            if _is_direct_call_of(read_side, _VALUE_AND_DATE_METHODS):
                method = _method_name(read_side)
                warnings.append(
                    f"{OFF_BUNDLE_WARNING} answer comparison: {method}(...) returns an object with `value` and "
                    f"`date`, so comparing it with {text!r} is never true; compare its `.value`"
                )
                continue
            read = _value_read(read_side, bindings)
            if read is None:
                continue
            method, question = read
            concept = question if question in answers else answers_casefold.get(question.casefold())
            if concept is None:
                continue
            if method in _RAW_VALUE_METHODS:
                warnings.append(
                    f"{OFF_BUNDLE_WARNING} answer comparison: {method}({question!r}) returns the coded answer's "
                    f"UUID, so comparing it with {text!r} is never true; compare "
                    f"getObservationReadableValue({question!r}) with an answer name"
                )
            elif text not in answers[concept]:
                warnings.append(
                    f"{OFF_BUNDLE_WARNING} answer(s) [{text!r}] for concept {concept!r} — expected one of: "
                    f"{answers[concept]}{_case_hint(text, answers[concept])}"
                )
    return warnings


def _comparisons(node: Any, scan: str | None):
    """Yield every equality comparison with the observations collection it is
    evaluated over: `observations`, `groupObservations` or None (unknown)."""
    if isinstance(node, list):
        for child in node:
            yield from _comparisons(child, scan)
        return
    node_type = getattr(node, "type", None)
    if node_type is None:
        return
    if node_type == "BinaryExpression" and getattr(node, "operator", None) in _EQUALITY_OPERATORS:
        yield node, scan
    if node_type == "CallExpression":
        callee = getattr(node, "callee", None)
        collection = getattr(getattr(getattr(callee, "object", None), "property", None), "name", None)
        if collection in (_TOP_LEVEL_OBSERVATIONS, _GROUP_OBSERVATIONS):
            yield from _comparisons(callee, scan)
            yield from _comparisons(getattr(node, "arguments", None) or [], collection)
            return
    for key, child in vars(node).items():
        if key == "type":
            continue
        if isinstance(child, list) or hasattr(child, "type"):
            yield from _comparisons(child, scan)


def _concept_name_comparison_warnings(
    text: str, top_level: set[str], known: set[str], group_children: dict[str, str], scan: str | None,
) -> list[str]:
    if text in top_level:
        return []
    if text in group_children:
        group = group_children[text]
        if scan != _TOP_LEVEL_OBSERVATIONS:
            return []
        return [
            f"{QUESTION_GROUP_WARNING}: compares a concept name with {text!r}, a question inside the question "
            f"group {group!r}; it is not a top-level observation, read it with "
            f"getObservationReadableValue({text!r}, {group!r})"
        ]
    if text in known:
        return []
    hint = _case_hint(text, sorted(known))
    if hint:
        return [f"{OFF_BUNDLE_WARNING} concept name {text!r} in a comparison{hint}"]
    return [f"{OFF_BUNDLE_WARNING} concept names: [{text!r}] (compared with a concept name)"]


def _is_concept_name(node: Any) -> bool:
    """`<x>.concept.name`."""
    if getattr(node, "type", None) != "MemberExpression" or getattr(node, "computed", False):
        return False
    prop = getattr(node, "property", None)
    owner = getattr(node, "object", None)
    return (
        getattr(prop, "name", None) == "name"
        and getattr(owner, "type", None) == "MemberExpression"
        and not getattr(owner, "computed", False)
        and getattr(getattr(owner, "property", None), "name", None) == "concept"
    )


def _is_direct_call_of(node: Any, methods: frozenset[str]) -> bool:
    return getattr(node, "type", None) == "CallExpression" and _method_name(node) in methods


def _value_read(node: Any, bindings: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """(method, question) when `node` reads a question's readable or stored
    value by a literal name — directly, as `<valueAndDateRead>.value`, or
    through a variable bound to such a read."""
    if getattr(node, "type", None) == "Identifier":
        return bindings.get(getattr(node, "name", None))
    if (
        getattr(node, "type", None) == "MemberExpression"
        and not getattr(node, "computed", False)
        and getattr(getattr(node, "property", None), "name", None) == "value"
        and _is_direct_call_of(getattr(node, "object", None), _VALUE_AND_DATE_METHODS)
    ):
        question = _literal_string(_concept_argument(node.object))
        return (_method_name(node.object), question) if question is not None else None
    if _is_direct_call_of(node, frozenset({"getReadableValue", "getValue"})):
        observation_read = getattr(getattr(node, "callee", None), "object", None)
        if _is_direct_call_of(observation_read, _OBSERVATION_RETURNING_METHODS):
            question = _literal_string(_concept_argument(observation_read))
            method = "getObservationReadableValue" if _method_name(node) == "getReadableValue" else "getObservationValue"
            return (method, question) if question is not None else None
        return None
    if not _is_direct_call_of(node, _READABLE_VALUE_METHODS | _RAW_VALUE_METHODS):
        return None
    question = _literal_string(_concept_argument(node))
    return (_method_name(node), question) if question is not None else None


def _value_read_bindings(tree: Any) -> dict[str, tuple[str, str]]:
    """Variables bound to a value read. A name bound to different reads in
    different places (separate functions) is left out, since which binding
    a comparison sees depends on scope."""
    reads_by_name: dict[str, set[tuple[str, str] | None]] = {}
    for node in _iter_nodes(tree):
        if getattr(node, "type", None) != "VariableDeclarator":
            continue
        name = getattr(getattr(node, "id", None), "name", None)
        if name:
            reads_by_name.setdefault(name, set()).add(_value_read(getattr(node, "init", None), {}))
    return {
        name: next(iter(reads))
        for name, reads in reads_by_name.items()
        if len(reads) == 1 and None not in reads
    }


def _iter_nodes(node: Any):
    """Yield every node of the AST."""
    if isinstance(node, list):
        for child in node:
            yield from _iter_nodes(child)
        return
    if getattr(node, "type", None) is None:
        return
    yield node
    for key, child in vars(node).items():
        if key == "type":
            continue
        if isinstance(child, list) or hasattr(child, "type"):
            yield from _iter_nodes(child)


def _answers_by_concept(spec: RuleSpec) -> dict[str, list[str]]:
    """Coded answers of every question a rule can read: the form, its sibling
    forms and the questions inside question groups."""
    return {**spec.question_group_answers, **spec.concept_answers}


def repeatable_group_warning(group: str) -> str:
    return (
        f"{QUESTION_GROUP_WARNING}: {group!r} is a repeatable question group; it holds a list of entries, "
        f"so its questions cannot be read by name and the rule tools do not support rules on them"
    )


def _miscased_concept_names(tree: Any, spec: RuleSpec) -> dict[str, list[str]]:
    """Concept names the rule reads that match a bundle concept only when case
    is ignored, mapped to the bundle's spelling(s). Covers every accessor that
    takes a concept name, including reads from other forms."""
    known = {name for name in [*spec.available_concepts, *spec.concept_answers] if name}
    by_casefold: dict[str, set[str]] = {}
    for name in known:
        by_casefold.setdefault(name.casefold(), set()).add(name)

    miscased: dict[str, list[str]] = {}
    for call in _iter_call_expressions(tree):
        if _method_name(call) not in _CONCEPT_READS | _VALUE_IN_METHODS:
            continue
        if _grouped_read_names(call) is not None:
            continue
        name = _literal_string(_concept_argument(call))
        if name is None:
            continue
        if name in known or _UUID_RE.match(name):
            continue
        actuals = by_casefold.get(name.casefold())
        if actuals:
            miscased[name] = sorted(actuals)
    return miscased


def _find_iife(tree: Any) -> Any | None:
    """Return the top-level arrow function expression if the IIFE shape matches.

    Accepts the canonical Avni rule shape:
        "use strict";
        ({ params, imports }) => { ... };

    The "use strict" directive is optional. The arrow function must destructure
    `{ params, imports }` in its first parameter.
    """
    body = getattr(tree, "body", None) or []
    for stmt in body:
        if getattr(stmt, "type", None) != "ExpressionStatement":
            continue
        expr = stmt.expression
        if getattr(expr, "type", None) != "ArrowFunctionExpression":
            continue
        params = getattr(expr, "params", []) or []
        if len(params) != 1:
            continue
        first = params[0]
        if getattr(first, "type", None) != "ObjectPattern":
            continue
        destructured = {
            getattr(p.key, "name", None)
            for p in (first.properties or [])
            if getattr(p, "key", None) is not None
        }
        if {"params", "imports"}.issubset(destructured):
            return expr
    return None


def _walk(node: Any, concepts: set[str], encounter_types: set[str]) -> None:
    """Recursively walk an esprima AST, collecting concept + encounter-type literals."""
    if isinstance(node, list):
        for child in node:
            _walk(child, concepts, encounter_types)
        return

    node_type = getattr(node, "type", None)
    if node_type is None:
        return

    if node_type == "CallExpression":
        _collect_call_args(node, concepts, encounter_types)
    elif node_type == "Property":
        _collect_property(node, encounter_types)

    for key, child in vars(node).items():
        if key == "type":
            continue
        if isinstance(child, list) or hasattr(child, "type"):
            _walk(child, concepts, encounter_types)


def _collect_call_args(
    call: Any, concepts: set[str], encounter_types: set[str]
) -> None:
    """When a call's callee is a known accessor, capture the first string arg."""
    callee = getattr(call, "callee", None)
    if callee is None or getattr(callee, "type", None) != "MemberExpression":
        return
    if getattr(callee, "computed", False):
        return
    prop = getattr(callee, "property", None)
    if prop is None or getattr(prop, "type", None) != "Identifier":
        return

    method = prop.name
    args = getattr(call, "arguments", None) or []
    if not args:
        return
    first = _concept_argument(call) if method in _CONCEPT_READS else args[0]
    value = _literal_string(first)
    if value is None:
        return

    if method in _CONCEPT_READS:
        if _UUID_RE.match(value) or _grouped_read_names(call) is not None:
            return
        concepts.add(value)
    elif method in (_CURRENT_FORM_VALUE_METHOD.get(), *_VISIT_VALUE_METHODS) and not _UUID_RE.match(value):
        # A visit read is grounded on every form: a made-up name must stay an
        # off-bundle error, so the wrong-form-type retry cannot swap in a field.
        if _grouped_read_names(call) is None:
            concepts.add(value)
    elif method in ENCOUNTER_TYPE_ACCESSORS:
        encounter_types.add(value)


def _collect_property(prop: Any, encounter_types: set[str]) -> None:
    """When a Property key matches a known key, capture its literal string value."""
    key = getattr(prop, "key", None)
    value = getattr(prop, "value", None)
    if key is None or value is None:
        return
    key_name = _property_key_name(key)
    if key_name not in _ENCOUNTER_TYPE_PROPERTY_KEYS:
        return
    if getattr(value, "type", None) != "Literal":
        return
    str_value = getattr(value, "value", None)
    if isinstance(str_value, str):
        encounter_types.add(str_value)


def _property_key_name(key: Any) -> str | None:
    key_type = getattr(key, "type", None)
    if key_type == "Identifier":
        return getattr(key, "name", None)
    if key_type == "Literal":
        v = getattr(key, "value", None)
        return v if isinstance(v, str) else None
    return None


# ── Answer grounding (CONCEPT_ANSWER_GROUNDING_SDD §7) ────────────────────────


def _check_answer_grounding(tree: Any, spec: RuleSpec) -> list[str]:
    """Validate `containsAnswerConceptName(...)` literals against the bundle.

    Walks RuleCondition chains: every `containsAnswer*(...)` call is paired
    with its anchoring `valueIn*(concept)` call by traversing the callee
    chain. The answer literals are checked against
    `spec.concept_answers[concept]`, matching case exactly as Avni does.
    Skips UUID-shaped args (the rule API accepts both names and UUIDs; we
    only validate names).
    No-op when no coded question is in scope.
    """
    all_answers = _answers_by_concept(spec)
    if not all_answers:
        return []

    answers_by_concept: dict[str, set[str]] = {
        concept: set(options)
        for concept, options in all_answers.items()
    }
    valid_listing_by_concept: dict[str, list[str]] = {
        concept: list(options)
        for concept, options in all_answers.items()
    }

    concepts_by_casefold: dict[str, list[str]] = {}
    for concept in all_answers:
        concepts_by_casefold.setdefault(concept.casefold(), []).append(concept)
    off_list: dict[str, set[str]] = {}
    for call in _iter_call_expressions(tree):
        if _method_name(call) not in _CONTAINS_ANSWER_METHODS:
            continue
        concept = _find_anchoring_concept(call)
        if concept is None or _UUID_RE.match(concept):
            continue
        valid = answers_by_concept.get(concept)
        if valid is None and concept.casefold() in concepts_by_casefold:
            # The concept name's case is reported by `_miscased_concept_names`;
            # still check the answers under the bundle's spelling.
            spellings = concepts_by_casefold[concept.casefold()]
            valid = set().union(*(answers_by_concept[spelling] for spelling in spellings))
            valid_listing_by_concept.setdefault(
                concept, [a for spelling in spellings for a in valid_listing_by_concept[spelling]],
            )
        if valid is None:
            # Concept isn't in this rule's grounding scope — non-coded
            # concept, or out-of-scope form. Skip rather than reject.
            continue
        for answer in _string_args(call):
            if _UUID_RE.match(answer):
                continue
            if answer not in valid:
                off_list.setdefault(concept, set()).add(answer)

    warnings: list[str] = []
    for concept, bad in off_list.items():
        valid_listing = valid_listing_by_concept[concept]
        warnings.append(
            f"{OFF_BUNDLE_WARNING} answer(s) {sorted(bad)} for concept {concept!r} — "
            f"expected one of: {valid_listing}"
        )
    return warnings


def _iter_call_expressions(node: Any):
    """Yield every CallExpression in the AST."""
    if isinstance(node, list):
        for child in node:
            yield from _iter_call_expressions(child)
        return
    node_type = getattr(node, "type", None)
    if node_type is None:
        return
    if node_type == "CallExpression":
        yield node
    for key, child in vars(node).items():
        if key == "type":
            continue
        if isinstance(child, list) or hasattr(child, "type"):
            yield from _iter_call_expressions(child)


def _method_name(call: Any) -> str | None:
    """Method name of a `something.method(...)` call, or None."""
    callee = getattr(call, "callee", None)
    if callee is None or getattr(callee, "type", None) != "MemberExpression":
        return None
    prop = getattr(callee, "property", None)
    if prop is None or getattr(prop, "type", None) != "Identifier":
        return None
    return prop.name


def _string_args(call: Any) -> list[str]:
    """Return every Literal string argument of a call."""
    out: list[str] = []
    for arg in (getattr(call, "arguments", None) or []):
        if getattr(arg, "type", None) == "Literal":
            v = getattr(arg, "value", None)
            if isinstance(v, str):
                out.append(v)
    return out


def _find_anchoring_concept(call: Any) -> str | None:
    """Walk back the receiver chain of `call` to find a `valueIn*(concept)` call.

    For `foo().when.valueInRegistration('X').containsAnswerConceptName('Y')`,
    starting at the containsAnswerConceptName call, this returns `'X'`.
    """
    cur = call
    # Cap depth defensively in case of weird shapes.
    for _ in range(64):
        callee = getattr(cur, "callee", None)
        if callee is None or getattr(callee, "type", None) != "MemberExpression":
            return None
        obj = getattr(callee, "object", None)
        # Walk past nested MemberExpressions (e.g. `.and.when`)
        while obj is not None and getattr(obj, "type", None) == "MemberExpression":
            obj = getattr(obj, "object", None)
        if obj is None or getattr(obj, "type", None) != "CallExpression":
            return None
        if _method_name(obj) in _VALUE_IN_METHODS:
            args = getattr(obj, "arguments", None) or []
            if args and getattr(args[0], "type", None) == "Literal":
                v = getattr(args[0], "value", None)
                return v if isinstance(v, str) else None
            return None
        cur = obj
    return None


# ── Per-kind return-shape checks (FORM_LEVEL_RULES_SDD §5.3) ─────────────────


_DECISION_ARRAY_KEYS: frozenset[str] = frozenset({
    "encounterDecisions",
    "registrationDecisions",
    "enrolmentDecisions",
})


def _check_return_shape(tree: Any, spec: RuleSpec) -> list[str]:
    """Soft per-kind return-shape sanity check.

    Returns warnings only — never blocks the write. The grounding checks
    above are the only hard gate. See FORM_LEVEL_RULES_SDD §5.3.
    """
    if spec.rule_kind == RuleKind.VISIT_SCHEDULE:
        return []  # Relies on the system-prompt rubric, no AST check.

    arrow = _find_iife(tree)
    if arrow is None:
        return []  # IIFE-shape failure already surfaced as a hard reject.

    returns = _collect_return_statements(arrow)
    if not returns:
        return [f"{spec.rule_kind.value}: no return statement found"]

    if spec.rule_kind == RuleKind.VALIDATION:
        return _check_validation_shape(returns, tree)
    if spec.rule_kind == RuleKind.EDIT_FORM:
        return _check_edit_form_shape(returns)
    if spec.rule_kind == RuleKind.DECISION:
        return _check_decision_shape(returns, tree)
    if spec.rule_kind == RuleKind.FORM_ELEMENT:
        return _check_form_element_shape(returns, arrow)
    return []


def _collect_return_statements(arrow_fn: Any) -> list[Any]:
    """Every ReturnStatement reachable from an arrow function's body.

    Concise-body arrows (`(args) => expr`) have no return statement node —
    the body itself is the return value. We synthesise a single fake-return
    pointer so the shape checks can treat both cases uniformly.
    """
    body = getattr(arrow_fn, "body", None)
    if body is None:
        return []
    if getattr(body, "type", None) != "BlockStatement":
        # Concise body — wrap so `getattr(r, "argument", None)` returns it.
        return [_ConciseReturnAdapter(body)]
    returns: list[Any] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not hasattr(node, "type"):
            return
        if node.type == "ReturnStatement":
            returns.append(node)
        for key, value in vars(node).items():
            if key == "type":
                continue
            if isinstance(value, list) or hasattr(value, "type"):
                visit(value)

    visit(body)
    return returns


class _ConciseReturnAdapter:
    """Adapter so callers can treat a concise-body expression like a return."""

    type = "ReturnStatement"

    def __init__(self, expression: Any) -> None:
        self.argument = expression


def _check_validation_shape(returns: list[Any], tree: Any) -> list[str]:
    """validationRule: return is array-shaped; createValidationError args are string-literals."""
    warnings: list[str] = []
    array_like = {"ArrayExpression", "Identifier", "CallExpression"}
    if not any(_return_arg_type(r) in array_like for r in returns):
        warnings.append(
            "validationRule: top-level return doesn't resolve to an "
            "array-shaped value. Expected an ArrayExpression literal, an "
            "Identifier (e.g. `validationResults`), or a CallExpression that "
            "yields an array."
        )

    for call in _iter_call_expressions(tree):
        if _method_name(call) != "createValidationError":
            continue
        args = getattr(call, "arguments", None) or []
        if not args:
            warnings.append(
                "validationRule: `createValidationError(...)` called without arguments — "
                "must take a single string messageKey."
            )
            break
        first = args[0]
        if getattr(first, "type", None) != "Literal" or not isinstance(
            getattr(first, "value", None), str
        ):
            warnings.append(
                "validationRule: `createValidationError(...)` first argument is not a "
                "string literal — pass the message key directly so it can be grounded."
            )
            break
    return warnings


def _check_edit_form_shape(returns: list[Any]) -> list[str]:
    """editFormRule: prefer `{ eligible: { value, message? } }`; flag legacy shapes."""
    has_preferred = False
    has_legacy_eligible_bool = False
    has_legacy_editable = False

    for ret in returns:
        arg = getattr(ret, "argument", None)
        if arg is None or getattr(arg, "type", None) != "ObjectExpression":
            continue
        for prop in (getattr(arg, "properties", None) or []):
            key_name = _property_key_name(getattr(prop, "key", None))
            value = getattr(prop, "value", None)
            if key_name == "eligible":
                if getattr(value, "type", None) == "ObjectExpression":
                    for inner in (getattr(value, "properties", None) or []):
                        if _property_key_name(getattr(inner, "key", None)) == "value":
                            has_preferred = True
                            break
                elif getattr(value, "type", None) == "Literal" and isinstance(
                    getattr(value, "value", None), bool
                ):
                    has_legacy_eligible_bool = True
            elif key_name == "editable":
                has_legacy_editable = True

    if not (has_preferred or has_legacy_eligible_bool or has_legacy_editable):
        return [
            "editFormRule: top-level return doesn't carry an `eligible` property "
            "in the expected shape — expected `{ eligible: { value: boolean, "
            "message?: string } }`."
        ]
    if has_legacy_eligible_bool or has_legacy_editable:
        return [
            "editFormRule: legacy return shape detected — prefer "
            "`{ eligible: { value, message } }` over `{ eligible: boolean }` "
            "or `{ editable: boolean }`."
        ]
    return []


def _check_form_element_shape(returns: list[Any], arrow: Any) -> list[str]:
    """formElementRule: return is a `FormElementStatus` (constructor or `statusBuilder.build()`).

    Per FIELD_AND_PAGE_VISIBILITY_RULES_SDD §4.3: top-level `return` must
    resolve to one of —
      - `new imports.rulesConfig.FormElementStatus(...)` constructor call;
      - a call to `.build()` on a `FormElementStatusBuilder` constructed in
        the same scope (via `new imports.rulesConfig.FormElementStatusBuilder`).
    Anything else (object literal, array, raw identifier without a matching
    constructor) is a soft warning. Grounding checks remain the hard gate.
    """
    if not returns:
        return ["formElementRule: no return statement found"]

    has_constructor_return = False
    has_builder_build_return = False
    builder_identifiers: set[str] = _builder_identifiers_in_scope(arrow)

    for ret in returns:
        arg = getattr(ret, "argument", None)
        if arg is None:
            continue
        if _is_form_element_status_constructor(arg):
            has_constructor_return = True
            break
        if _is_builder_build_call(arg, builder_identifiers):
            has_builder_build_return = True
            break

    if has_constructor_return or has_builder_build_return:
        return []

    return [
        "formElementRule: top-level return doesn't resolve to a "
        "`FormElementStatus` — expected either "
        "`new imports.rulesConfig.FormElementStatus(...)` or a "
        "`statusBuilder.build()` call on a `FormElementStatusBuilder` "
        "constructed in the same scope."
    ]


def _is_form_element_status_constructor(node: Any) -> bool:
    """True when `node` is `new imports.rulesConfig.FormElementStatus(...)`."""
    if getattr(node, "type", None) != "NewExpression":
        return False
    callee = getattr(node, "callee", None)
    return _is_member_chain(callee, ("imports", "rulesConfig", "FormElementStatus"))


def _is_builder_build_call(node: Any, builder_identifiers: set[str]) -> bool:
    """True when `node` is `<builder>.build()` and the builder name is in scope."""
    if getattr(node, "type", None) != "CallExpression":
        return False
    callee = getattr(node, "callee", None)
    if callee is None or getattr(callee, "type", None) != "MemberExpression":
        return False
    prop = getattr(callee, "property", None)
    if prop is None or getattr(prop, "name", None) != "build":
        return False
    obj = getattr(callee, "object", None)
    if obj is None or getattr(obj, "type", None) != "Identifier":
        return False
    return getattr(obj, "name", None) in builder_identifiers


def _builder_identifiers_in_scope(arrow: Any) -> set[str]:
    """Names declared as `new imports.rulesConfig.FormElementStatusBuilder(...)` in the IIFE."""
    out: set[str] = set()
    body = getattr(arrow, "body", None)
    if body is None:
        return out
    for node in _iter_all_nodes(body):
        if getattr(node, "type", None) != "VariableDeclarator":
            continue
        init = getattr(node, "init", None)
        if init is None or getattr(init, "type", None) != "NewExpression":
            continue
        callee = getattr(init, "callee", None)
        if not _is_member_chain(
            callee, ("imports", "rulesConfig", "FormElementStatusBuilder")
        ):
            continue
        identifier = getattr(node, "id", None)
        if identifier is None or getattr(identifier, "type", None) != "Identifier":
            continue
        name = getattr(identifier, "name", None)
        if isinstance(name, str):
            out.add(name)
    return out


def _check_builder_entity_key(arrow: Any, spec: RuleSpec) -> list[str]:
    """Hard check: rulesConfig classes receive the form's record under the form's entity key.

    The rule reads the form's record from `params.entity`. Passing that record
    to a builder or `RuleCondition` under another entity key (e.g.
    `{programEncounter: encounter}` on a general-visit form) builds the rule
    for the wrong form type. Only properties whose value is the form's record
    (`params.entity` itself, or a variable bound to it) are checked; derived
    records such as `{programEnrolment: programEncounter.programEnrolment}`
    are legitimate.
    """
    entity_variables = _identifiers_bound_to_params_entity(arrow)
    expected_key = entity_param_for_form_type(spec.form_type)
    warnings: list[str] = []
    for node in _iter_all_nodes(getattr(arrow, "body", None)):
        if getattr(node, "type", None) != "NewExpression":
            continue
        class_name = _rules_config_class_name(getattr(node, "callee", None))
        if class_name is None:
            continue
        for argument in getattr(node, "arguments", None) or []:
            if getattr(argument, "type", None) != "ObjectExpression":
                continue
            for prop in getattr(argument, "properties", None) or []:
                key = _property_key_name(prop)
                if (
                    key in ENTITY_PARAMS
                    and key != expected_key
                    and _is_form_record(getattr(prop, "value", None), entity_variables)
                ):
                    warnings.append(
                        f"{WRONG_FORM_TYPE_WARNING} in {class_name}: the form's record is "
                        f"passed as {key!r}, but a {spec.form_type or 'subject'} "
                        f"form needs {expected_key!r}"
                    )
    return warnings


def _check_visit_reads_on_a_form_without_a_visit(tree: Any, spec: RuleSpec) -> list[str]:
    """Hard check: no visit read (valueInEncounter / valueInCancelEncounter)
    on a registration, enrolment or exit form, where it finds no answer and
    the condition is never true."""
    own_read = current_form_value_method(spec.form_type)
    if own_read in _VISIT_VALUE_METHODS:
        return []
    form_kind = _FORM_KIND_IN_WORDS.get(spec.form_type, spec.form_type or "subject")
    used = sorted({
        method for method in map(_method_name, _iter_call_expressions(tree))
        if method in _VISIT_VALUE_METHODS
    })
    return [
        f"{WRONG_FORM_TYPE_WARNING}: {method} reads a visit's answers, but this is a "
        f"{form_kind} form, so it finds no answer and the condition is never true; "
        f"read this form's answers with {own_read}"
        for method in used
    ]


def _is_form_record(value: Any, entity_variables: set[str]) -> bool:
    """True for `params.entity` or an identifier bound to it."""
    if getattr(value, "type", None) == "Identifier":
        return getattr(value, "name", None) in entity_variables
    return _is_member_chain(value, ("params", "entity"))


def _identifiers_bound_to_params_entity(arrow: Any) -> set[str]:
    """Names in the IIFE that hold the form's record.

    Covers `const x = params.entity`, `const {entity} = params`,
    `const {entity: x} = params`, and aliases such as `const y = x` of any of
    those, followed to a fixed point.
    """
    declarators = [
        node for node in _iter_all_nodes(getattr(arrow, "body", None))
        if getattr(node, "type", None) == "VariableDeclarator"
    ]
    names: set[str] = set()
    for declarator in declarators:
        names.update(_names_bound_to_params_entity(declarator))

    added = True
    while added:
        added = False
        for declarator in declarators:
            identifier = getattr(declarator, "id", None)
            init = getattr(declarator, "init", None)
            name = getattr(identifier, "name", None)
            if (
                getattr(identifier, "type", None) == "Identifier"
                and getattr(init, "type", None) == "Identifier"
                and getattr(init, "name", None) in names
                and isinstance(name, str)
                and name not in names
            ):
                names.add(name)
                added = True
    return names


def _names_bound_to_params_entity(declarator: Any) -> set[str]:
    """Names one declarator binds directly to `params.entity`."""
    identifier = getattr(declarator, "id", None)
    init = getattr(declarator, "init", None)
    identifier_type = getattr(identifier, "type", None)

    if identifier_type == "Identifier" and _is_member_chain(init, ("params", "entity")):
        name = getattr(identifier, "name", None)
        return {name} if isinstance(name, str) else set()

    is_params = getattr(init, "type", None) == "Identifier" and getattr(init, "name", None) == "params"
    if identifier_type != "ObjectPattern" or not is_params:
        return set()
    names: set[str] = set()
    for prop in getattr(identifier, "properties", None) or []:
        if _property_key_name(prop) != "entity":
            continue
        target = getattr(prop, "value", None)
        if getattr(target, "type", None) == "AssignmentPattern":
            target = getattr(target, "left", None)
        name = getattr(target, "name", None)
        if getattr(target, "type", None) == "Identifier" and isinstance(name, str):
            names.add(name)
    return names


def _rules_config_class_name(callee: Any) -> str | None:
    """Class name for `new imports.rulesConfig.<X>(...)`, or `new <X>(...)` for a builder or RuleCondition.

    A bare `new <X>(...)` counts only for names ending in `Builder` and for
    `RuleCondition`, the rulesConfig classes that take the form's record.
    """
    callee_type = getattr(callee, "type", None)
    if callee_type == "MemberExpression":
        name = getattr(getattr(callee, "property", None), "name", None)
        if isinstance(name, str) and _is_member_chain(callee, ("imports", "rulesConfig", name)):
            return name
        return None
    if callee_type == "Identifier":
        name = getattr(callee, "name", None)
        if isinstance(name, str) and (name.endswith("Builder") or name == "RuleCondition"):
            return name
    return None


def _property_key_name(prop: Any) -> str | None:
    """Key of an object-literal property written as an identifier or a string."""
    if getattr(prop, "computed", False):
        return None
    key = getattr(prop, "key", None)
    key_type = getattr(key, "type", None)
    if key_type == "Identifier":
        return getattr(key, "name", None)
    if key_type == "Literal" and isinstance(getattr(key, "value", None), str):
        return key.value
    return None


def _is_member_chain(node: Any, expected: tuple[str, ...]) -> bool:
    """True when `node` is the MemberExpression chain `expected[0].expected[1]....`.

    For `imports.rulesConfig.FormElementStatus`:
      MemberExpression(property=FormElementStatus,
                       object=MemberExpression(property=rulesConfig,
                                               object=Identifier(imports)))

    Walks from the outermost member-expression inward, collecting property
    names; compares the (root_identifier, *reversed_property_chain) tuple
    against `expected`.
    """
    if node is None or len(expected) < 2:
        return False
    properties: list[str] = []
    cur: Any = node
    while getattr(cur, "type", None) == "MemberExpression":
        prop = getattr(cur, "property", None)
        name = getattr(prop, "name", None) if prop is not None else None
        if not isinstance(name, str):
            return False
        properties.append(name)
        cur = getattr(cur, "object", None)
    if getattr(cur, "type", None) != "Identifier":
        return False
    root = getattr(cur, "name", None)
    chain = (root, *reversed(properties))
    return chain == tuple(expected)


def _check_decision_shape(returns: list[Any], tree: Any) -> list[str]:
    """decisionRule: return is the container object; body touches at least one decisions array."""
    warnings: list[str] = []
    for ret in returns:
        arg = getattr(ret, "argument", None)
        if arg is None:
            continue
        arg_type = getattr(arg, "type", None)
        if arg_type == "ArrayExpression":
            warnings.append(
                "decisionRule: top-level return is an ArrayExpression, but "
                "Avni expects the decisions container "
                "`{ encounterDecisions, registrationDecisions, enrolmentDecisions }` "
                "— not a flat list."
            )
            break
        if arg_type not in {"ObjectExpression", "Identifier", "CallExpression"}:
            warnings.append(
                f"decisionRule: top-level return resolves to {arg_type!r}; "
                f"expected an ObjectExpression or an Identifier holding one."
            )
            break

    touched: set[str] = set()
    for node in _iter_all_nodes(tree):
        node_type = getattr(node, "type", None)
        if node_type == "MemberExpression":
            prop = getattr(node, "property", None)
            if prop is not None and getattr(prop, "type", None) == "Identifier":
                name = getattr(prop, "name", None)
                if name in _DECISION_ARRAY_KEYS:
                    touched.add(name)
        elif node_type == "Property":
            key_name = _property_key_name(getattr(node, "key", None))
            if key_name in _DECISION_ARRAY_KEYS:
                touched.add(key_name)

    if not touched:
        warnings.append(
            "decisionRule: rule body doesn't reference any of "
            "`encounterDecisions / registrationDecisions / enrolmentDecisions` — "
            "decision rules must populate at least one of these arrays on the "
            "returned container."
        )
    return warnings


def _return_arg_type(ret: Any) -> str | None:
    arg = getattr(ret, "argument", None)
    if arg is None:
        return None
    return getattr(arg, "type", None)


def _iter_all_nodes(node: Any):
    """Yield every AST node — used by per-kind shape checks for body scans."""
    if isinstance(node, list):
        for child in node:
            yield from _iter_all_nodes(child)
        return
    if not hasattr(node, "type"):
        return
    yield node
    for key, value in vars(node).items():
        if key == "type":
            continue
        if isinstance(value, list) or hasattr(value, "type"):
            yield from _iter_all_nodes(value)

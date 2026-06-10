"""JS validation for generated rule functions.

`check(result, spec)` parses the generated JS with `esprima`, verifies the IIFE
shape Avni expects, and walks the AST to confirm every concept-name and
encounter-type string the rule references is in `RuleSpec.available_*`. The
validator never executes the JS — runtime-equivalence is out of scope (see
specs/VISIT_SCHEDULE_RULE_SDD.md §2, §7.3).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import esprima
from esprima.error_handler import Error as EsprimaError

from domain.rules.accessors import CONCEPT_ACCESSORS, ENCOUNTER_TYPE_ACCESSORS
from domain.rules.rule_spec import RuleResult, RuleSpec

log = logging.getLogger(__name__)


# ── Property keys whose Literal value is an encounter-type name ───────────────
_ENCOUNTER_TYPE_PROPERTY_KEYS: frozenset[str] = frozenset({
    "encounterType",
})

# RuleCondition chain methods used for answer grounding (§7 of
# CONCEPT_ANSWER_GROUNDING_SDD.md).
_VALUE_IN_METHODS: frozenset[str] = frozenset({
    "valueInEncounter",
    "valueInRegistration",
    "valueInEntireEnrolment",
    "valueInLastEncounter",
})
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

    referenced_concepts: set[str] = set()
    referenced_encounter_types: set[str] = set()
    _walk(tree, referenced_concepts, referenced_encounter_types)

    available_concepts = _lowered(spec.available_concepts)
    available_encounters = _lowered(spec.available_encounter_types)

    bad_concepts = sorted(
        name for name in referenced_concepts
        if name.casefold() not in available_concepts
    )
    bad_encounters = sorted(
        name for name in referenced_encounter_types
        if name.casefold() not in available_encounters
    )

    warnings: list[str] = []
    if bad_concepts:
        warnings.append(f"off-bundle concept names: {bad_concepts}")
    if bad_encounters:
        warnings.append(f"off-bundle encounter types: {bad_encounters}")

    warnings.extend(_check_answer_grounding(tree, spec))

    return (not warnings), warnings


def _lowered(names: list[str]) -> set[str]:
    return {n.casefold() for n in names if n}


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
    first = args[0]
    if getattr(first, "type", None) != "Literal":
        return
    value = getattr(first, "value", None)
    if not isinstance(value, str):
        return

    if method in CONCEPT_ACCESSORS:
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
    `spec.concept_answers[concept]` (case-insensitive). Skips UUID-shaped
    args (the rule API accepts both names and UUIDs; we only validate names).
    No-op when `spec.concept_answers` is empty.
    """
    if not spec.concept_answers:
        return []

    answers_by_concept: dict[str, set[str]] = {
        concept.casefold(): {a.casefold() for a in options}
        for concept, options in spec.concept_answers.items()
    }
    valid_listing_by_concept: dict[str, list[str]] = {
        concept.casefold(): list(options)
        for concept, options in spec.concept_answers.items()
    }

    off_list: dict[str, set[str]] = {}
    for call in _iter_call_expressions(tree):
        if _method_name(call) not in _CONTAINS_ANSWER_METHODS:
            continue
        concept = _find_anchoring_concept(call)
        if concept is None or _UUID_RE.match(concept):
            continue
        valid = answers_by_concept.get(concept.casefold())
        if valid is None:
            # Concept isn't in this rule's grounding scope — non-coded
            # concept, or out-of-scope form. Skip rather than reject.
            continue
        for answer in _string_args(call):
            if _UUID_RE.match(answer):
                continue
            if answer.casefold() not in valid:
                off_list.setdefault(concept, set()).add(answer)

    warnings: list[str] = []
    for concept, bad in off_list.items():
        valid_listing = valid_listing_by_concept[concept.casefold()]
        warnings.append(
            f"off-bundle answer(s) {sorted(bad)} for concept {concept!r} — "
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

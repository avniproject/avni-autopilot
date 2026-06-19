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
from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec

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

    # Hard warnings (grounding failures) — these block the write.
    hard_warnings: list[str] = []
    if bad_concepts:
        hard_warnings.append(f"off-bundle concept names: {bad_concepts}")
    if bad_encounters:
        hard_warnings.append(f"off-bundle encounter types: {bad_encounters}")
    hard_warnings.extend(_check_answer_grounding(tree, spec))

    # Soft warnings (return-shape mismatches) — surface to the caller but
    # do not block writes. See SDD §5.3.
    soft_warnings = _check_return_shape(tree, spec)

    return (not hard_warnings), hard_warnings + soft_warnings


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

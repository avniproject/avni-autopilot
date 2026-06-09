"""JS validation for generated rule functions.

`check(result, spec)` parses the generated JS with `esprima`, verifies the IIFE
shape Avni expects, and walks the AST to confirm every concept-name and
encounter-type string the rule references is in `RuleSpec.available_*`. The
validator never executes the JS — runtime-equivalence is out of scope (see
specs/VISIT_SCHEDULE_RULE_SDD.md §2, §7.3).
"""

from __future__ import annotations

import logging
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

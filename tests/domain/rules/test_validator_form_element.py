"""Unit tests for the ``formElementRule`` return-shape AST check.

Covers the soft-warning surface introduced by
specs/FIELD_AND_PAGE_VISIBILITY_RULES_SDD.md §4.3:

- Canonical ``new imports.rulesConfig.FormElementStatus(...)`` return is
  accepted with no warning.
- Canonical ``statusBuilder.build()`` return is accepted when the builder
  was constructed in scope.
- Plain object literals, arrays, and bare identifiers without a matching
  constructor surface the soft warning.
- Grounding remains the hard gate — the return-shape warning never blocks
  ``check`` from returning ``ok=True``.

Usage:
    PYTHONPATH=src pytest tests/domain/rules/test_validator_form_element.py
"""

from __future__ import annotations

import pytest

from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec
from domain.rules.validator import check


def _spec() -> RuleSpec:
    return RuleSpec(
        rule_kind=RuleKind.FORM_ELEMENT,
        intent="show only when place of delivery is hospital",
        form_name="ANC",
        form_type="Encounter",
        available_concepts=["Place of delivery"],
        concept_answers={"Place of delivery": ["Hospital"]},
    )


def _result(js: str) -> RuleResult:
    return RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high")


# ── Accepted shapes ──────────────────────────────────────────────────────────


def test_form_element_status_constructor_return_is_accepted() -> None:
    js = """\
"use strict";
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, true, null);
};
"""
    ok, warnings = check(_result(js), _spec())
    assert ok is True
    assert warnings == []


def test_status_builder_build_call_is_accepted() -> None:
    js = """\
"use strict";
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({encounter, formElement});
  statusBuilder.show().when.valueInEncounter("Place of delivery").containsAnswerConceptName("Hospital");
  return statusBuilder.build();
};
"""
    ok, warnings = check(_result(js), _spec())
    assert ok is True
    assert warnings == []


def test_builder_name_other_than_status_builder_is_still_accepted() -> None:
    """The variable doesn't have to be named ``statusBuilder`` — any
    identifier bound to ``new imports.rulesConfig.FormElementStatusBuilder``
    is in scope."""
    js = """\
"use strict";
({params, imports}) => {
  const formElement = params.formElement;
  const sb = new imports.rulesConfig.FormElementStatusBuilder({entity: params.entity, formElement});
  sb.show();
  return sb.build();
};
"""
    ok, warnings = check(_result(js), _spec())
    assert ok is True
    assert warnings == []


# ── Soft-warning shapes ───────────────────────────────────────────────────────


def test_plain_object_literal_return_produces_soft_warning() -> None:
    js = """\
"use strict";
({params, imports}) => {
  return { visibility: true };
};
"""
    ok, warnings = check(_result(js), _spec())
    # Soft warning, not a hard reject — grounding is the only hard gate.
    assert ok is True
    assert any("formElementRule" in w for w in warnings)


def test_array_literal_return_produces_soft_warning() -> None:
    js = """\
"use strict";
({params, imports}) => {
  return [];
};
"""
    ok, warnings = check(_result(js), _spec())
    assert ok is True
    assert any("formElementRule" in w for w in warnings)


def test_build_call_without_in_scope_builder_produces_soft_warning() -> None:
    """A bare ``foo.build()`` return where ``foo`` was NOT constructed via
    ``new imports.rulesConfig.FormElementStatusBuilder(...)`` should not be
    silently accepted — the validator can't confirm the shape."""
    js = """\
"use strict";
({params, imports}) => {
  return foo.build();
};
"""
    ok, warnings = check(_result(js), _spec())
    assert ok is True
    assert any("formElementRule" in w for w in warnings)


# ── Hard-gate behaviours (preserved across the new check) ────────────────────


def test_empty_js_is_rejected_before_shape_check() -> None:
    ok, warnings = check(_result(""), _spec())
    assert ok is False
    assert warnings == ["empty JS"]


def test_off_bundle_concept_is_a_hard_reject_even_with_good_shape() -> None:
    """The constructor return is well-formed but the rule reads an
    observation by a concept that isn't on the bundle's allowlist →
    hard reject. Confirms the new return-shape check doesn't relax
    the existing grounding gate."""
    js = """\
"use strict";
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  const value = encounter.getObservationReadableValue("Nonexistent Concept");
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, true, value);
};
"""
    ok, warnings = check(_result(js), _spec())
    assert ok is False
    assert any("off-bundle concept" in w for w in warnings)

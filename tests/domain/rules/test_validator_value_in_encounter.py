"""Unit tests for grounding ``valueInEncounter`` field names in ``validator.check``.

``valueInEncounter`` reads a field of the form being filled in, so its name
must be one of that form's fields. ``valueInRegistration`` reads another form
and is left unchecked, since the context lists only the current form's fields.

Usage:
    PYTHONPATH=src pytest tests/domain/rules/test_validator_value_in_encounter.py
"""

from __future__ import annotations

from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec
from domain.rules.validator import check


def _spec() -> RuleSpec:
    return RuleSpec(
        rule_kind=RuleKind.FORM_ELEMENT,
        intent="show only when the condition holds",
        form_name="QA General Visit Encounter",
        form_type="Encounter",
        available_concepts=["QA Visit Coded", "QA Visit Numeric"],
        concept_answers={"QA Visit Coded": ["QA Yes", "QA No"]},
    )


def _rule(condition: str) -> RuleResult:
    js = f"""\
"use strict";
({{params, imports}}) => {{
  const encounter = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({{encounter, formElement}});
  statusBuilder.show().when.{condition};
  return statusBuilder.build();
}};
"""
    return RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high")


def test_field_not_on_the_form_is_rejected() -> None:
    ok, warnings = check(_rule('valueInEncounter("Haemoglobin level").is.lessThan(7)'), _spec())
    assert ok is False
    assert "off-bundle concept names: ['Haemoglobin level']" in warnings


def test_field_on_the_form_is_accepted() -> None:
    ok, warnings = check(_rule('valueInEncounter("QA Visit Numeric").is.lessThan(7)'), _spec())
    assert ok is True
    assert warnings == []


def test_field_name_match_ignores_case() -> None:
    ok, _ = check(_rule('valueInEncounter("qa visit numeric").is.lessThan(7)'), _spec())
    assert ok is True


def test_registration_field_is_not_checked_against_the_current_form() -> None:
    ok, warnings = check(_rule('valueInRegistration("Date of birth").defined.matches()'), _spec())
    assert ok is True
    assert not any(w.startswith("off-bundle concept names") for w in warnings)


def test_field_given_as_a_uuid_is_not_checked_against_names() -> None:
    ok, _ = check(_rule('valueInEncounter("514bcb8b-efc1-4eb3-96bd-4366d4d29874").defined.matches()'), _spec())
    assert ok is True

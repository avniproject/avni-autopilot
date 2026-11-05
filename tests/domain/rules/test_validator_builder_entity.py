"""Unit tests for the builder entity-key hard check in ``validator.check``.

A rule reads the form's record from ``params.entity``. Handing that record to
a rulesConfig builder under another form type's key builds the rule for the
wrong form type, so ``check`` must reject it.

Usage:
    PYTHONPATH=src pytest tests/domain/rules/test_validator_builder_entity.py
"""

from __future__ import annotations

import pytest

from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec
from domain.rules.validator import check


def _spec(form_type: str, rule_kind: RuleKind = RuleKind.FORM_ELEMENT) -> RuleSpec:
    return RuleSpec(
        rule_kind=rule_kind,
        intent="show only when place of delivery is hospital",
        form_name="Egg Demand Encounter",
        form_type=form_type,
        available_concepts=["Place of delivery"],
        available_encounter_types=["Egg Demand"],
        concept_answers={"Place of delivery": ["Hospital"]},
    )


def _result(js: str, rule_kind: RuleKind = RuleKind.FORM_ELEMENT) -> RuleResult:
    return RuleResult(rule_kind=rule_kind, js=js, confidence="high")


def _form_element_rule(entity_variable: str, builder_argument: str) -> str:
    return f"""\
"use strict";
({{params, imports}}) => {{
  const {entity_variable} = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({builder_argument});
  statusBuilder.show().when.valueInEncounter("Place of delivery").containsAnswerConceptName("Hospital");
  return statusBuilder.build();
}};
"""


def _wrong_form_type_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if w.startswith("wrong form type")]


# ── Rejected ─────────────────────────────────────────────────────────────────


def test_general_visit_rule_built_as_programme_visit_is_rejected() -> None:
    js = _form_element_rule("encounter", "{programEncounter: encounter, formElement}")
    ok, warnings = check(_result(js), _spec("Encounter"))
    assert ok is False
    assert _wrong_form_type_warnings(warnings) == [
        "wrong form type in FormElementStatusBuilder: the form's record is passed "
        "as 'programEncounter', but a Encounter form needs 'encounter'"
    ]


def test_programme_visit_rule_built_as_general_visit_is_rejected() -> None:
    js = _form_element_rule("programEncounter", "{encounter: programEncounter, formElement}")
    ok, warnings = check(_result(js), _spec("ProgramEncounter"))
    assert ok is False
    assert len(_wrong_form_type_warnings(warnings)) == 1


def test_quoted_key_and_destructured_builder_are_checked() -> None:
    js = """\
"use strict";
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  const {FormElementStatusBuilder} = imports.rulesConfig;
  const statusBuilder = new FormElementStatusBuilder({"programEncounter": encounter, formElement});
  statusBuilder.show().when.valueInEncounter("Place of delivery").containsAnswerConceptName("Hospital");
  return statusBuilder.build();
};
"""
    ok, warnings = check(_result(js), _spec("Encounter"))
    assert ok is False
    assert len(_wrong_form_type_warnings(warnings)) == 1


@pytest.mark.parametrize(
    "condition_class",
    ["imports.rulesConfig.RuleCondition", "RuleCondition"],
)
def test_rule_condition_built_for_the_wrong_form_type_is_rejected(condition_class: str) -> None:
    js = f"""\
"use strict";
({{params, imports}}) => {{
  const encounter = params.entity;
  const formElement = params.formElement;
  const {{RuleCondition}} = imports.rulesConfig;
  const visible = new {condition_class}({{programEncounter: encounter, formElement}})
    .when.valueInEncounter("Place of delivery").containsAnswerConceptName("Hospital").matches();
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visible, null);
}};
"""
    ok, warnings = check(_result(js), _spec("Encounter"))
    assert ok is False
    assert _wrong_form_type_warnings(warnings) == [
        "wrong form type in RuleCondition: the form's record is passed "
        "as 'programEncounter', but a Encounter form needs 'encounter'"
    ]


@pytest.mark.parametrize(
    ("binding", "builder_argument"),
    [
        ("", "{programEncounter: params.entity, formElement}"),
        ("const {entity} = params;", "{programEncounter: entity, formElement}"),
        ("const {entity: visit} = params;", "{programEncounter: visit, formElement}"),
        ("const encounter = params.entity; const visit = encounter;", "{programEncounter: visit, formElement}"),
    ],
)
def test_form_record_reached_another_way_is_still_checked(binding: str, builder_argument: str) -> None:
    js = f"""\
"use strict";
({{params, imports}}) => {{
  {binding}
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({builder_argument});
  statusBuilder.show().when.valueInEncounter("Place of delivery").containsAnswerConceptName("Hospital");
  return statusBuilder.build();
}};
"""
    ok, warnings = check(_result(js), _spec("Encounter"))
    assert ok is False
    assert len(_wrong_form_type_warnings(warnings)) == 1


# ── Accepted ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("form_type", "entity_variable", "builder_argument"),
    [
        ("Encounter", "encounter", "{encounter, formElement}"),
        ("Encounter", "encounter", "{encounter: encounter, formElement}"),
        ("ProgramEncounter", "programEncounter", "{programEncounter, formElement}"),
        ("ProgramEnrolment", "programEnrolment", "{programEnrolment, formElement}"),
        ("IndividualProfile", "individual", "{individual, formElement}"),
        ("Encounter", "encounter", "{encounter: params.entity, formElement}"),
    ],
)
def test_builder_given_the_forms_own_entity_key_is_accepted(
    form_type: str, entity_variable: str, builder_argument: str,
) -> None:
    js = _form_element_rule(entity_variable, builder_argument)
    ok, warnings = check(_result(js), _spec(form_type))
    assert ok is True
    assert _wrong_form_type_warnings(warnings) == []


def test_derived_record_under_another_key_is_accepted() -> None:
    js = """\
"use strict";
({params, imports}) => {
  const programEncounter = params.entity;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({
    programEnrolment: programEncounter.programEnrolment,
  });
  scheduleBuilder.add({name: "Egg Demand", encounterType: "Egg Demand", earliestDate: new Date(), maxDate: new Date()});
  return scheduleBuilder.getAll();
};
"""
    ok, warnings = check(
        _result(js, RuleKind.VISIT_SCHEDULE),
        _spec("ProgramEncounter", RuleKind.VISIT_SCHEDULE),
    )
    assert _wrong_form_type_warnings(warnings) == []
    assert ok is True


def test_rule_condition_given_the_forms_own_entity_key_is_accepted() -> None:
    js = """\
"use strict";
({params, imports}) => {
  const programEncounter = params.entity;
  const formElement = params.formElement;
  const visible = new imports.rulesConfig.RuleCondition({programEncounter, formElement})
    .when.valueInEncounter("Place of delivery").containsAnswerConceptName("Hospital").matches();
  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visible, null);
};
"""
    ok, warnings = check(_result(js), _spec("ProgramEncounter"))
    assert ok is True
    assert _wrong_form_type_warnings(warnings) == []

"""Rules read the answers of the form being filled in with the method that
reads that form type.

rules-config's valueInEncounter reads a visit's answers. On a registration,
enrolment or exit form there is no visit, so it finds no answer and every
condition on it is false: a field shown "when X is Active" is never shown.
The generator is told each form type's method, and the checker rejects a
visit read on a form without a visit.

Usage:
    PYTHONPATH=src pytest tests/domain/rules/test_current_form_reads.py
"""

from __future__ import annotations

import pytest

from domain.rules.prompts import (
    build_field_batch_system_prompt,
    build_system_prompt,
    build_user_prompt,
    current_form_value_method,
)
from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec
from domain.rules.validator import WRONG_FORM_TYPE_WARNING, check

_ENTITY = {
    "IndividualProfile": "individual",
    "ProgramEnrolment": "programEnrolment",
    "ProgramExit": "programEnrolment",
    "Encounter": "encounter",
    "ProgramEncounter": "programEncounter",
    "IndividualEncounterCancellation": "encounter",
    "ProgramEncounterCancellation": "programEncounter",
}


def _spec(form_type: str, **extra) -> RuleSpec:
    fields = {
        "rule_kind": RuleKind.FORM_ELEMENT,
        "intent": "show Coordinator name when Operational Status is Active",
        "form_name": "School Registration",
        "form_type": form_type,
        "available_concepts": ["Operational Status", "Coordinator name"],
        "concept_answers": {"Operational Status": ["Active", "Closed"]},
    }
    return RuleSpec(**{**fields, **extra})


def _rule(form_type: str, condition: str) -> RuleResult:
    entity = _ENTITY[form_type]
    js = f"""\
"use strict";
({{params, imports}}) => {{
  const {entity} = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({{{entity}, formElement}});
  statusBuilder.show().when.{condition};
  return statusBuilder.build();
}};
"""
    return RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high")


def _wrong_form_type(warnings: list[str]) -> list[str]:
    return [w for w in warnings if w.startswith(WRONG_FORM_TYPE_WARNING)]


@pytest.mark.parametrize(("form_type", "method"), [
    ("IndividualProfile", "valueInRegistration"),
    ("ProgramEnrolment", "valueInEnrolment"),
    ("ProgramExit", "valueInExit"),
    ("Encounter", "valueInEncounter"),
    ("ProgramEncounter", "valueInEncounter"),
    ("IndividualEncounterCancellation", "valueInCancelEncounter"),
    ("ProgramEncounterCancellation", "valueInCancelEncounter"),
])
def test_each_form_type_reads_its_own_answers_with_its_method(form_type: str, method: str) -> None:
    assert current_form_value_method(form_type) == method


@pytest.mark.parametrize(("form_type", "kind", "method"), [
    ("IndividualProfile", "registration", "valueInRegistration"),
    ("ProgramEnrolment", "programme enrolment", "valueInEnrolment"),
    ("ProgramExit", "programme exit", "valueInExit"),
])
@pytest.mark.parametrize("visit_read", ["valueInEncounter", "valueInCancelEncounter"])
def test_a_visit_read_on_a_form_without_a_visit_is_rejected(
    form_type: str, kind: str, method: str, visit_read: str,
) -> None:
    ok, warnings = check(
        _rule(form_type, f'{visit_read}("Operational Status").containsAnswerConceptName("Active")'),
        _spec(form_type),
    )
    assert ok is False
    assert _wrong_form_type(warnings) == [
        f"{WRONG_FORM_TYPE_WARNING}: {visit_read} reads a visit's answers, but this is a {kind} form, "
        f"so it finds no answer and the condition is never true; read this form's answers with {method}"
    ]


@pytest.mark.parametrize("form_type", list(_ENTITY))
def test_the_forms_own_method_is_accepted(form_type: str) -> None:
    method = current_form_value_method(form_type)
    ok, warnings = check(
        _rule(form_type, f'{method}("Operational Status").containsAnswerConceptName("Active")'),
        _spec(form_type),
    )
    assert ok is True, warnings
    assert warnings == []


@pytest.mark.parametrize("form_type", ["Encounter", "ProgramEncounter"])
def test_a_visit_form_still_reads_with_value_in_encounter(form_type: str) -> None:
    ok, warnings = check(
        _rule(form_type, 'valueInEncounter("Operational Status").containsAnswerConceptName("Active")'), _spec(form_type),
    )
    assert ok is True
    assert _wrong_form_type(warnings) == []


def test_a_registration_form_may_still_read_the_last_visit() -> None:
    ok, warnings = check(
        _rule("IndividualProfile", 'valueInLastEncounter("Operational Status", ["Monthly Visit"]).defined'),
        _spec("IndividualProfile", available_encounter_types=["Monthly Visit"]),
    )
    assert _wrong_form_type(warnings) == []


@pytest.mark.parametrize(("form_type", "method"), [
    ("IndividualProfile", "valueInRegistration"),
    ("ProgramEnrolment", "valueInEnrolment"),
])
def test_the_forms_own_read_is_checked_against_its_fields(form_type: str, method: str) -> None:
    ok, warnings = check(
        _rule(form_type, f'{method}("Operational state").containsAnswerConceptName("Active")'), _spec(form_type),
    )
    assert ok is False
    assert "off-bundle concept names: ['Operational state']" in warnings

    ok, warnings = check(
        _rule(form_type, f'{method}("operational status").containsAnswerConceptName("Active")'), _spec(form_type),
    )
    assert ok is False
    assert any("Avni matches names exactly" in w for w in warnings)


def test_a_registration_read_on_a_visit_form_is_still_not_checked_against_the_visit_form() -> None:
    ok, warnings = check(
        _rule("Encounter", 'valueInRegistration("Date of birth").defined'), _spec("Encounter"),
    )
    assert ok is True, warnings


def test_a_question_group_question_on_a_registration_form_must_be_read_with_its_group() -> None:
    spec = _spec(
        "IndividualProfile",
        available_concepts=["Operational Status", "Contact details"],
        question_group_children={"Contact details": ["Mobile"]},
    )
    ok, warnings = check(_rule("IndividualProfile", 'valueInRegistration("Mobile").defined'), spec)
    assert ok is False
    assert warnings == [
        "question group read: 'Mobile' is a question inside the question group 'Contact details'; "
        "read it with the group, e.g. valueInRegistration('Mobile', 'Contact details')"
    ]

    ok, warnings = check(_rule("IndividualProfile", 'valueInRegistration("Mobile", "Contact details").defined'), spec)
    assert ok is True, warnings


@pytest.mark.parametrize("rule_kind", list(RuleKind))
@pytest.mark.parametrize("form_type", ["IndividualProfile", "ProgramEnrolment", "ProgramExit"])
def test_the_generator_is_told_the_forms_own_method(rule_kind: RuleKind, form_type: str) -> None:
    method = current_form_value_method(form_type)
    for prompt in (build_system_prompt(rule_kind, form_type), build_field_batch_system_prompt(form_type)):
        assert f".when.{method}('<concept>')" in prompt
        assert f"show().when.{method}('X').not.containsAnswerConceptName('No')" in prompt
        assert "valueInEncounter(" not in prompt


def test_a_visit_forms_prompt_keeps_value_in_encounter() -> None:
    prompt = build_system_prompt(RuleKind.FORM_ELEMENT, "Encounter")
    assert ".when.valueInEncounter('<concept>')" in prompt
    assert "show().when.valueInEncounter('X').not.containsAnswerConceptName('No')" in prompt


def test_the_target_field_on_a_registration_form_is_read_with_value_in_registration() -> None:
    prompt = build_user_prompt(
        _spec("IndividualProfile", target_field_concept="Coordinator name"), "(none)", "(none)",
    )
    assert "Read its value as individual.getObservationReadableValue('Coordinator name') " \
           "or valueInRegistration('Coordinator name')." in prompt
    assert "valueInEncounter" not in prompt


def test_question_groups_on_an_enrolment_form_are_read_with_value_in_enrolment() -> None:
    prompt = build_user_prompt(
        _spec("ProgramEnrolment", question_group_children={"Contact details": ["Mobile"]}), "(none)", "(none)",
    )
    assert "valueInEnrolment('<question concept>', '<group concept>')" in prompt
    assert "valueInEncounter" not in prompt


def test_an_exit_form_offers_no_rule_condition_read_for_a_group_question() -> None:
    """valueInExit takes no group argument, so only the record read is offered."""
    prompt = build_user_prompt(
        _spec("ProgramExit", question_group_children={"Contact details": ["Mobile"]},
              target_field_concept="Mobile", target_question_group="Contact details"),
        "(none)", "(none)",
    )
    assert "programEnrolment.getObservationReadableValue('<question concept>', '<group concept>')." in prompt
    assert "read its value as programEnrolment.getObservationReadableValue('Mobile', 'Contact details')." in prompt
    assert "valueInExit(" not in prompt


@pytest.mark.parametrize("form_type", list(_ENTITY))
def test_a_made_up_name_in_a_visit_read_is_off_bundle_on_every_form(form_type: str) -> None:
    """Off-bundle on every form type, so the chat's wrong-form-type retry, which could swap in another
    field, does not run for it."""
    ok, warnings = check(
        _rule(form_type, 'valueInEncounter("Ghost Field").containsAnswerConceptName("Active")'), _spec(form_type),
    )
    assert ok is False
    assert "off-bundle concept names: ['Ghost Field']" in warnings


@pytest.mark.parametrize("form_type", ["ProgramEnrolment", "ProgramExit", "IndividualEncounterCancellation"])
def test_the_generator_may_use_the_forms_method_even_when_no_helper_lists_it(form_type: str) -> None:
    method = current_form_value_method(form_type)
    assert f"listed under HELPERS in the user message,\n     and `{method}` from constraint 1." in \
        build_system_prompt(RuleKind.FORM_ELEMENT, form_type)
    assert f"listed under HELPERS, and `{method}`\n     from constraint 1." in build_field_batch_system_prompt(form_type)

"""Unit tests for the `suggest_*` rule tools in `src/chat/tools.py`.

The generator is stubbed; the real validator runs, so a rule built for the
wrong form type comes back rejected without any code.

Usage:
    pytest tests/test_chat_rule_tools.py
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import chat.tools as tools
from domain.rules.rule_spec import RuleResult

_WRONG_FORM_TYPE_JS = """\
"use strict";
({params, imports}) => {
  const encounter = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({programEncounter: encounter, formElement});
  statusBuilder.show().when.valueInEncounter("Eggs needed").containsAnswerConceptName("Yes");
  return statusBuilder.build();
};
"""

_GENERAL_VISIT_JS = _WRONG_FORM_TYPE_JS.replace(
    "{programEncounter: encounter, formElement}", "{encounter, formElement}",
)

_CONTEXT = {
    "form_type": "Encounter",
    "subject_type": "Household",
    "program": None,
    "encounter_type": "Egg Demand",
    "available_concepts": ["Eggs needed"],
    "available_encounter_types": ["Egg Demand"],
    "available_programs": [],
    "concept_answers": {"Eggs needed": ["Yes", "No"]},
}


@pytest.fixture
def rule_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(tools, "load_form_rule_context", lambda _bundle, _form, _form_uuid=None: _CONTEXT)
    session = SimpleNamespace(bundle_path=tmp_path / "bundle.zip")
    return {"configurable": {"session": session}}


_ORIGINAL_INTENT_EXAMPLES = object()


@pytest.fixture(autouse=True)
def _stub_retrieval(monkeypatch: pytest.MonkeyPatch) -> list:
    retrieved_for = []

    def retrieve(spec):
        retrieved_for.append(spec)
        return _ORIGINAL_INTENT_EXAMPLES

    monkeypatch.setattr(tools._rule_generator, "retrieve", retrieve)
    return retrieved_for


def _stub_generator(monkeypatch: pytest.MonkeyPatch, js: str) -> None:
    monkeypatch.setattr(
        tools._rule_generator,
        "generate",
        lambda spec, context=None: RuleResult(rule_kind=spec.rule_kind, js=js, confidence="high"),
    )


def _field_rule(config: dict) -> dict:
    return tools.suggest_form_element_rule.func(
        form_name="Egg Demand Encounter",
        page_name="Details",
        field_name="Number of eggs",
        intent="show only when 'Eggs needed' is Yes",
        config=config,
    )


def test_rejected_field_rule_returns_reasons_and_no_code(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    _stub_generator(monkeypatch, _WRONG_FORM_TYPE_JS)
    result = _field_rule(rule_config)
    assert result["status"] == "rejected"
    assert "js" not in result
    assert any(w.startswith("wrong form type") for w in result["warnings"])


def test_rejected_form_rule_returns_no_code(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    _stub_generator(monkeypatch, "")
    result = tools.suggest_form_rule.func(
        form_name="Egg Demand Encounter",
        rule_kind="validationRule",
        intent="block save when 'Eggs needed' is empty",
        config=rule_config,
    )
    assert result["status"] == "rejected"
    assert "js" not in result


def test_general_visit_rule_built_for_a_general_visit_is_suggested(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    _stub_generator(monkeypatch, _GENERAL_VISIT_JS)
    result = _field_rule(rule_config)
    assert result["status"] == "suggested"
    assert result["js"] == _GENERAL_VISIT_JS


def _stub_generator_sequence(monkeypatch: pytest.MonkeyPatch, *scripts: str) -> list:
    specs = []
    remaining = list(scripts)

    def generate(spec, context=None):
        specs.append((spec, context))
        return RuleResult(rule_kind=spec.rule_kind, js=remaining.pop(0), confidence="high")

    monkeypatch.setattr(tools._rule_generator, "generate", generate)
    return specs


def test_rule_built_for_another_form_type_is_regenerated_once_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    specs = _stub_generator_sequence(monkeypatch, _WRONG_FORM_TYPE_JS, _GENERAL_VISIT_JS)

    result = _field_rule(rule_config)

    assert result["status"] == "suggested"
    assert result["js"] == _GENERAL_VISIT_JS
    assert len(specs) == 2
    (first, first_context), (retry, retry_context) = specs
    assert "wrong form type" in retry.intent
    assert "`encounter`" in retry.intent
    assert retry.intent.startswith(first.intent)
    assert first_context is _ORIGINAL_INTENT_EXAMPLES
    assert retry_context is _ORIGINAL_INTENT_EXAMPLES


def test_both_attempts_use_one_retrieval_for_the_users_own_intent(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict, _stub_retrieval: list,
) -> None:
    _stub_generator_sequence(monkeypatch, _WRONG_FORM_TYPE_JS, _GENERAL_VISIT_JS)

    _field_rule(rule_config)

    assert [spec.intent for spec in _stub_retrieval] == ["show only when 'Eggs needed' is Yes"]


def test_rule_is_generated_without_retry_when_examples_cannot_be_retrieved_up_front(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    specs = _stub_generator_sequence(monkeypatch, _WRONG_FORM_TYPE_JS)

    def failing_retrieve(spec):
        raise RuntimeError("KB retrieval failed")

    monkeypatch.setattr(tools._rule_generator, "retrieve", failing_retrieve)

    result = _field_rule(rule_config)

    assert result["status"] == "rejected"
    assert len(specs) == 1


def test_regenerated_rule_still_built_for_another_form_type_is_rejected(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    specs = _stub_generator_sequence(monkeypatch, _WRONG_FORM_TYPE_JS, _WRONG_FORM_TYPE_JS)

    result = _field_rule(rule_config)

    assert result["status"] == "rejected"
    assert "js" not in result
    assert len(specs) == 2


def test_rule_that_also_reads_a_field_not_on_the_form_is_not_regenerated(
    monkeypatch: pytest.MonkeyPatch, rule_config: dict,
) -> None:
    wrong_type_and_field = _WRONG_FORM_TYPE_JS.replace('"Eggs needed"', '"Hens owned"')
    specs = _stub_generator_sequence(monkeypatch, wrong_type_and_field)

    result = _field_rule(rule_config)

    assert result["status"] == "rejected"
    assert len(specs) == 1
    assert any(w.startswith("off-bundle concept names") for w in result["warnings"])


_REGISTRATION_CONTEXT = {
    **_CONTEXT,
    "form_type": "IndividualProfile",
    "encounter_type": None,
    "available_concepts": ["Operational Status", "Coordinator name"],
    "concept_answers": {"Operational Status": ["Active", "Closed"]},
}

_REGISTRATION_JS = """\
"use strict";
({params, imports}) => {
  const individual = params.entity;
  const formElement = params.formElement;
  const statusBuilder = new imports.rulesConfig.FormElementStatusBuilder({individual, formElement});
  statusBuilder.show().when.valueInRegistration("Operational Status").containsAnswerConceptName("Active");
  return statusBuilder.build();
};
"""
_REGISTRATION_JS_READING_A_VISIT = _REGISTRATION_JS.replace("valueInRegistration", "valueInEncounter")


def _registration_field_rule(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    monkeypatch.setattr(tools, "load_form_rule_context", lambda _bundle, _form, _form_uuid=None: _REGISTRATION_CONTEXT)
    config = {"configurable": {"session": SimpleNamespace(bundle_path=tmp_path / "bundle.zip")}}
    return tools.suggest_form_element_rule.func(
        form_name="School Registration",
        page_name="Details",
        field_name="Coordinator name",
        intent="show only when Operational Status is Active",
        config=config,
    )


def test_a_registration_rule_reading_a_visit_is_regenerated_with_the_registration_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """valueInEncounter finds nothing on a registration form, so the field would never show."""
    specs = _stub_generator_sequence(monkeypatch, _REGISTRATION_JS_READING_A_VISIT, _REGISTRATION_JS)
    result = _registration_field_rule(monkeypatch, tmp_path)
    assert result["status"] == "suggested"
    assert result["js"] == _REGISTRATION_JS
    assert "read this form's answers with valueInRegistration" in specs[1][0].intent
    assert "read this form's answers with `valueInRegistration`." in specs[1][0].intent


def test_a_registration_rule_still_reading_a_visit_after_the_retry_shows_no_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _stub_generator_sequence(monkeypatch, _REGISTRATION_JS_READING_A_VISIT, _REGISTRATION_JS_READING_A_VISIT)
    result = _registration_field_rule(monkeypatch, tmp_path)
    assert result["status"] == "rejected"
    assert "js" not in result
    assert any("never true" in w for w in result["warnings"])

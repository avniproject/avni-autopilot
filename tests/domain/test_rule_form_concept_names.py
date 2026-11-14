"""Unit tests for the field names a rule on an organisation's own form may use.

App Designer lets a question's name differ from its concept's name, and Avni
reads observations by concept name. `load_form_rule_context` must therefore
offer, and `validator.check` must accept, concept names.

Usage:
    uv run --extra dev pytest tests/domain/test_rule_form_concept_names.py
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import chat.tools as tools
from domain.bundle_editor import list_bundle_fields, load_form_rule_context
from domain.rules.prompts import build_user_prompt
from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec
from domain.rules.validator import check

_FORM_NAME = "Child Visit"
_SUBJECT_UUID = "subject-child"
_ENCOUNTER_TYPE_UUID = "encounter-child-visit"


def _element(question: str, concept: dict | None) -> dict:
    element = {"uuid": f"fe-{question}", "name": question}
    if concept is not None:
        element["concept"] = concept
    return element


def _write_bundle(root: Path) -> Path:
    form = {
        "uuid": "form-child-visit",
        "name": _FORM_NAME,
        "formType": "Encounter",
        "formElementGroups": [{
            "name": "Details",
            "formElements": [
                _element("Weight of the child (kg)", {"uuid": "c-weight", "name": "Weight", "dataType": "Numeric"}),
                _element("Is the child sick today?", {
                    "uuid": "c-sick",
                    "name": "Child sick",
                    "dataType": "Coded",
                    "answers": [{"name": "Yes"}, {"name": "No"}],
                }),
                _element("Remarks", {"uuid": "c-remarks", "name": "Remarks", "dataType": "Text"}),
            ],
        }],
    }
    files = {
        "subjectTypes.json": [{"uuid": _SUBJECT_UUID, "name": "Child"}],
        "programs.json": [],
        "encounterTypes.json": [{"uuid": _ENCOUNTER_TYPE_UUID, "name": "Child Visit"}],
        "formMappings.json": [{
            "uuid": "mapping-child-visit",
            "formUUID": form["uuid"],
            "subjectTypeUUID": _SUBJECT_UUID,
            "encounterTypeUUID": _ENCOUNTER_TYPE_UUID,
        }],
    }
    for name, content in files.items():
        (root / name).write_text(json.dumps(content), encoding="utf-8")
    forms_dir = root / "forms"
    forms_dir.mkdir()
    (forms_dir / "child-visit.json").write_text(json.dumps(form), encoding="utf-8")
    return root


def _spec_from_bundle(root: Path) -> RuleSpec:
    context = load_form_rule_context(str(_write_bundle(root)), _FORM_NAME)
    return RuleSpec(
        rule_kind=RuleKind.FORM_ELEMENT,
        intent="show only when the condition holds",
        form_name=_FORM_NAME,
        form_type=context["form_type"],
        available_concepts=context["available_concepts"],
        concept_answers=context["concept_answers"],
        question_names_by_concept=context["question_names_by_concept"],
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


def test_context_lists_concept_names_not_question_names(tmp_path: Path) -> None:
    context = load_form_rule_context(str(_write_bundle(tmp_path)), _FORM_NAME)

    assert context["available_concepts"] == ["Child sick", "Remarks", "Weight"]
    assert context["concept_answers"] == {"Child sick": ["Yes", "No"]}
    assert context["question_names_by_concept"] == {
        "Weight": ["Weight of the child (kg)"],
        "Child sick": ["Is the child sick today?"],
    }


def test_generator_is_told_which_question_each_concept_is_asked_as(tmp_path: Path) -> None:
    prompt = build_user_prompt(_spec_from_bundle(tmp_path), helpers_text="", examples_text="")

    assert '- Weight  (question on the form: "Weight of the child (kg)"; the rule must use "Weight")' in prompt
    assert '- Child sick  (question on the form: "Is the child sick today?"; the rule must use "Child sick")' in prompt
    assert "\n- Remarks\n" in prompt


def test_bundle_fields_show_each_question_with_its_concept_name(tmp_path: Path) -> None:
    fields = [
        (field["name"], field["conceptName"])
        for form in list_bundle_fields(str(_write_bundle(tmp_path)))["forms"]
        for section in form["sections"]
        for field in section["fields"]
    ]

    assert fields == [
        ("Weight of the child (kg)", "Weight"),
        ("Is the child sick today?", "Child sick"),
        ("Remarks", "Remarks"),
    ]


def test_rule_reading_the_concept_name_is_accepted(tmp_path: Path) -> None:
    ok, warnings = check(_rule('valueInEncounter("Weight").is.lessThan(7)'), _spec_from_bundle(tmp_path))

    assert ok is True
    assert warnings == []


def test_rule_reading_the_question_name_is_rejected_since_avni_finds_no_value_under_it(tmp_path: Path) -> None:
    ok, warnings = check(_rule('valueInEncounter("Weight of the child (kg)").is.lessThan(7)'), _spec_from_bundle(tmp_path))

    assert ok is False
    assert "off-bundle concept names: ['Weight of the child (kg)']" in warnings


def test_answers_of_a_coded_question_are_checked_under_its_concept_name(tmp_path: Path) -> None:
    spec = _spec_from_bundle(tmp_path)

    ok, _ = check(_rule('valueInEncounter("Child sick").containsAnswerConceptName("Yes")'), spec)
    off_list_ok, off_list_warnings = check(_rule('valueInEncounter("Child sick").containsAnswerConceptName("Maybe")'), spec)

    assert ok is True
    assert off_list_ok is False
    assert any("off-bundle answer(s) ['Maybe'] for concept 'Child sick'" in warning for warning in off_list_warnings)


def test_pasted_organisation_rule_asked_for_a_small_change_is_suggested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pasted_rule_with_change = _rule('valueInEncounter("Weight").is.lessThan(8)').js
    monkeypatch.setattr(tools._rule_generator, "retrieve", lambda spec: object())
    monkeypatch.setattr(
        tools._rule_generator,
        "generate",
        lambda spec, context=None: RuleResult(rule_kind=spec.rule_kind, js=pasted_rule_with_change, confidence="high"),
    )
    config = {"configurable": {"session": SimpleNamespace(bundle_path=_write_bundle(tmp_path))}}

    result = tools.suggest_form_element_rule.func(
        form_name=_FORM_NAME,
        page_name="Details",
        field_name="Remarks",
        intent="same rule, but show Remarks when Weight is below 8 instead of 7",
        config=config,
    )

    assert result["status"] == "suggested", result.get("warnings")
    assert result["js"] == pasted_rule_with_change


def test_questions_inside_a_question_group_are_not_offered_on_their_own(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    form_path = bundle / "forms" / "child-visit.json"
    form = json.loads(form_path.read_text(encoding="utf-8"))
    form["formElementGroups"][0]["formElements"] += [
        _element("Medicines given", {"uuid": "c-medicines", "name": "Medicines", "dataType": "QuestionGroup"}),
        {
            **_element("Medicine name", {
                "uuid": "c-medicine-name", "name": "Medicine name", "dataType": "Coded",
                "answers": [{"name": "ORS"}, {"name": "Zinc"}],
            }),
            "parentFormElementUuid": "fe-Medicines given",
        },
    ]
    form_path.write_text(json.dumps(form), encoding="utf-8")

    context = load_form_rule_context(str(bundle), _FORM_NAME)

    assert "Medicines" in context["available_concepts"]
    assert "Medicine name" not in context["available_concepts"]
    assert "Medicine name" not in context["concept_answers"]


def test_generator_is_told_to_pass_the_record_under_the_form_types_key() -> None:
    from domain.rules.prompts import build_system_prompt

    prompt = build_system_prompt(RuleKind.FORM_ELEMENT, "encounter")

    assert "new imports.rulesConfig.FormElementStatusBuilder({encounter, formElement})" in prompt
    assert "never copy that key" in prompt
    assert "copied\n     exactly, including case" in prompt


def _bundle_with_question_group(root: Path) -> Path:
    bundle = _write_bundle(root)
    form_path = bundle / "forms" / "child-visit.json"
    form = json.loads(form_path.read_text(encoding="utf-8"))
    form["formElementGroups"][0]["formElements"] += [
        _element("Vitals", {"uuid": "c-vitals", "name": "QA15CN Vitals", "dataType": "QuestionGroup"}),
        {**_element("Pulse rate", {"uuid": "c-pulse", "name": "QA15CN Pulse", "dataType": "Numeric"}), "parentFormElementUuid": "fe-Vitals"},
        {**_element("Temperature", {"uuid": "c-temp", "name": "QA15CN Temp", "dataType": "Numeric"}), "parentFormElementUuid": "fe-Vitals"},
    ]
    form_path.write_text(json.dumps(form), encoding="utf-8")
    return bundle


def _group_spec(root: Path, *, kind: RuleKind = RuleKind.VALIDATION, target_field: str | None = None) -> RuleSpec:
    return _spec_for_bundle(_bundle_with_question_group(root), kind=kind, target_field=target_field)


def _spec_for_bundle(bundle: Path, *, kind: RuleKind = RuleKind.VALIDATION, target_field: str | None = None) -> RuleSpec:
    context = load_form_rule_context(str(bundle), _FORM_NAME)
    return RuleSpec(
        rule_kind=kind,
        intent="x",
        form_name=_FORM_NAME,
        form_type=context["form_type"],
        available_concepts=context["available_concepts"],
        concept_answers=context["concept_answers"],
        question_group_children=context["question_group_children"],
        question_group_answers=context["question_group_answers"],
        repeatable_question_groups=context["repeatable_question_groups"],
        question_names_by_concept=context["question_names_by_concept"],
        target_question_group=context["question_group_by_question"].get("Details", {}).get(target_field),
    )


def _validation_rule(read: str) -> RuleResult:
    js = f"""\
"use strict";
({{params, imports}}) => {{
  const encounter = params.entity;
  const validationResults = [];
  const pulse = {read};
  if (pulse > 1) validationResults.push(imports.common.createValidationError("Pulse too high"));
  return validationResults;
}};
"""
    return RuleResult(rule_kind=RuleKind.VALIDATION, js=js, confidence="high")


def test_context_lists_question_group_children_by_group(tmp_path: Path) -> None:
    context = load_form_rule_context(str(_bundle_with_question_group(tmp_path)), _FORM_NAME)

    assert context["question_group_children"] == {"QA15CN Vitals": ["QA15CN Pulse", "QA15CN Temp"]}
    assert context["question_group_by_question"] == {"Details": {"Pulse rate": "QA15CN Vitals", "Temperature": "QA15CN Vitals"}}
    assert "QA15CN Pulse" not in context["available_concepts"]


def test_question_read_with_its_group_is_accepted(tmp_path: Path) -> None:
    ok, warnings = check(_validation_rule('encounter.getObservationReadableValue("QA15CN Pulse", "QA15CN Vitals")'), _group_spec(tmp_path))
    assert ok is True, warnings


def test_question_read_without_its_group_is_rejected(tmp_path: Path) -> None:
    ok, warnings = check(_validation_rule('encounter.getObservationReadableValue("QA15CN Pulse")'), _group_spec(tmp_path))
    assert ok is False
    assert warnings == [
        "question group read: 'QA15CN Pulse' is a question inside the question group 'QA15CN Vitals'; "
        "read it with the group, e.g. getObservationReadableValue('QA15CN Pulse', 'QA15CN Vitals')"
    ]


def test_question_read_with_another_group_or_a_question_not_in_it_is_rejected(tmp_path: Path) -> None:
    spec = _group_spec(tmp_path)
    ok, warnings = check(_validation_rule('encounter.getObservationValue("QA15CN Pulse", "Vitals")'), spec)
    assert ok is False
    assert "off-bundle question group 'Vitals': no question group with that concept name on this form" in warnings
    ok, warnings = check(_validation_rule('encounter.findObservation("Weight", "QA15CN Vitals")'), spec)
    assert ok is False
    assert "off-bundle concept name 'Weight' in question group 'QA15CN Vitals': the group holds ['QA15CN Pulse', 'QA15CN Temp']" in warnings


def test_rule_on_a_group_field_reading_a_runtime_name_without_the_group_is_rejected(tmp_path: Path) -> None:
    spec = _group_spec(tmp_path, kind=RuleKind.FORM_ELEMENT, target_field="Pulse rate")
    without_group = _rule('valueInEncounter("Weight").is.lessThan(7)').js.replace(
        "return statusBuilder.build();",
        "if (encounter.getObservationReadableValue(formElement.concept.name) > 1) statusBuilder.show();\n  return statusBuilder.build();",
    )
    with_group = without_group.replace("(formElement.concept.name)", '(formElement.concept.name, "QA15CN Vitals")')

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=without_group, confidence="high"), spec)
    assert ok is False
    assert any(w.startswith("question group read: getObservationReadableValue(...)") for w in warnings)

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=with_group, confidence="high"), spec)
    assert ok is True, warnings


def test_concept_read_by_uuid_is_not_rejected(tmp_path: Path) -> None:
    ok, warnings = check(_validation_rule('encounter.getObservationReadableValue("6a1c1c2e-1c5b-4f0e-9b8a-2d4c1f0e7a11")'), _group_spec(tmp_path))
    assert ok is True, warnings


def test_generator_is_told_how_to_read_question_group_children(tmp_path: Path) -> None:
    prompt = build_user_prompt(_group_spec(tmp_path, kind=RuleKind.FORM_ELEMENT, target_field="Pulse rate"), helpers_text="", examples_text="")

    assert "QUESTION_GROUPS" in prompt
    assert "encounter.getObservationReadableValue('<question concept>', '<group concept>')" in prompt
    assert "- QA15CN Vitals:\n    - QA15CN Pulse  (question on the form: \"Pulse rate\")\n" in prompt
    assert "The field this rule is set on is inside the question group 'QA15CN Vitals'" in prompt


def test_prompt_has_no_question_group_block_for_a_form_without_groups(tmp_path: Path) -> None:
    prompt = build_user_prompt(_spec_from_bundle(tmp_path), helpers_text="", examples_text="")

    assert "QUESTION_GROUPS" not in prompt
    assert "CONCEPT_ANSWERS\n" in prompt and "\n\nHELPERS\n" in prompt


def test_pasted_working_rule_reading_a_group_question_with_its_group_is_suggested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pasted = _validation_rule('encounter.getObservationReadableValue("QA15CN Pulse", "QA15CN Vitals")').js
    monkeypatch.setattr(tools._rule_generator, "retrieve", lambda spec: object())
    monkeypatch.setattr(
        tools._rule_generator, "generate",
        lambda spec, context=None: RuleResult(rule_kind=spec.rule_kind, js=pasted, confidence="high"),
    )
    config = {"configurable": {"session": SimpleNamespace(bundle_path=_bundle_with_question_group(tmp_path))}}

    result = tools.suggest_form_rule.func(
        form_name=_FORM_NAME, rule_kind="validationRule",
        intent="block saving when 'QA15CN Pulse' in 'QA15CN Vitals' is above 1", config=config,
    )

    assert result["status"] == "suggested", result.get("warnings")



def _add_group(bundle: Path, group: dict, children: list[dict]) -> Path:
    form_path = bundle / "forms" / "child-visit.json"
    form = json.loads(form_path.read_text(encoding="utf-8"))
    form["formElementGroups"][0]["formElements"] += [group, *children]
    form_path.write_text(json.dumps(form), encoding="utf-8")
    return bundle


def test_questions_of_a_repeatable_group_are_not_offered_since_the_grouped_read_cannot_take_a_list(tmp_path: Path) -> None:
    bundle = _add_group(
        _write_bundle(tmp_path),
        {**_element("Medicines", {"uuid": "c-meds", "name": "QA15CN Medicines", "dataType": "QuestionGroup"}),
         "keyValues": [{"key": "repeatable", "value": True}]},
        [{**_element("Medicine", {"uuid": "c-med", "name": "QA15CN Medicine", "dataType": "Text"}), "parentFormElementUuid": "fe-Medicines"}],
    )
    spec = _spec_for_bundle(bundle)

    assert spec.question_group_children == {}
    assert spec.repeatable_question_groups == {"QA15CN Medicines": ["QA15CN Medicine"]}
    repeatable = "question group read: 'QA15CN Medicines' is a repeatable question group"
    for read in (
        'encounter.getObservationReadableValue("QA15CN Medicine", "QA15CN Medicines")',
        'encounter.getObservationReadableValue("QA15CN Medicine")',
    ):
        ok, warnings = check(_validation_rule(read), spec)
        assert ok is False
        assert any(w.startswith(repeatable) for w in warnings), warnings
        assert not any("no question group" in w or "off-bundle concept names" in w for w in warnings), warnings


def test_coded_group_question_answers_are_given_to_the_generator(tmp_path: Path) -> None:
    bundle = _add_group(
        _bundle_with_question_group(tmp_path),
        _element("Symptoms", {"uuid": "c-symptoms", "name": "QA15CN Symptoms", "dataType": "QuestionGroup"}),
        [{**_element("Fever?", {"uuid": "c-fever", "name": "QA15CN Fever", "dataType": "Coded", "answers": [{"name": "Yes"}, {"name": "No"}]}),
          "parentFormElementUuid": "fe-Symptoms"}],
    )

    prompt = build_user_prompt(_spec_for_bundle(bundle), helpers_text="", examples_text="")

    assert '    - QA15CN Fever  (question on the form: "Fever?"; answers: Yes, No)' in prompt


def test_grouped_read_with_a_miscased_group_or_question_names_the_bundle_spelling(tmp_path: Path) -> None:
    spec = _group_spec(tmp_path)
    _, group_warnings = check(_validation_rule('encounter.getObservationReadableValue("QA15CN Pulse", "qa15cn vitals")'), spec)
    _, question_warnings = check(_validation_rule('encounter.getObservationReadableValue("qa15cn pulse", "QA15CN Vitals")'), spec)

    assert any(w.endswith("Avni matches names exactly, the bundle has 'QA15CN Vitals'") for w in group_warnings)
    assert any(w.endswith("Avni matches names exactly, the bundle has 'QA15CN Pulse'") for w in question_warnings)


def test_enrolment_wide_read_with_the_group_as_a_later_argument_is_accepted(tmp_path: Path) -> None:
    ok, warnings = check(
        _validation_rule('params.entity.programEnrolment.findLatestObservationFromPreviousEncounters("QA15CN Pulse", encounter, "QA15CN Vitals")'),
        _group_spec(tmp_path),
    )
    assert ok is True, warnings


def test_rule_on_a_group_field_reading_with_value_in_encounter_and_its_group_is_accepted(tmp_path: Path) -> None:
    spec = _group_spec(tmp_path, kind=RuleKind.FORM_ELEMENT, target_field="Pulse rate")
    js = _rule('valueInEncounter(formElement.concept.name, "QA15CN Vitals").is.lessThan(7)').js

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high"), spec)

    assert ok is True, warnings


def test_rule_on_a_group_field_naming_another_group_is_rejected(tmp_path: Path) -> None:
    bundle = _add_group(
        _bundle_with_question_group(tmp_path),
        _element("Symptoms", {"uuid": "c-symptoms", "name": "QA15CN Symptoms", "dataType": "QuestionGroup"}),
        [{**_element("Fever?", {"uuid": "c-fever", "name": "QA15CN Fever", "dataType": "Text"}), "parentFormElementUuid": "fe-Symptoms"}],
    )
    spec = _spec_for_bundle(bundle, kind=RuleKind.FORM_ELEMENT, target_field="Pulse rate")
    js = _rule('valueInEncounter(formElement.concept.name, "QA15CN Symptoms").is.lessThan(7)').js

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high"), spec)

    assert ok is False
    assert any("reads this field with the group 'QA15CN Symptoms', but the field is inside 'QA15CN Vitals'" in w for w in warnings)


def test_rule_on_a_group_field_reading_with_value_in_encounter_without_its_group_is_rejected(tmp_path: Path) -> None:
    spec = _group_spec(tmp_path, kind=RuleKind.FORM_ELEMENT, target_field="Pulse rate")
    js = _rule('valueInEncounter(formElement.concept.name).is.lessThan(7)').js

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high"), spec)

    assert ok is False
    assert any("read it with getObservationReadableValue(<name>, 'QA15CN Vitals')" in w for w in warnings)


def test_group_of_the_rules_field_is_found_by_page_and_question(tmp_path: Path) -> None:
    context = load_form_rule_context(str(_bundle_with_question_group(tmp_path)), _FORM_NAME)

    assert context["question_group_by_question"].get("Details", {}).get("Pulse rate") == "QA15CN Vitals"
    assert context["question_group_by_question"].get("Other page", {}).get("Pulse rate") is None



def _coded_group_bundle(root: Path) -> Path:
    return _add_group(
        _bundle_with_question_group(root),
        _element("Pulse check", {"uuid": "c-pulse-check", "name": "QA15CN PulseCheck", "dataType": "QuestionGroup"}),
        [{**_element("Is pulse regular?", {"uuid": "c-pulse-reg", "name": "QA15CN PulseReg", "dataType": "Coded",
                                          "answers": [{"name": "Yes"}, {"name": "No"}]}),
          "parentFormElementUuid": "fe-Pulse check"}],
    )


def test_show_hide_rule_reading_a_coded_group_question_with_value_in_encounter_and_its_group_is_accepted(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path), kind=RuleKind.FORM_ELEMENT)

    ok, warnings = check(_rule('valueInEncounter("QA15CN PulseReg", "QA15CN PulseCheck").containsAnswerConceptName("No")'), spec)
    off_list_ok, off_list_warnings = check(_rule('valueInEncounter("QA15CN PulseReg", "QA15CN PulseCheck").containsAnswerConceptName("no")'), spec)

    assert ok is True, warnings
    assert off_list_ok is False
    assert any("off-bundle answer(s) ['no'] for concept 'QA15CN PulseReg'" in w for w in off_list_warnings)


def test_rule_set_on_a_repeatable_group_question_that_reads_other_fields_is_suggested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    bundle = _add_group(
        _write_bundle(tmp_path),
        {**_element("Kids", {"uuid": "c-kids", "name": "QA15CN Kids", "dataType": "QuestionGroup"}),
         "keyValues": [{"key": "repeatable", "value": "true"}]},
        [{**_element("Kid name", {"uuid": "c-kid", "name": "QA15CN Kid", "dataType": "Text"}), "parentFormElementUuid": "fe-Kids"}],
    )
    reads_other_field = _rule('valueInEncounter("Child sick").containsAnswerConceptName("Yes")').js
    monkeypatch.setattr(tools._rule_generator, "retrieve", lambda spec: object())
    monkeypatch.setattr(
        tools._rule_generator, "generate",
        lambda spec, context=None: RuleResult(rule_kind=spec.rule_kind, js=reads_other_field, confidence="high"),
    )
    config = {"configurable": {"session": SimpleNamespace(bundle_path=bundle)}}

    result = tools.suggest_form_element_rule.func(
        form_name=_FORM_NAME, page_name="Details", field_name="Kid name",
        intent="show only when 'Child sick' is Yes", config=config,
    )

    assert result["status"] == "suggested", result.get("warnings")


def _comparison_rule(expression: str, setup: str = "") -> RuleResult:
    js = f"""\
"use strict";
({{params, imports}}) => {{
  const encounter = params.entity;
  const validationResults = [];
  {setup}
  if ({expression}) validationResults.push(imports.common.createValidationError("x"));
  return validationResults;
}};
"""
    return RuleResult(rule_kind=RuleKind.VALIDATION, js=js, confidence="high")


def test_concept_name_comparisons_are_checked_against_the_form(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path))
    loop = "encounter.observations.some(obs => obs.concept.name === {name})"

    assert check(_comparison_rule(loop.format(name='"Weight"')), spec)[0] is True
    _, miscased = check(_comparison_rule(loop.format(name='"weight"')), spec)
    _, child = check(_comparison_rule(loop.format(name='"QA15CN PulseReg"')), spec)
    _, unknown = check(_comparison_rule(loop.format(name='"Haemoglobin"')), spec)

    assert miscased == ["off-bundle concept name 'weight' in a comparison; Avni matches names exactly, the bundle has 'Weight'"]
    assert any("'QA15CN PulseReg', a question inside the question group 'QA15CN PulseCheck'" in w for w in child)
    assert unknown == ["off-bundle concept names: ['Haemoglobin'] (compared with a concept name)"]


def test_coded_answers_compared_as_text_are_checked(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path))

    readable = 'encounter.getObservationReadableValue("Child sick") === {answer}'
    assert check(_comparison_rule(readable.format(answer='"Yes"')), spec)[0] is True
    _, wrong_case = check(_comparison_rule(readable.format(answer='"yes"')), spec)
    _, through_variable = check(_comparison_rule('sick === "Maybe"', setup='const sick = encounter.getObservationReadableValue("Child sick");'), spec)
    _, raw_value = check(_comparison_rule('encounter.getObservationValue("Child sick") === "Yes"'), spec)
    grouped_ok, _ = check(_comparison_rule('encounter.getObservationReadableValue("QA15CN PulseReg", "QA15CN PulseCheck") === "No"'), spec)

    assert any("off-bundle answer(s) ['yes'] for concept 'Child sick'" in w and "the bundle has 'Yes'" in w for w in wrong_case)
    assert any("off-bundle answer(s) ['Maybe'] for concept 'Child sick'" in w for w in through_variable)
    assert any("returns the coded answer's UUID" in w for w in raw_value)
    assert grouped_ok is True


def test_value_in_entire_enrolment_with_a_group_name_in_its_encounter_argument_is_rejected(tmp_path: Path) -> None:
    js = _rule('valueInEntireEnrolment("QA15CN Pulse", "QA15CN Vitals").greaterThan(3)').js

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=js, confidence="high"), _group_spec(tmp_path, kind=RuleKind.FORM_ELEMENT))

    assert ok is False
    assert any("the second argument of valueInEntireEnrolment is the current encounter, not a question group" in w for w in warnings)


def test_suggested_grouped_read_puts_the_group_where_the_method_takes_it(tmp_path: Path) -> None:
    _, warnings = check(
        _validation_rule('params.entity.programEnrolment.findObservationInLastEncounter("QA15CN Pulse", encounter)'),
        _group_spec(tmp_path),
    )

    assert any("e.g. getObservationReadableValue('QA15CN Pulse', 'QA15CN Vitals')" in w for w in warnings), warnings


def test_concept_name_at_a_later_argument_is_read_from_its_own_position(tmp_path: Path) -> None:
    spec = _group_spec(tmp_path, kind=RuleKind.FORM_ELEMENT, target_field="Pulse rate")
    reads_top_level = _rule('valueInEncounter("Weight").is.lessThan(7)').js.replace(
        "return statusBuilder.build();",
        'params.entity.programEnrolment.findLatestPreviousEncounterWithValueForConcept(encounter, "Weight", "7");\n  return statusBuilder.build();',
    )
    reads_unknown = reads_top_level.replace('encounter, "Weight", "7"', 'encounter, "Haemoglobin", "7"')

    ok, warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=reads_top_level, confidence="high"), spec)
    unknown_ok, unknown_warnings = check(RuleResult(rule_kind=RuleKind.FORM_ELEMENT, js=reads_unknown, confidence="high"), spec)

    assert ok is True, warnings
    assert unknown_ok is False
    assert "off-bundle concept names: ['Haemoglobin']" in unknown_warnings


def test_grouped_read_with_the_group_in_a_variable_is_not_reported_as_off_bundle(tmp_path: Path) -> None:
    js = _validation_rule('encounter.getObservationReadableValue("QA15CN Pulse", GROUP)').js.replace(
        "const validationResults = [];", 'const validationResults = [];\n  const GROUP = "QA15CN Vitals";',
    )

    ok, warnings = check(RuleResult(rule_kind=RuleKind.VALIDATION, js=js, confidence="high"), _group_spec(tmp_path))

    assert ok is True, warnings


def test_same_variable_name_in_two_functions_is_not_checked_against_the_wrong_question(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path))
    setup = (
        'function a() { const value = encounter.getObservationReadableValue("Weight"); return value === "kg"; }\n'
        '  function b() { const value = encounter.getObservationReadableValue("Child sick"); return value === "Yes"; }'
    )

    ok, warnings = check(_comparison_rule("a() || b()", setup=setup), spec)

    assert ok is True, warnings


def test_empty_text_presence_checks_are_not_answer_comparisons(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path))

    for expression in ('encounter.getObservationReadableValue("Child sick") !== ""', 'encounter.getObservationValue("Child sick") != ""'):
        ok, warnings = check(_comparison_rule(expression), spec)
        assert ok is True, (expression, warnings)


def test_comparing_an_answer_concepts_name_is_not_treated_as_a_question_name(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path))
    setup = 'const yes = params.formElement.concept.getAnswers().find(answer => answer.concept.name === "Yes");'

    ok, warnings = check(_comparison_rule("yes", setup=setup), spec)

    assert ok is True, warnings


def test_individuals_last_encounter_read_ignores_a_group_argument(tmp_path: Path) -> None:
    ok, warnings = check(
        _validation_rule('params.entity.individual.findObservationInLastEncounter("QA15CN Pulse", encounter, "QA15CN Vitals")'),
        _group_spec(tmp_path),
    )

    assert ok is False
    assert any("individual.findObservationInLastEncounter ignores a question group argument" in w for w in warnings)


def test_value_and_date_read_is_compared_through_its_value(tmp_path: Path) -> None:
    spec = _spec_for_bundle(_coded_group_bundle(tmp_path))
    read = 'params.entity.programEnrolment.findObservationValueInEntireEnrolment("Child sick")'

    _, direct = check(_comparison_rule(f'{read} === "Yes"'), spec)
    value_ok, _ = check(_comparison_rule(f'{read}.value === "Yes"'), spec)
    _, wrong_answer = check(_comparison_rule(f'{read}.value === "Maybe"'), spec)

    assert any("returns an object with `value` and `date`" in w for w in direct)
    assert value_ok is True
    assert any("off-bundle answer(s) ['Maybe'] for concept 'Child sick'" in w for w in wrong_answer)


def test_comparing_a_repeatable_group_question_by_concept_name_gives_the_repeatable_reason(tmp_path: Path) -> None:
    bundle = _add_group(
        _write_bundle(tmp_path),
        {**_element("Kids", {"uuid": "c-kids", "name": "QA15CN Kids", "dataType": "QuestionGroup"}),
         "keyValues": [{"key": "repeatable", "value": True}]},
        [{**_element("Kid name", {"uuid": "c-kid", "name": "QA15CN Kid", "dataType": "Text"}), "parentFormElementUuid": "fe-Kids"}],
    )

    _, warnings = check(_comparison_rule('encounter.observations.some(obs => obs.concept.name === "QA15CN Kid")'), _spec_for_bundle(bundle))

    assert any(w.startswith("question group read: 'QA15CN Kids' is a repeatable question group") for w in warnings)

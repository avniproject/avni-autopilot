"""Shared fixtures for offline pipeline tests.

`scripted_form_linking` stands in for the model's form-linking call: each
form sheet is linked to the entity whose modelling-doc row names it exactly
(Registration Form, Enrolment Form, Exit Form, Forms Linked, Cancellation
Form). Field enrichment is left out, so no model or network is used.
"""

from __future__ import annotations

import pytest

import pipeline.nodes as nodes
from domain.form_links import FormLinkResult, build_entity_catalog


class _ScriptedLinkHelper:
    def is_available(self) -> bool:
        return True


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _scripted_classify_forms(forms, entity_spec, sheet_names, _helper):  # noqa: ANN001, ANN202
    catalog = build_entity_catalog(entity_spec)
    claims: dict[str, FormLinkResult] = {}

    def claim(source: str | None, **link: str | None) -> None:
        if source:
            claims.setdefault(_norm(source), FormLinkResult(sheet_name=source, confidence="high", **link))

    for name, hints in catalog.subject_types:
        claim(hints["registration_form_source"], form_type="IndividualProfile", subject_type=name)
    for name, hints in catalog.programs:
        target = hints["target_subject_type"]
        claim(hints["enrolment_form_source"], form_type="ProgramEnrolment",
              subject_type=target, program=name)
        claim(hints["exit_form_source"], form_type="ProgramExit", subject_type=target, program=name)
    program_targets = {name: hints["target_subject_type"] for name, hints in catalog.programs}
    for name, hints in catalog.program_encounters:
        program = hints["program"]
        subject = hints["subject_type"] or program_targets.get(program)
        claim(hints["form_source"], form_type="ProgramEncounter",
              subject_type=subject, program=program, encounter_type=name)
        claim(hints["cancellation_form_source"], form_type="ProgramEncounterCancellation",
              subject_type=subject, program=program, encounter_type=name)
    for name, hints in catalog.encounters:
        claim(hints["form_source"], form_type="Encounter",
              subject_type=hints["subject_type"], encounter_type=name)
        claim(hints["cancellation_form_source"], form_type="IndividualEncounterCancellation",
              subject_type=hints["subject_type"], encounter_type=name)

    results = [
        claims[_norm(sheet)].model_copy(update={"sheet_name": sheet})
        for sheet in sheet_names if _norm(sheet) in claims
    ]
    return results, []


@pytest.fixture
def scripted_form_linking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodes, "LLMHelper", _ScriptedLinkHelper)
    monkeypatch.setattr(nodes, "classify_forms", _scripted_classify_forms)
    monkeypatch.setattr(nodes, "attach_unmatched_rule_intents", lambda _spec, _helper: [])
    monkeypatch.setattr(
        nodes, "enrich_forms",
        lambda forms, _instructions, _helper: (forms, [], [], []),
    )

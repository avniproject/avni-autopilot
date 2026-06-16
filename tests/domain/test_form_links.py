"""Unit tests for `src/domain/form_links.py`.

Covers:
- Catalog assembly from EntitySpec.
- Validator rejection of off-pool references and shape mismatches.
- Junk-sheet classification (form_type=None) → drop.
- Orphan-entity detection.
- Roundtrip on the four named Ekam failure cases via a stub agent.

Usage:
    pytest tests/domain/test_form_links.py
"""

from __future__ import annotations

import pytest

from domain.form_links import (
    EntityCatalog,
    FormLinkResult,
    FormLinkResults,
    apply_form_link_results,
    build_entity_catalog,
    classify_forms,
    orphan_entities,
    validate_result,
)
from models import (
    EncounterTypeSpec,
    EntitySpec,
    FormSpec,
    ProgramSpec,
    SubjectTypeSpec,
)


# ── Catalog assembly ──────────────────────────────────────────────────────────


def _ekam_entity_spec() -> EntitySpec:
    """A trimmed Ekam-style catalog covering the four failure cases."""
    return EntitySpec(
        subject_types=[
            SubjectTypeSpec(name="Individual"),
            SubjectTypeSpec(name="Awareness"),
            SubjectTypeSpec(name="Stakeholder Meeting"),
        ],
        programs=[
            ProgramSpec(name="Pregnancy", target_subject_type="Individual"),
            ProgramSpec(name="Save a child", target_subject_type="Individual"),
            ProgramSpec(name="Referral", target_subject_type="Individual"),
        ],
        encounter_types=[
            EncounterTypeSpec(
                name="Save a Child Followup ",
                program_name="Save a child",
                subject_type="Individual",
                is_program_encounter=True,
            ),
            EncounterTypeSpec(
                name="Referral to Hospital Follow up",
                program_name="Referral",
                subject_type="Individual",
                is_program_encounter=True,
            ),
        ],
    )


def test_catalog_separates_program_and_standalone_encounters() -> None:
    spec = EntitySpec(
        encounter_types=[
            EncounterTypeSpec(name="ANC", program_name="Pregnancy",
                              is_program_encounter=True),
            EncounterTypeSpec(name="General Survey", subject_type="Household",
                              is_program_encounter=False),
        ],
    )
    cat = build_entity_catalog(spec)
    assert [n for n, _ in cat.program_encounters] == ["ANC"]
    assert [n for n, _ in cat.encounters] == ["General Survey"]


def test_catalog_carries_source_columns() -> None:
    spec = EntitySpec(
        subject_types=[
            SubjectTypeSpec(name="Awareness",
                            registration_form_source="Forms EKAM"),
        ],
        programs=[
            ProgramSpec(name="Pregnancy",
                        target_subject_type="Individual",
                        enrolment_form_source="Forms EKAM",
                        exit_form_source=None),
        ],
        encounter_types=[
            EncounterTypeSpec(name="ANC", program_name="Pregnancy",
                              is_program_encounter=True,
                              form_source="Forms EKAM",
                              cancellation_form_source="ANC Cancellation"),
        ],
    )
    cat = build_entity_catalog(spec)
    _, st_hints = cat.subject_types[0]
    assert st_hints["registration_form_source"] == "Forms EKAM"
    _, prog_hints = cat.programs[0]
    assert prog_hints["enrolment_form_source"] == "Forms EKAM"
    _, enc_hints = cat.program_encounters[0]
    assert enc_hints["cancellation_form_source"] == "ANC Cancellation"


# ── Validator ─────────────────────────────────────────────────────────────────


@pytest.fixture
def ekam_catalog() -> EntityCatalog:
    return build_entity_catalog(_ekam_entity_spec())


def test_validator_accepts_well_formed_program_encounter(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="Save a Child Follow Up",
        form_type="ProgramEncounter",
        subject_type="Individual",
        program="Save a child",
        encounter_type="Save a Child Followup ",
        confidence="medium",
        reasoning="trailing-space variant",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert ok, err


def test_validator_rejects_off_pool_subject(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="X",
        form_type="IndividualProfile",
        subject_type="Imaginary Subject",
        confidence="high",
        reasoning="invented",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert not ok
    assert "subject_type" in err
    assert "Imaginary Subject" in err


def test_validator_rejects_off_pool_program(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="X",
        form_type="ProgramEnrolment",
        subject_type="Individual",
        program="Nonexistent",
        confidence="high",
        reasoning="invented",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert not ok
    assert "program" in err


def test_validator_rejects_off_pool_encounter(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="X",
        form_type="ProgramEncounter",
        subject_type="Individual",
        program="Pregnancy",
        encounter_type="Not An Encounter",
        confidence="high",
        reasoning="invented",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert not ok
    assert "encounter_type" in err


def test_validator_rejects_missing_required_program(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="X",
        form_type="ProgramEncounter",
        subject_type="Individual",
        program=None,
        encounter_type="Save a Child Followup ",
        confidence="high",
        reasoning="missing program",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert not ok
    assert "program" in err and "required" in err


def test_validator_rejects_mixed_junk_shape(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="X",
        form_type=None,
        subject_type="Individual",
        confidence="low",
        reasoning="bad shape",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert not ok
    assert "junk" in err


def test_validator_accepts_clean_junk(ekam_catalog) -> None:
    r = FormLinkResult(
        sheet_name="Sheet35",
        form_type=None,
        confidence="high",
        reasoning="excel default",
    )
    ok, err = validate_result(r, ekam_catalog)
    assert ok, err


# ── Application — auto-apply all validated results ────────────────────────────


def test_applies_classification_and_drops_junk(ekam_catalog) -> None:
    forms = [
        FormSpec(name="Save a Child Follow Up", formType="Encounter"),
        FormSpec(name="Sheet35", formType="Encounter"),
    ]
    results = [
        FormLinkResult(
            sheet_name="Save a Child Follow Up",
            form_type="ProgramEncounter",
            subject_type="Individual",
            program="Save a child",
            encounter_type="Save a Child Followup ",
            confidence="high",
            reasoning="exact (trailing space)",
        ),
        FormLinkResult(
            sheet_name="Sheet35", form_type=None,
            confidence="high", reasoning="excel default",
        ),
    ]
    outcome = apply_form_link_results(forms, results, ekam_catalog)
    assert outcome.dropped == ["Sheet35"]
    assert outcome.warnings == []
    assert len(outcome.linked_forms) == 1
    stamped = outcome.linked_forms[0]
    assert stamped.formType == "ProgramEncounter"
    assert stamped.program == "Save a child"
    assert stamped.encounterType == "Save a Child Followup "


def test_medium_confidence_still_applies(ekam_catalog) -> None:
    forms = [FormSpec(name="Awareness", formType="Encounter")]
    results = [
        FormLinkResult(
            sheet_name="Awareness",
            form_type="IndividualProfile",
            subject_type="Awareness",
            confidence="medium",
            reasoning="single match",
        ),
    ]
    outcome = apply_form_link_results(forms, results, ekam_catalog)
    assert outcome.dropped == []
    assert outcome.warnings == []
    assert outcome.linked_forms[0].formType == "IndividualProfile"
    assert outcome.linked_forms[0].subjectType == "Awareness"


def test_unclassified_form_keeps_defaults_with_warning(ekam_catalog) -> None:
    forms = [FormSpec(name="A new sheet", formType="Encounter")]
    outcome = apply_form_link_results(forms, [], ekam_catalog)
    assert outcome.linked_forms[0].formType == "Encounter"
    assert outcome.dropped == []
    assert any("not classified" in w for w in outcome.warnings)


def test_off_pool_result_keeps_defaults_with_warning(ekam_catalog) -> None:
    forms = [FormSpec(name="Awareness", formType="Encounter")]
    results = [
        FormLinkResult(
            sheet_name="Awareness",
            form_type="IndividualProfile",
            subject_type="Imaginary",
            confidence="high",
            reasoning="hallucinated",
        ),
    ]
    outcome = apply_form_link_results(forms, results, ekam_catalog)
    assert outcome.linked_forms[0].formType == "Encounter"
    assert any("rejected" in w for w in outcome.warnings)


# ── Orphan detection ──────────────────────────────────────────────────────────


def test_orphan_subject_when_no_registration_match(ekam_catalog) -> None:
    results = [
        FormLinkResult(
            sheet_name="Individual Registration",
            form_type="IndividualProfile",
            subject_type="Individual",
            confidence="high",
            reasoning="exact",
        ),
    ]
    orphans = orphan_entities(ekam_catalog, results)
    assert any("'Awareness'" in line and "registration" in line for line in orphans)
    assert any("'Stakeholder Meeting'" in line for line in orphans)


# ── End-to-end roundtrip via a stub agent ─────────────────────────────────────


class _StubHelper:
    """Stand-in for `LLMHelper` that returns canned classifications."""

    def __init__(self, response: FormLinkResults) -> None:
        self._response = response

    def is_available(self) -> bool:
        return True

    def classify(self, system: str, user: str, response_model):  # noqa: D401
        return self._response


def test_classify_forms_roundtrip_named_ekam_failures(ekam_catalog) -> None:
    """The four named failure cases all resolve through the stub agent."""
    spec = _ekam_entity_spec()
    forms = [
        FormSpec(name="Awareness", formType="Encounter"),
        FormSpec(name="Stakeholders Meeting", formType="Encounter"),
        FormSpec(name="Save a Child Follow Up", formType="Encounter"),
        FormSpec(name="Refferal Enrolment Hospital for", formType="Encounter"),
        FormSpec(name="Sheet35", formType="Encounter"),
    ]
    spec.forms = list(forms)

    canned = FormLinkResults(results=[
        FormLinkResult(
            sheet_name="Awareness", form_type="IndividualProfile",
            subject_type="Awareness", confidence="high",
            reasoning="exact match in Subject Types tab",
        ),
        FormLinkResult(
            sheet_name="Stakeholders Meeting",
            form_type="IndividualProfile",
            subject_type="Stakeholder Meeting",
            confidence="high",
            reasoning="plural/singular variant",
        ),
        FormLinkResult(
            sheet_name="Save a Child Follow Up",
            form_type="ProgramEncounter",
            subject_type="Individual",
            program="Save a child",
            encounter_type="Save a Child Followup ",
            confidence="high",
            reasoning="trailing-space + spacing variant",
        ),
        FormLinkResult(
            sheet_name="Refferal Enrolment Hospital for",
            form_type="ProgramEncounter",
            subject_type="Individual",
            program="Referral",
            encounter_type="Referral to Hospital Follow up",
            confidence="medium",
            reasoning="typo + Excel 31-char truncation",
        ),
        FormLinkResult(
            sheet_name="Sheet35", form_type=None,
            confidence="high", reasoning="excel default",
        ),
    ])

    helper = _StubHelper(canned)
    results, warnings = classify_forms(
        forms, spec, [f.name for f in forms], helper,
    )
    assert warnings == []
    assert len(results) == 5

    outcome = apply_form_link_results(forms, results, ekam_catalog)
    assert outcome.warnings == []
    assert outcome.dropped == ["Sheet35"]

    by_name = {f.name: f for f in outcome.linked_forms if f.formType != "Encounter"}
    assert by_name["Awareness"].subjectType == "Awareness"
    assert by_name["Stakeholders Meeting"].subjectType == "Stakeholder Meeting"
    sacfu = by_name["Save a Child Follow Up"]
    assert sacfu.formType == "ProgramEncounter"
    assert sacfu.program == "Save a child"
    assert sacfu.encounterType == "Save a Child Followup "
    referral = by_name["Refferal Enrolment Hospital for"]
    assert referral.formType == "ProgramEncounter"
    assert referral.program == "Referral"
    assert referral.encounterType == "Referral to Hospital Follow up"

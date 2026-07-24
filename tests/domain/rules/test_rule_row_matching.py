"""Unit tests for Rules-tab row → form matching (exact pass + LLM pass).

Usage:
    pytest tests/domain/rules/test_rule_row_matching.py
"""

from __future__ import annotations

from domain.rules.parser import (
    RuleRowMatch,
    RuleRowMatches,
    apply_intents_to_forms,
    attach_unmatched_rule_intents,
    match_rule_rows_to_forms,
)
from models import FormSpec


# ── Helpers ───────────────────────────────────────────────────────────────────


class _StubHelper:
    """LLMHelper stand-in returning a canned RuleRowMatches response."""

    def __init__(self, matches: list[RuleRowMatch] | None = None, available: bool = True):
        self._matches = matches or []
        self._available = available
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return self._available

    def classify(self, system_prompt: str, user_prompt: str, response_model: type):
        self.calls.append((system_prompt, user_prompt))
        return RuleRowMatches(matches=self._matches)


def _form(name: str) -> FormSpec:
    return FormSpec(name=name, formType="Encounter")


# ── apply_intents_to_forms ────────────────────────────────────────────────────


def test_exact_rows_attach_and_variants_are_returned() -> None:
    forms = [_form("Pregnancy"), _form("ANC")]
    intents = {
        "ANC": {"visitScheduleRule": "monthly"},
        "Pregnancy Enrolment": {"visitScheduleRule": "schedule ANC"},
    }
    unmatched = apply_intents_to_forms(forms, intents)
    assert forms[1].rule_intents == {"visitScheduleRule": "monthly"}
    assert forms[0].rule_intents == {}
    assert unmatched == {"Pregnancy Enrolment": {"visitScheduleRule": "schedule ANC"}}


def test_all_exact_matches_return_empty_unmatched() -> None:
    forms = [_form("ANC")]
    unmatched = apply_intents_to_forms(forms, {"ANC": {"validationRule": "v"}})
    assert unmatched == {}


# ── match_rule_rows_to_forms ──────────────────────────────────────────────────


def test_resolves_variant_row_to_listed_form() -> None:
    helper = _StubHelper([RuleRowMatch(row="Pregnancy Enrolment", form="Pregnancy")])
    resolved, warnings = match_rule_rows_to_forms(
        ["Pregnancy Enrolment"], ["Pregnancy", "ANC"], helper,
    )
    assert resolved == {"Pregnancy Enrolment": "Pregnancy"}
    assert warnings == []


def test_hallucinated_form_name_is_rejected_with_warning() -> None:
    helper = _StubHelper([RuleRowMatch(row="Pregnancy Enrolment", form="Enrolment Form")])
    resolved, warnings = match_rule_rows_to_forms(
        ["Pregnancy Enrolment"], ["Pregnancy"], helper,
    )
    assert resolved == {}
    assert any("unknown form" in w for w in warnings)
    assert any("matched no form" in w for w in warnings)


def test_null_match_reports_unresolved_row() -> None:
    helper = _StubHelper([RuleRowMatch(row="Sheet35", form=None)])
    resolved, warnings = match_rule_rows_to_forms(["Sheet35"], ["Pregnancy"], helper)
    assert resolved == {}
    assert any("Sheet35" in w for w in warnings)


def test_unknown_row_names_in_response_are_ignored() -> None:
    helper = _StubHelper([RuleRowMatch(row="Not A Row", form="Pregnancy")])
    resolved, warnings = match_rule_rows_to_forms(["Real Row"], ["Pregnancy"], helper)
    assert resolved == {}
    assert any("Real Row" in w for w in warnings)


def test_unavailable_helper_drops_rows_with_warning() -> None:
    helper = _StubHelper(available=False)
    resolved, warnings = match_rule_rows_to_forms(["Row"], ["Pregnancy"], helper)
    assert resolved == {}
    assert any("no LLM" in w for w in warnings)
    assert helper.calls == []


def test_empty_inputs_are_noop() -> None:
    helper = _StubHelper()
    assert match_rule_rows_to_forms([], ["F"], helper) == ({}, [])
    assert match_rule_rows_to_forms(["R"], [], helper) == ({}, [])
    assert helper.calls == []


# ── attach_unmatched_rule_intents ─────────────────────────────────────────────


def test_attach_resolves_pending_intents_and_clears_them() -> None:
    from models import EntitySpec

    spec = EntitySpec(forms=[_form("Pregnancy"), _form("ANC")])
    spec.unmatched_rule_intents = {
        "Pregnancy Enrolment": {"visitScheduleRule": "schedule ANC"},
    }
    helper = _StubHelper([RuleRowMatch(row="Pregnancy Enrolment", form="Pregnancy")])

    warnings = attach_unmatched_rule_intents(spec, helper)

    assert warnings == []
    assert spec.forms[0].rule_intents == {"visitScheduleRule": "schedule ANC"}
    assert spec.unmatched_rule_intents == {}

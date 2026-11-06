"""Guards the AND/OR evaluation-order guidance given to the rule generator.

rules-config evaluates a RuleCondition chain from the right (each `.and` /
`.or` joins everything before it with the rest of the chain), so the
generator must be told not to mix them in one chain.

Usage:
    uv run --extra dev pytest tests/domain/rules/test_rule_condition_grouping.py
"""

from __future__ import annotations

import pytest

from domain.rules.knowledge_base import KnowledgeBase
from domain.rules.prompts import build_field_batch_system_prompt, build_system_prompt
from domain.rules.rule_spec import RuleKind


def test_catalog_has_or_and_grouping_entries() -> None:
    by_name = {h.name: h for h in KnowledgeBase().helpers}
    assert "RuleCondition.or.when" in by_name
    grouping = by_name["RuleCondition.and_or_grouping"]
    assert "A && (B || (C && D))" in grouping.signature
    assert "and_or_grouping" in by_name["RuleCondition.and.when"].use_when


@pytest.mark.parametrize("rule_kind", list(RuleKind))
def test_system_prompt_forbids_mixed_and_or_chain(rule_kind: RuleKind) -> None:
    prompt = build_system_prompt(rule_kind, "programEncounter")
    assert "A && (B || (C && D))" in prompt
    assert "Never mix `.and` and" in prompt


def test_catalog_has_not_entry_for_hide_when_answer() -> None:
    by_name = {h.name: h for h in KnowledgeBase().helpers}
    negation = by_name["RuleCondition.not"]
    assert ".not.containsAnswerConceptName('No')" in negation.example_snippet
    assert "unanswered" in negation.use_when


def test_grouping_example_nests_instead_of_distributing() -> None:
    grouping = {h.name: h for h in KnowledgeBase().helpers}["RuleCondition.and_or_grouping"]
    assert "dangerSigns && (highSystolic || albuminAndSugar)" in grouping.example_snippet


@pytest.mark.parametrize("rule_kind", list(RuleKind))
def test_system_prompt_keeps_hide_when_answer_negated(rule_kind: RuleKind) -> None:
    prompt = build_system_prompt(rule_kind, "programEncounter")
    assert ".not.containsAnswerConceptName('No')" in prompt


def test_field_batch_prompt_keeps_hide_when_answer_negated() -> None:
    assert ".not.containsAnswerConceptName('No')" in build_field_batch_system_prompt("programEncounter")

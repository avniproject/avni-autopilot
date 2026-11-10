"""Rule warnings start with a plain label naming the rule, not an internal key.

Usage:
    uv run --extra dev pytest tests/domain/rules/test_rule_warning_labels.py
"""

from __future__ import annotations

from types import SimpleNamespace

from domain.rules.orchestrator import PendingFieldRule, PendingFormRule, collect_pending_rules
from domain.rules.rule_spec import RuleKind

_FORM = SimpleNamespace(name="Woman Registration", rule_intents={}, sections=[])


def test_form_rule_label_names_the_rule_and_form() -> None:
    labels = {
        kind: PendingFormRule(form_spec=_FORM, kind_value=kind.value, rule_spec=None).label
        for kind in (RuleKind.VISIT_SCHEDULE, RuleKind.VALIDATION, RuleKind.EDIT_FORM, RuleKind.DECISION)
    }
    assert labels[RuleKind.VALIDATION] == "Validation rule for form 'Woman Registration'"
    assert labels[RuleKind.VISIT_SCHEDULE] == "Visit schedule rule for form 'Woman Registration'"
    assert all("rules." not in label and "Rule for" not in label for label in labels.values())


def test_field_rule_label_names_the_field_page_and_form() -> None:
    pending = PendingFieldRule(
        form_spec=_FORM, section_name="Basic details", field_name="Age", rule_spec=None,
    )
    assert pending.label == "Field rule for 'Age' (page 'Basic details') in form 'Woman Registration'"


def test_an_unknown_rule_column_is_named_in_plain_words() -> None:
    spec = SimpleNamespace(forms=[SimpleNamespace(
        name="ANC", rule_intents={"approvalRule": "approve when complete"}, sections=[],
    )])
    _form, _field, warnings = collect_pending_rules(spec, [], [])
    assert warnings == ["Form 'ANC': 'approvalRule' is not a rule the builder writes, so it was skipped."]

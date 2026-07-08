"""Node-level tests for concurrent rule generation.

Covers FORM_RULE_BATCHING_SDD.md §9 Phase 1:

- Generation calls across forms (form-level singles and field batches)
  overlap in time under the thread pool.
- `forms_json` mutations and `parse_warnings` are identical to a
  ``AVNI_RULES_MAX_CONCURRENCY=1`` run.
- One worker failing yields a namespaced warning for that rule only;
  every other rule is generated and written normally.
- Per-generation logs carry ``path=single`` / ``path=field_batch``.

Usage:
    PYTHONPATH=src pytest tests/pipeline/test_generate_rules_concurrency.py
"""

from __future__ import annotations

import logging
import threading
import time

from domain.rules import orchestrator
from domain.rules.rule_spec import RuleKind, RuleResult
from models import EntitySpec, FieldSpec, FormSpec, SectionSpec
from pipeline import nodes

_DELAY_SECONDS = 0.15


class _StubKB:
    def retrieve_batch(self, specs):
        return [None] * len(specs)


class _StubGenerator:
    """Records call spans; sleeps to make overlap measurable."""

    def __init__(self, fail_kind_values: frozenset[str] = frozenset()):
        self.fail_kind_values = fail_kind_values
        self.spans: list[tuple[float, float]] = []
        self._lock = threading.Lock()
        self.kb = _StubKB()

    def is_available(self) -> bool:
        return True

    def _record_span(self, start: float) -> None:
        with self._lock:
            self.spans.append((start, time.monotonic()))

    def generate(self, spec, context=None) -> RuleResult:
        start = time.monotonic()
        time.sleep(_DELAY_SECONDS)
        self._record_span(start)
        if spec.rule_kind.value in self.fail_kind_values:
            raise RuntimeError("stubbed worker failure")
        return RuleResult(
            rule_kind=spec.rule_kind,
            js=f"// {spec.form_name}:{spec.rule_kind.value}",
            confidence="high",
        )

    def generate_field_batch(self, entries) -> list[RuleResult]:
        start = time.monotonic()
        time.sleep(_DELAY_SECONDS)
        self._record_span(start)
        return [
            RuleResult(
                rule_kind=RuleKind.FORM_ELEMENT,
                js=f"// field {field_name}",
                confidence="high",
            )
            for field_name, _, _, _ in entries
        ]


def _make_state() -> dict:
    forms = [
        FormSpec(
            name="Registration",
            formType="IndividualProfile",
            rule_intents={
                RuleKind.VISIT_SCHEDULE.value: "schedule a follow-up",
                RuleKind.VALIDATION.value: "age must be over 18",
            },
            sections=[SectionSpec(name="Basics", fields=[
                FieldSpec(name="Consent", rule_intent="show when eligible"),
            ])],
        ),
        FormSpec(
            name="Exit Survey",
            formType="IndividualProfile",
            rule_intents={RuleKind.DECISION.value: "flag high risk"},
            sections=[SectionSpec(name="Outcome", fields=[
                FieldSpec(name="Reason", rule_intent="hide when consented"),
            ])],
        ),
    ]
    forms_json = [
        {"content": {"name": "Registration", "formElementGroups": [
            {"name": "Basics", "formElements": [{"name": "Consent", "rule": ""}]},
        ]}},
        {"content": {"name": "Exit Survey", "formElementGroups": [
            {"name": "Outcome", "formElements": [{"name": "Reason", "rule": ""}]},
        ]}},
    ]
    return {
        "entity_spec": EntitySpec(forms=forms),
        "parse_warnings": [],
        "forms_json": forms_json,
        "encounter_types_json": [{"name": "ANC"}],
        "programs_json": [{"name": "Pregnancy"}],
        "org_name": "test-org",
    }


def _patch(monkeypatch, stub: _StubGenerator, max_concurrency: str) -> None:
    monkeypatch.setattr(nodes, "RuleGenerator", lambda: stub)
    monkeypatch.setattr(
        orchestrator, "validate_and_decide",
        lambda result, spec: (bool(result.js), list(result.warnings)),
    )
    monkeypatch.setenv("AVNI_RULES_MAX_CONCURRENCY", max_concurrency)


def _spans_overlap(spans: list[tuple[float, float]]) -> bool:
    return any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(spans)
        for b_start, b_end in spans[i + 1:]
    )


def test_calls_overlap_and_all_rules_written(monkeypatch, caplog):
    stub = _StubGenerator()
    _patch(monkeypatch, stub, "8")
    state = _make_state()

    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger=orchestrator.log.name):
        result = nodes.generate_rules(state)
    elapsed = time.monotonic() - started

    # 5 calls (3 form-level + 2 field batches) of 0.15s each: sequential
    # would take >= 0.75s.
    assert elapsed < 4 * _DELAY_SECONDS
    assert len(stub.spans) == 5
    assert _spans_overlap(stub.spans)

    forms = {f["content"]["name"]: f["content"] for f in result["forms_json"]}
    assert forms["Registration"]["visitScheduleRule"].startswith("//")
    assert forms["Registration"]["validationRule"].startswith("//")
    assert forms["Exit Survey"]["decisionRule"].startswith("//")
    assert forms["Registration"]["formElementGroups"][0]["formElements"][0][
        "rule"] == "// field Consent"
    assert forms["Exit Survey"]["formElementGroups"][0]["formElements"][0][
        "rule"] == "// field Reason"
    assert result["parse_warnings"] == []

    assert any("path=single" in r.message for r in caplog.records)
    assert any("path=field_batch" in r.message for r in caplog.records)


def test_concurrent_run_matches_sequential_run(monkeypatch):
    _patch(monkeypatch, _StubGenerator(), "8")
    concurrent_result = nodes.generate_rules(_make_state())

    _patch(monkeypatch, _StubGenerator(), "1")
    sequential_result = nodes.generate_rules(_make_state())

    assert concurrent_result["forms_json"] == sequential_result["forms_json"]
    assert (concurrent_result["parse_warnings"]
            == sequential_result["parse_warnings"])


def test_worker_failure_isolated_to_one_rule(monkeypatch):
    stub = _StubGenerator(
        fail_kind_values=frozenset({RuleKind.VALIDATION.value}),
    )
    _patch(monkeypatch, stub, "8")

    result = nodes.generate_rules(_make_state())

    warnings = result["parse_warnings"]
    assert any(
        w.startswith("rules.validationRule.Registration:")
        and "generation worker failed" in w
        for w in warnings
    )
    assert len(warnings) == 1

    forms = {f["content"]["name"]: f["content"] for f in result["forms_json"]}
    assert "validationRule" not in forms["Registration"]
    assert forms["Registration"]["visitScheduleRule"].startswith("//")
    assert forms["Exit Survey"]["decisionRule"].startswith("//")
    assert forms["Registration"]["formElementGroups"][0]["formElements"][0][
        "rule"] == "// field Consent"

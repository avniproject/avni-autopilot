"""Rule generation via Claude.

`RuleGenerator(kb).generate(spec)` retrieves helpers + examples from the
knowledge base, prompts Claude with a structured-output schema, and returns a
`RuleResult`. The caller (pipeline node or chat tool) runs `validator.check`
afterwards and decides whether to write the JS into the form bundle.

Construct one `RuleGenerator` per pipeline run (or hold a module-level
instance for long-lived chat tools) — the system prompt is cached across
`generate()` calls so subsequent forms hit the Anthropic prompt cache. See
specs/VISIT_SCHEDULE_RULE_SDD.md §7.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from domain.rules.knowledge_base import KnowledgeBase, RetrievedContext
from domain.rules.prompts import (
    build_field_batch_system_prompt,
    build_field_batch_user_prompt,
    build_system_prompt,
    build_user_prompt,
    entity_param_for_form_type,
)
from domain.rules.rule_spec import RuleKind, RuleResult, RuleSpec

log = logging.getLogger(__name__)


MODEL = "claude-sonnet-4-6"
_FIELD_BATCH_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096


class _LLMRuleOutput(BaseModel):
    """The schema Claude returns via `with_structured_output`.

    Internal only — the public API hands back `RuleResult`, which adds the
    `rule_kind` from the input spec and folds notes into `warnings`.
    """

    js: str = ""
    confidence: Literal["high", "medium", "low"] = "low"
    used_helpers: list[str] = Field(default_factory=list)
    referenced_concepts: list[str] = Field(default_factory=list)
    referenced_encounter_types: list[str] = Field(default_factory=list)
    notes: str = ""


class _LLMFieldRuleItem(BaseModel):
    """One field's rule output within a batch response."""

    field_name: str
    js: str = ""
    confidence: Literal["high", "medium", "low"] = "low"
    used_helpers: list[str] = Field(default_factory=list)
    referenced_concepts: list[str] = Field(default_factory=list)
    referenced_encounter_types: list[str] = Field(default_factory=list)
    notes: str = ""


class _LLMFieldBatchOutput(BaseModel):
    """The schema Haiku returns for a per-form field batch call."""

    rules: list[_LLMFieldRuleItem] = Field(default_factory=list)


class RuleGenerator:
    """Stateful generator: wraps `ChatAnthropic.with_structured_output(_LLMRuleOutput)`.

    Construct once per run; call `generate(spec)` per form. The system prompt
    is sent with `cache_control={"type":"ephemeral"}` so Anthropic's prompt
    cache hits on every call after the first within the cache TTL.
    """

    def __init__(
        self,
        kb: KnowledgeBase | None = None,
        model: str = MODEL,
        field_batch_model: str = _FIELD_BATCH_MODEL,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._kb = kb or KnowledgeBase()
        self._has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._model = None
        self._field_batch_model = None
        if self._has_key:
            chat = ChatAnthropic(model=model, max_tokens=max_tokens)
            self._model = chat.with_structured_output(_LLMRuleOutput)
            field_chat = ChatAnthropic(model=field_batch_model, max_tokens=max_tokens)
            self._field_batch_model = field_chat.with_structured_output(_LLMFieldBatchOutput)

    def is_available(self) -> bool:
        """True when ANTHROPIC_API_KEY is set and the structured model is bound."""
        return self._has_key and self._model is not None

    @property
    def kb(self) -> KnowledgeBase:
        """Read-only handle to the underlying KnowledgeBase.

        Exposed so the pipeline can call `kb.retrieve_batch(specs)` for all
        rules in a bundle in one Voyage request — see `pipeline.nodes.generate_rules`.
        """
        return self._kb

    def generate(
        self, spec: RuleSpec, context: RetrievedContext | None = None,
    ) -> RuleResult:
        """Produce a `RuleResult` for one rule intent.

        If `context` is provided, the retrieve step is skipped — used by the
        pipeline's batch path (`generate_rules` calls `kb.retrieve_batch`
        once for all forms, then passes each context here). Single-rule
        callers (the chat `set_form_rule` tool) leave `context=None` and
        retrieve runs inline as before.

        Failures (missing API key, KB retrieval error, Anthropic call error)
        return a `RuleResult` with empty `js` and a `warnings` entry — never
        raise. The caller can treat any empty-`js` result uniformly.
        """
        if not self.is_available():
            return _empty(spec, "ANTHROPIC_API_KEY not set; rule generation skipped")

        if context is None:
            try:
                ctx = self._kb.retrieve(spec)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"KB retrieve failed for form {spec.form_name!r}: {exc}")
                return _empty(spec, f"KB retrieval failed: {exc}")
        else:
            ctx = context

        helpers_text = self._kb.render_helpers(ctx.helpers)
        examples_text = self._kb.render_examples(ctx.examples)

        entity_param = entity_param_for_form_type(spec.form_type)
        system_text = build_system_prompt(spec.rule_kind, entity_param)
        user_text = build_user_prompt(spec, helpers_text, examples_text)

        system_msg = SystemMessage(content=[{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }])
        human_msg = HumanMessage(content=user_text)

        try:
            output: _LLMRuleOutput = self._model.invoke(  # type: ignore[assignment]
                [system_msg, human_msg]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Claude rule generation failed for form {spec.form_name!r}: {exc}")
            return _empty(spec, f"Claude call failed: {exc}")

        warnings: list[str] = []
        if output.notes:
            warnings.append(f"model note: {output.notes}")

        return RuleResult(
            rule_kind=spec.rule_kind,
            js=output.js or "",
            confidence=output.confidence,
            used_helpers=list(output.used_helpers),
            referenced_concepts=list(output.referenced_concepts),
            referenced_encounter_types=list(output.referenced_encounter_types),
            warnings=warnings,
        )


    def generate_field_batch(
        self,
        entries: list[tuple[str, str, RuleSpec, RetrievedContext | None]],
    ) -> list[RuleResult]:
        """Generate field-level rules for all fields in one form in a single Haiku call.

        entries: list of (field_name, section_name, rule_spec, context).
        Returns one RuleResult per entry, in the same order.
        """
        if not entries:
            return []
        if not self.is_available():
            return [
                _empty(spec, "ANTHROPIC_API_KEY not set; rule generation skipped")
                for _, _, spec, _ in entries
            ]

        # Merge per-field KB contexts into one combined set (union by key).
        seen_helper_keys: set[str] = set()
        seen_example_keys: set[str] = set()
        merged_helpers = []
        merged_examples = []
        for _, _, _, ctx in entries:
            if ctx is None:
                continue
            for h in ctx.helpers:
                if h.key not in seen_helper_keys:
                    seen_helper_keys.add(h.key)
                    merged_helpers.append(h)
            for ex in ctx.examples:
                if ex.key not in seen_example_keys:
                    seen_example_keys.add(ex.key)
                    merged_examples.append(ex)

        helpers_text = self._kb.render_helpers(merged_helpers)
        examples_text = self._kb.render_examples(merged_examples)

        shared_spec = entries[0][2]
        entity_param = entity_param_for_form_type(shared_spec.form_type)
        field_entries = [(name, section, spec) for name, section, spec, _ in entries]

        system_text = build_field_batch_system_prompt(entity_param)
        user_text = build_field_batch_user_prompt(field_entries, helpers_text, examples_text)

        system_msg = SystemMessage(content=[{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }])
        human_msg = HumanMessage(content=user_text)

        try:
            output: _LLMFieldBatchOutput = self._field_batch_model.invoke(  # type: ignore[assignment]
                [system_msg, human_msg]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"Haiku field batch failed for form {shared_spec.form_name!r}: {exc}"
            )
            return [
                _empty(spec, f"field batch call failed: {exc}")
                for _, _, spec, _ in entries
            ]

        result_by_name = {item.field_name: item for item in output.rules}
        results: list[RuleResult] = []
        for field_name, _, rule_spec, _ in entries:
            item = result_by_name.get(field_name)
            if item is None:
                results.append(_empty(rule_spec, "field not returned by model"))
                continue
            warnings: list[str] = []
            if item.notes:
                warnings.append(f"model note: {item.notes}")
            results.append(RuleResult(
                rule_kind=RuleKind.FORM_ELEMENT,
                js=item.js or "",
                confidence=item.confidence,
                used_helpers=list(item.used_helpers),
                referenced_concepts=list(item.referenced_concepts),
                referenced_encounter_types=list(item.referenced_encounter_types),
                warnings=warnings,
            ))
        return results


def _empty(spec: RuleSpec, reason: str) -> RuleResult:
    """Build the canonical empty result that callers treat as 'rule unwritten'."""
    return RuleResult(rule_kind=spec.rule_kind, js="", confidence="low", warnings=[reason])

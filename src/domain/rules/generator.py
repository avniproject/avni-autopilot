"""Rule generation via Claude.

`RuleGenerator(kb).generate(spec)` retrieves helpers + examples from the
knowledge base, prompts Claude with a structured-output schema, and returns a
`RuleResult`. The caller (pipeline node or chat tool) runs `validator.check`
afterwards and decides whether to write the JS into the form bundle.

Construct one `RuleGenerator` per pipeline run — the system prompt is cached
across `generate()` calls so subsequent forms hit the Anthropic prompt cache.
For one-shot use, the module-level `generate_rule(spec)` builds a fresh
instance. See specs/VISIT_SCHEDULE_RULE_SDD.md §7.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from domain.rules.knowledge_base import KnowledgeBase
from domain.rules.prompts import (
    build_system_prompt,
    build_user_prompt,
    entity_param_for_form_type,
)
from domain.rules.rule_spec import RuleResult, RuleSpec

log = logging.getLogger(__name__)


# Sonnet 4.6 is the default — capable enough for short JS generation against a
# bounded helper surface, materially cheaper than Opus for the per-form
# generation cadence (~20 forms per bundle build).
MODEL = "claude-sonnet-4-6"
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
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self._kb = kb or KnowledgeBase()
        self._has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._model = None
        if self._has_key:
            chat = ChatAnthropic(model=model, max_tokens=max_tokens)
            self._model = chat.with_structured_output(_LLMRuleOutput)

    def is_available(self) -> bool:
        """True when ANTHROPIC_API_KEY is set and the structured model is bound."""
        return self._has_key and self._model is not None

    def generate(self, spec: RuleSpec) -> RuleResult:
        """Produce a `RuleResult` for one rule intent.

        Failures (missing API key, KB retrieval error, Anthropic call error)
        return a `RuleResult` with empty `js` and a `warnings` entry — never
        raise. The caller can treat any empty-`js` result uniformly.
        """
        if not self.is_available():
            return _empty(spec, "ANTHROPIC_API_KEY not set; rule generation skipped")

        try:
            ctx = self._kb.retrieve(spec)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"KB retrieve failed for form {spec.form_name!r}: {exc}")
            return _empty(spec, f"KB retrieval failed: {exc}")

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


def generate_rule(spec: RuleSpec, kb: KnowledgeBase | None = None) -> RuleResult:
    """One-shot convenience: build a generator, generate once, discard.

    Pipeline code should construct `RuleGenerator` once and reuse it across
    forms to keep the prompt cache warm. This helper is for chat tools and
    ad-hoc callers that only generate a single rule per call.
    """
    return RuleGenerator(kb=kb).generate(spec)


def _empty(spec: RuleSpec, reason: str) -> RuleResult:
    """Build the canonical empty result that callers treat as 'rule unwritten'."""
    return RuleResult(rule_kind=spec.rule_kind, js="", confidence="low", warnings=[reason])

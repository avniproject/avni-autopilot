"""LLMHelper — wraps ChatAnthropic (Haiku) for form enrichment and classification.

Public surface:
    helper = LLMHelper()
    output = helper.suggest_names(problems)   # one call across all forms
    result = helper.classify(system, user, ResponseModel)

`suggest_names` accepts a list of _FieldProblem instances (from enricher.py) and
returns a _SuggestionsOutput with one _NameSuggestion per problem, matched back
by `problem_index` — the 1-based position of the problem in the prompt. Field
names can exceed 255 characters and span lines, so echoing them back for
matching is unreliable; a small integer round-trips exactly.

`classify` is a generic single-call helper for any structured-output task that
needs its own Pydantic response model (form-link classifier, entity resolvers, …).

If ANTHROPIC_API_KEY is unset, `LLMHelper.is_available()` returns False and
callers should skip the LLM step.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from domain.enricher import _FieldProblem

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 16384


# ── Suggestion schema ─────────────────────────────────────────────────────────


class _NameSuggestion(BaseModel):
    problem_index: int = Field(description="The [N] number of the problem being answered")
    suggested_name: str


class _SuggestionsOutput(BaseModel):
    suggestions: list[_NameSuggestion] = Field(default_factory=list)


# ── System prompt ─────────────────────────────────────────────────────────────

_SUGGEST_SYSTEM_PROMPT = """\
You are an Avni form-modelling assistant. You receive a list of field naming
problems detected in one or more forms. Your only job is to suggest a clear,
concise replacement name for each problem.

Three kinds of problems:

1. long_name — the field name exceeds 255 characters. Suggest a shorter name
   that preserves the field's intent in under 255 characters.

2. duplicate_field — two or more fields in the same form share the same name.
   Suggest a disambiguated name for this occurrence using the section context
   provided.

3. conflicting_concept — the same field name appears in multiple forms with
   different answer lists. Avni requires globally unique concept names, so
   each form's version must be renamed to make it distinct. Suggest a name
   that makes clear which program or context this field belongs to, using the
   form name or program as a qualifier (e.g. "Exit Reason (Pregnancy)" rather
   than "Exit Reason 1").

For each problem return:
  problem_index  — the number shown in the problem's [N] marker
  suggested_name — your proposed replacement

Rules:
  1. suggested_name MUST be ≤ 255 characters.
  2. Within a form, no two suggested_names may be identical.
  3. Use section names or neighbouring field names as qualifiers when
     disambiguating duplicates (e.g. "Remarks (Distribution)" rather than
     "Remarks 1").
  4. For conflicting_concept, all occurrences must end up with unique names.
     One occurrence may keep the original name if that is still unambiguous;
     the rest must be renamed to make them distinct.
  5. Return one entry per problem. Do not skip any.
"""


# ── Prompt builder ────────────────────────────────────────────────────────────


def _build_problem_prompt(problems: list[_FieldProblem]) -> str:
    dup_totals: dict[tuple[str, str], int] = {}
    for p in problems:
        if p.kind == "duplicate_field":
            key = (p.form_name, p.field_name.strip().lower())
            dup_totals[key] = dup_totals.get(key, 0) + 1

    lines: list[str] = []
    current_form: str | None = None
    for i, p in enumerate(problems, 1):
        if p.form_name != current_form:
            lines.append(f"\nFORM: {p.form_name}")
            current_form = p.form_name
        if p.kind == "long_name":
            lines.append(f'[{i}] long_name — "{p.field_name}"')
        elif p.kind == "conflicting_concept":
            lines.append(f'[{i}] conflicting_concept — "{p.field_name}"')
            lines.append(f'    This form\'s answers: {", ".join(p.own_options)}')
            for other_form, other_opts in p.conflicting_forms:
                lines.append(f'    "{other_form}" answers: {", ".join(other_opts)}')
        else:
            total = dup_totals.get((p.form_name, p.field_name.strip().lower()), 1)
            lines.append(
                f'[{i}] duplicate_field ({p.occurrence} of {total}) — "{p.field_name}"'
            )
        for sec_name, field_names in p.context_sections.items():
            lines.append(f'    Section "{sec_name}" fields: {", ".join(field_names)}')

    return "\n".join(lines).strip()


# ── LLMHelper ─────────────────────────────────────────────────────────────────


class LLMHelper:
    """Wraps ChatAnthropic (Haiku) for enrichment name suggestions and classification."""

    def __init__(self, model: str = MODEL, max_tokens: int = MAX_TOKENS) -> None:
        self._has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self._model = None
        if self._has_key:
            chat = ChatAnthropic(model=model, max_tokens=max_tokens)
            self._model = chat.with_structured_output(_SuggestionsOutput)

    def is_available(self) -> bool:
        return self._has_key and self._model is not None

    def suggest_names(self, problems: list[_FieldProblem]) -> _SuggestionsOutput:
        """Send all detected field problems to Claude in one call.

        Returns a _SuggestionsOutput whose suggestions are matched back to
        problems by 1-based `problem_index` in enricher.py.
        """
        if not self.is_available():
            raise RuntimeError(
                "LLMHelper called without ANTHROPIC_API_KEY; check is_available() first"
            )
        system = SystemMessage(content=[{
            "type": "text",
            "text": _SUGGEST_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }])
        result = self._model.invoke(
            [system, HumanMessage(content=_build_problem_prompt(problems))]
        )
        return result  # type: ignore[return-value]

    def classify(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type,
        *,
        retries: int = 2,
    ) -> Any:
        """Single Haiku call returning a parsed `response_model` instance.

        Generic enough that any caller (form-link classifier, future entity
        resolvers, …) can pass its own Pydantic container. Retries up to
        `retries` times on parse / API errors before letting the last
        exception propagate.
        """
        if not self.is_available():
            raise RuntimeError(
                "LLMHelper called without ANTHROPIC_API_KEY; check is_available() first"
            )
        chat = ChatAnthropic(model=MODEL, max_tokens=MAX_TOKENS)
        model = chat.with_structured_output(response_model)
        system = SystemMessage(content=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }])
        human = HumanMessage(content=user_prompt)
        last_exc: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                return model.invoke([system, human])
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        assert last_exc is not None
        raise last_exc


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)

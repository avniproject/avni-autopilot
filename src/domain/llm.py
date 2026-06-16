"""
LLMHelper — wraps ChatAnthropic (Haiku) with structured-output enrichment.

Public surface:
    helper = LLMHelper()                                      # builds the model + prompt once
    enriched: EnrichedFormSpec = helper.enrich(form_spec, df, user_instructions)

The helper is invoked once per form by `enricher.py`. It builds a prompt with:
  - the parser's `FormSpec` (current best-effort parse)
  - the raw form sheet rendered as a Markdown table
  - optional `user_instructions` (free-text)

…and asks Claude to return an `EnrichedFormSpec` (the parser's `FormSpec`
unchanged + a list of `Change` records). The form on the response is ignored
by `enricher.py`; only the Change records flow downstream, after a
deterministic justification gate filters them.

Caching: the system prompt is constant across forms, so we mark the last block
with `cache_control={"type": "ephemeral"}` for prompt-cache hits on every call
after the first.

If `ANTHROPIC_API_KEY` is unset, `LLMHelper.is_available()` returns False and
the enricher should skip enrichment.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from models import EnrichedFormSpec, FormSpec

MODEL = "claude-haiku-4-5"
# Refined FormSpecs for large forms (100+ fields with options) can be sizeable.
# Haiku 4.5 supports up to 64K output; 16K leaves comfortable headroom.
MAX_TOKENS = 16384


# ── System prompt (stable; cached) ────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an Avni form-modelling assistant. Given a parser's best-effort
FormSpec for a single form plus the raw form sheet as a Markdown table, you
identify TWO specific issues and emit Change records for them. You do NOT
modify the FormSpec itself — return it exactly as the parser produced it.

The TWO kinds of issues you handle (and nothing else):

1. long_name — A field name longer than 255 characters. Avni's database
   column is varchar(255), so any name exceeding that MUST be shortened.
   Propose a shorter name that preserves the original intent.

2. duplicate_field — Two or more fields share the same (case-insensitive)
   name within this form. Field names must be unique per form. Propose a
   disambiguated name for each duplicate occurrence based on the context.

Change record shape:
  change_id   — stable, unique within this form (e.g. "field-7/long_name")
  form        — form name
  field       — the current field name
  kind        — EXACTLY "long_name" or "duplicate_field". Any other value
                will be rejected.
  before      — long_name:       {"name": "<current long name>"}
                duplicate_field: {"name": "<dup name>", "section": "<source sections or single section>"}
                                 (emit ONE change per occurrence; the section
                                  hint lets the applier find the right one)
  after       — long_name:       {"name": "<proposed shorter name>"}
                duplicate_field: {"name": "<disambiguated name>"}
  reason      — one short sentence explaining why

Hard rules:
  1. Return the parser's FormSpec UNCHANGED. Do NOT alter any field's name,
     dataType, options, or any other attribute in the FormSpec you return.
     The applier performs renames from your Change records after the user
     has confirmed each one.
  2. NEVER emit a Change with a `kind` other than "long_name" or
     "duplicate_field". Out-of-scope refinements (data type, options,
     selection type, min/max, skip logic, adding/removing fields, etc.) are
     not handled by this system right now — silently omit them.
  3. If the form has no long names AND no duplicates, return the FormSpec
     unchanged with `changes: []`.
"""


def _form_spec_to_compact_dict(form: FormSpec) -> dict[str, Any]:
    """Strip empty/default fields so the prompt isn't padded with nulls."""
    out: dict[str, Any] = {
        "name": form.name,
        "formType": form.formType,
    }
    if form.subjectType:
        out["subjectType"] = form.subjectType
    if form.program:
        out["program"] = form.program
    if form.encounterType:
        out["encounterType"] = form.encounterType
    out["sections"] = [
        {"name": s.name, "fields": [_field_compact(f) for f in s.fields]}
        for s in (form.sections or [])
    ]
    return out


def _field_compact(f: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"name": f.name, "dataType": f.dataType}
    if f.group:
        d["group"] = f.group  # section hint matters for duplicate disambiguation
    return d


def _df_to_markdown(df: pd.DataFrame, max_rows: int = 200) -> str:
    """Render the form sheet as a Markdown table, truncating very long sheets."""
    df_clean = df.copy().fillna("")
    if len(df_clean) > max_rows:
        df_clean = df_clean.head(max_rows)
        return df_clean.to_markdown(index=False) + f"\n\n_(truncated to first {max_rows} rows)_"
    return df_clean.to_markdown(index=False)


# ── LLMHelper ─────────────────────────────────────────────────────────────────


class LLMHelper:
    """Wraps a ChatAnthropic model bound to the EnrichedFormSpec output schema."""

    def __init__(self, model: str = MODEL, max_tokens: int = MAX_TOKENS) -> None:
        self._has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not self._has_key:
            self._model = None
            return
        chat = ChatAnthropic(model=model, max_tokens=max_tokens)
        # Bind the structured-output schema once; reuse on every call.
        self._model = chat.with_structured_output(EnrichedFormSpec)

    def is_available(self) -> bool:
        return self._has_key and self._model is not None

    def enrich(
        self,
        form: FormSpec,
        df: pd.DataFrame,
        user_instructions: str | None = None,
    ) -> EnrichedFormSpec:
        """Send one form to Claude and get back a refined FormSpec + changes."""
        if not self.is_available():
            raise RuntimeError(
                "LLMHelper called without ANTHROPIC_API_KEY; check is_available() first"
            )

        user_blocks = [
            f"## Form: {form.name}\n",
            "### Parser's current FormSpec\n```json\n",
            _json_dumps(_form_spec_to_compact_dict(form)),
            "\n```\n",
            "### Source sheet (Markdown table)\n",
            _df_to_markdown(df),
            "\n",
        ]
        if user_instructions:
            user_blocks.append(
                f"\n### User instructions\n{user_instructions}\n"
            )
        user_blocks.append(
            "\nReturn an EnrichedFormSpec with the refined FormSpec and a "
            "Change list describing every refinement. If nothing needs to "
            "change, return the FormSpec unchanged with changes=[]."
        )
        user_text = "".join(user_blocks)

        # The system prompt is identical across calls, so cache it.
        system = SystemMessage(
            content=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }]
        )
        result = self._model.invoke([system, HumanMessage(content=user_text)])
        # `with_structured_output` returns the parsed Pydantic model directly.
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
        system = SystemMessage(
            content=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        )
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

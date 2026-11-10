"""Tests for how the chat answers questions about Avni from the docs.

The docs tool never invites an answer from the model's own knowledge: no
key, a failed search and no match all tell the model to say the docs do
not cover it. No Voyage or Anthropic call is made.

Usage:
    uv run --extra dev pytest tests/test_chat_docs_answers.py
"""

from __future__ import annotations

import pytest

import chat.tools as tools
from chat.prompts import SYSTEM_PROMPT
from domain.docs.knowledge_base import DocEntry


@pytest.fixture
def voyage_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key-not-used")


def _ask(question: str = "Is there a GPS field type?") -> dict:
    return tools.answer_avni_question.invoke({"question": question})


def _assert_no_answer_from_memory(result: dict) -> None:
    assert result["excerpts"] == []
    assert "do not answer" in result["note"]
    assert "own knowledge" not in result["note"]


def test_without_a_voyage_key_the_chat_is_told_not_to_answer_from_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    result = _ask()
    _assert_no_answer_from_memory(result)
    assert result["note"] == tools._DOCS_UNAVAILABLE_NOTE


def test_a_failed_search_is_reported_without_its_error_text(
    monkeypatch: pytest.MonkeyPatch, voyage_key: None,
) -> None:
    def failing_retrieve(_query: str, **_kwargs: object) -> list[DocEntry]:
        raise RuntimeError("voyage 429: free-tier rate limit for key sk-123")

    monkeypatch.setattr(tools._docs_kb, "retrieve", failing_retrieve)
    result = _ask()
    _assert_no_answer_from_memory(result)
    assert "429" not in result["note"] and "sk-123" not in result["note"]


def test_no_matching_entry_tells_the_chat_the_docs_do_not_cover_it(
    monkeypatch: pytest.MonkeyPatch, voyage_key: None,
) -> None:
    monkeypatch.setattr(tools._docs_kb, "retrieve", lambda _query, **_kwargs: [])  # noqa: ARG005
    result = _ask()
    _assert_no_answer_from_memory(result)
    assert "do not cover it" in result["note"]


def test_excerpts_carry_their_entry_titles_and_an_answer_only_from_these_note(
    monkeypatch: pytest.MonkeyPatch, voyage_key: None,
) -> None:
    entry = DocEntry(
        key="form-element-types-and-configuration",
        title="Form Element Types and Configuration",
        body="Location fields capture an address level.",
    )
    monkeypatch.setattr(tools._docs_kb, "retrieve", lambda _query, **_kwargs: [entry])  # noqa: ARG005
    result = _ask()
    assert result["excerpts"] == [{"title": entry.title, "content": entry.body}]
    assert "only with what these excerpts say" in result["note"]
    assert "title" in result["note"]
    assert "do not cover it" in result["note"]


def test_the_prompt_never_invites_answers_from_own_knowledge() -> None:
    assert "own knowledge" not in SYSTEM_PROMPT
    assert "Answering questions about Avni:" in SYSTEM_PROMPT
    assert "do not cover it" in SYSTEM_PROMPT
    assert "Name the docs entry" in SYSTEM_PROMPT

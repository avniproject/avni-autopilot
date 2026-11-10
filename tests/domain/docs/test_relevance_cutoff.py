"""Docs search leaves out entries below the relevance cut-off, and the
calibration picks that cut-off from labelled questions.

A fake embedder stands in for Voyage, so no API is called.

Usage:
    uv run --extra dev pytest tests/domain/docs/test_relevance_cutoff.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import chat.tools as tools
import domain.docs.knowledge_base as kb_mod
from config import Settings
from domain.docs import kb_cli
from domain.docs.knowledge_base import DocsKnowledgeBase
from domain.docs.relevance_calibration import load_questions, suggest_min_relevance
from domain.rules.knowledge_base import Embedder

_ENTRIES = {
    "gps": ("GPS capture", "Location is captured with each form."),
    "users": ("User management", "Create users for field workers."),
    "rules": ("Rules", "Rules are written in JavaScript."),
}
# Unit vectors per entry; the query vector is chosen per test.
_VECTORS = {
    "gps": np.array([1.0, 0.0, 0.0], dtype=np.float32),
    "users": np.array([0.0, 1.0, 0.0], dtype=np.float32),
    "rules": np.array([0.0, 0.0, 1.0], dtype=np.float32),
}


class _QueryEmbedder(Embedder):
    """Embeds each known query to a fixed vector."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls = 0

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        assert input_type == "query"
        self.calls += 1
        return [self.vectors[t] for t in texts]


@pytest.fixture
def kb(tmp_path: Path) -> DocsKnowledgeBase:
    entries_dir = tmp_path / "entries"
    entries_dir.mkdir()
    for key, (title, body) in _ENTRIES.items():
        (entries_dir / f"{key}.json").write_text(json.dumps({"key": key, "title": title, "body": body}))
    knowledge_base = DocsKnowledgeBase(root=tmp_path)
    knowledge_base._catalog_vectors = dict(_VECTORS)
    return knowledge_base


def _use_embedder(monkeypatch: pytest.MonkeyPatch, vectors: dict[str, list[float]]) -> _QueryEmbedder:
    embedder = _QueryEmbedder(vectors)
    monkeypatch.setattr(kb_mod, "_default_embedder", lambda: embedder)
    return embedder


def test_entries_below_the_cut_off_are_left_out(kb: DocsKnowledgeBase, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_embedder(monkeypatch, {"gps?": [0.9, 0.3, 0.0]})
    assert [e.key for e in kb.retrieve("gps?", top_k=3)] == ["gps", "users", "rules"]
    assert [e.key for e in kb.retrieve("gps?", top_k=3, min_relevance=0.5)] == ["gps"]
    assert kb.retrieve("gps?", top_k=3, min_relevance=0.99) == []


def test_search_scores_every_query_with_one_embedding_call(
    kb: DocsKnowledgeBase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = _use_embedder(monkeypatch, {"a": [1.0, 0.0, 0.0], "b": [0.0, 0.0, 1.0]})
    ranked = kb.search(["a", "b"], top_k=1)
    assert embedder.calls == 1
    assert [(round(s, 3), e.key) for s, e in ranked[0]] == [(1.0, "gps")]
    assert [e.key for _s, e in ranked[1]] == ["rules"]


def test_the_chat_tool_applies_the_configured_cut_off(
    kb: DocsKnowledgeBase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key-not-used")
    _use_embedder(monkeypatch, {"Is there a payroll module?": [0.3, 0.3, 0.3]})
    monkeypatch.setattr(tools, "_docs_kb", kb)
    ask = {"question": "Is there a payroll module?"}

    monkeypatch.setattr(tools, "settings", Settings(docs_min_relevance=None))
    assert len(tools.answer_avni_question.invoke(ask)["excerpts"]) == 3

    monkeypatch.setattr(tools, "settings", Settings(docs_min_relevance=0.7))
    result = tools.answer_avni_question.invoke(ask)
    assert result == {"excerpts": [], "note": tools._DOCS_NO_MATCH_NOTE}


def test_the_cut_off_separates_covered_from_uncovered_scores() -> None:
    outcome = suggest_min_relevance([0.62, 0.55, 0.71], [0.31, 0.40, 0.47])
    assert 0.47 < outcome.min_relevance <= 0.55
    assert (outcome.covered_kept, outcome.uncovered_rejected) == (3, 3)


def test_overlapping_scores_favour_keeping_covered_questions() -> None:
    outcome = suggest_min_relevance([0.50, 0.60], [0.55, 0.30])
    assert outcome.covered_kept + outcome.uncovered_rejected == 3
    assert outcome.min_relevance < 0.50


def test_calibration_needs_both_kinds_of_question() -> None:
    with pytest.raises(ValueError):
        suggest_min_relevance([0.5], [])


def test_the_shipped_question_set_names_real_entries() -> None:
    entries = DocsKnowledgeBase().entries
    questions = load_questions(kb_cli._CALIBRATION_QUESTIONS, {e.key for e in entries})
    covered = [q for q in questions if q.expected_key]
    assert len(covered) >= 25
    assert len(questions) - len(covered) >= 15


def test_unknown_entry_keys_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"question": "x", "expected_key": "no-such-entry"}]))
    with pytest.raises(ValueError, match="no-such-entry"):
        load_questions(path, {"gps"})


def test_calibrate_prints_the_suggested_cut_off(
    kb: DocsKnowledgeBase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps([
        {"question": "where am I", "expected_key": "gps"},
        {"question": "add a user", "expected_key": "users"},
        {"question": "bake bread", "expected_key": None},
    ]))
    _use_embedder(monkeypatch, {
        "where am I": [0.9, 0.1, 0.0], "add a user": [0.1, 0.8, 0.0], "bake bread": [0.4, 0.4, 0.4],
    })
    kb_cli._calibrate(kb, questions)
    out = capsys.readouterr().out
    assert "Suggested DOCS_MIN_RELEVANCE=" in out
    assert "keeps 2/2 covered, rejects 1/1 uncovered" in out


def test_calibrate_names_labelled_entries_without_an_embedding(
    kb: DocsKnowledgeBase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A new entry added before `rebuild` has no vector; calibrate says so instead of crashing."""
    del kb._catalog_vectors["users"]
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps([
        {"question": "add a user", "expected_key": "users"},
        {"question": "bake bread", "expected_key": None},
    ]))
    embedder = _use_embedder(monkeypatch, {"add a user": [0.1, 0.8, 0.0], "bake bread": [0.4, 0.4, 0.4]})
    assert kb_cli._calibrate(kb, questions) == 1
    out = capsys.readouterr().out
    assert "users" in out and "avni-docs-kb rebuild" in out
    assert "Suggested" not in out
    assert embedder.calls == 0, "questions were embedded before the refusal"


def test_negative_scores_never_give_a_cut_off_outside_zero_to_one() -> None:
    outcome = suggest_min_relevance([-0.02, 0.10], [-0.08])
    assert outcome.min_relevance is None or 0.0 <= outcome.min_relevance <= 1.0
    separable = suggest_min_relevance([0.10, 0.20], [-0.30, -0.10])
    assert separable.min_relevance == 0.0
    assert (separable.covered_kept, separable.uncovered_rejected) == (2, 2)


def test_every_suggestion_is_a_value_the_config_accepts() -> None:
    import random

    rng = random.Random(18)
    for _ in range(500):
        covered = [rng.uniform(-1, 1) for _ in range(rng.randint(1, 6))]
        uncovered = [rng.uniform(-1, 1) for _ in range(rng.randint(1, 6))]
        cut = suggest_min_relevance(covered, uncovered).min_relevance
        Settings(docs_min_relevance=cut)


def test_no_cut_off_is_suggested_when_none_helps(
    kb: DocsKnowledgeBase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    assert suggest_min_relevance([0.30], [0.60]).min_relevance is None
    questions = tmp_path / "q.json"
    questions.write_text(json.dumps([
        {"question": "where am I", "expected_key": "gps"},
        {"question": "bake bread", "expected_key": None},
    ]))
    _use_embedder(monkeypatch, {"where am I": [0.3, 0.9, 0.0], "bake bread": [1.0, 0.0, 0.0]})
    assert kb_cli._calibrate(kb, questions) == 0
    out = capsys.readouterr().out
    assert "Leave DOCS_MIN_RELEVANCE unset" in out
    assert "Suggested" not in out


@pytest.mark.parametrize("value", [1.5, -0.1, float("nan")])
def test_a_cut_off_outside_zero_to_one_is_refused(value: float) -> None:
    with pytest.raises(ValueError, match="DOCS_MIN_RELEVANCE must be between 0 and 1"):
        Settings(docs_min_relevance=value)


@pytest.mark.parametrize("value", [None, 0.0, 0.42, 1.0])
def test_a_cut_off_from_zero_to_one_is_accepted(value: float | None) -> None:
    assert Settings(docs_min_relevance=value).docs_min_relevance == value


@pytest.mark.parametrize(("raw", "message"), [
    ("abc", "DOCS_MIN_RELEVANCE must be a number, got 'abc'"),
    ("1.5", "DOCS_MIN_RELEVANCE must be between 0 and 1, got 1.5"),
])
def test_the_service_will_not_start_with_a_bad_cut_off(raw: str, message: str) -> None:
    import os
    import subprocess
    import sys

    src = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "DOCS_MIN_RELEVANCE": raw, "PYTHONPATH": str(src),
           "ANTHROPIC_API_KEY": "", "VOYAGE_API_KEY": ""}
    run = subprocess.run([sys.executable, "-c", "import config"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert run.returncode != 0
    assert message in run.stderr


def test_a_blank_cut_off_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from config import _env_float

    monkeypatch.setenv("DOCS_MIN_RELEVANCE", "  ")
    assert _env_float("DOCS_MIN_RELEVANCE", None) is None

"""Pick the docs-search relevance cut-off from labelled questions.

A labelled question either names the docs entry that answers it
(`expected_key`) or is one the docs do not cover (`expected_key: null`).
The cut-off is the cosine score that keeps the most covered questions'
answering entries while rejecting the most uncovered questions' best
matches. Ties go to the lower cut-off: a covered question answered with
"the docs do not cover it" is the costlier mistake for a user who could
have been helped, and the excerpt note still tells the model to check
what it is given.

See `avni-docs-kb calibrate`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class CalibrationQuestion(BaseModel):
    question: str
    expected_key: str | None = None


class CalibrationOutcome(BaseModel):
    # None when no cut-off rejects an uncovered question without losing more
    # covered ones: leave DOCS_MIN_RELEVANCE unset.
    min_relevance: float | None
    covered_kept: int
    covered_total: int
    uncovered_rejected: int
    uncovered_total: int


def load_questions(path: Path, entry_keys: set[str]) -> list[CalibrationQuestion]:
    """Read the question set; every `expected_key` must name an existing entry."""
    questions = [CalibrationQuestion(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
    unknown = sorted({q.expected_key for q in questions if q.expected_key} - entry_keys)
    if unknown:
        raise ValueError(f"Calibration questions name unknown docs entries: {unknown}")
    return questions


def suggest_min_relevance(
    covered_scores: list[float], uncovered_scores: list[float],
) -> CalibrationOutcome:
    """Cut-off maximising covered scores kept (>=) plus uncovered scores rejected (<).

    Candidates are the midpoints between neighbouring distinct scores, kept
    within 0–1 (the range DOCS_MIN_RELEVANCE accepts). When no candidate
    beats keeping every entry, the outcome has no cut-off.
    """
    if not covered_scores or not uncovered_scores:
        raise ValueError("Both covered and uncovered questions are needed to calibrate")
    scores = sorted(set(covered_scores) | set(uncovered_scores))
    candidates = {
        min(1.0, max(0.0, (a + b) / 2)) for a, b in zip(scores, scores[1:])
    }

    def outcome(cut: float | None) -> CalibrationOutcome:
        return CalibrationOutcome(
            min_relevance=cut,
            covered_kept=sum(cut is None or s >= cut for s in covered_scores),
            covered_total=len(covered_scores),
            uncovered_rejected=sum(cut is not None and s < cut for s in uncovered_scores),
            uncovered_total=len(uncovered_scores),
        )

    def correct(o: CalibrationOutcome) -> int:
        return o.covered_kept + o.uncovered_rejected

    no_cut_off = outcome(None)
    best = max(
        (outcome(cut) for cut in sorted(candidates)),
        key=lambda o: (correct(o), -o.min_relevance),
        default=no_cut_off,
    )
    return best if correct(best) > correct(no_cut_off) else no_cut_off

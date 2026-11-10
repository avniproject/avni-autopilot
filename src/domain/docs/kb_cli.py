"""CLI for maintaining the documentation knowledge base.

Commands:

  rebuild   Load resources/docs/entries/*.json, re-embed any entries whose
            content has changed, and write resources/docs/.embeddings.json.
            Run this after editing any entry JSON file.

  calibrate Embed the labelled questions in
            resources/docs/relevance_calibration.json (one Voyage call for
            all of them), score them against the entries and print the
            DOCS_MIN_RELEVANCE cut-off that best separates questions the docs
            cover from questions they do not. Re-run after changing entries.

Invoke via the project script (`avni-docs-kb rebuild`) or directly
(`python -m domain.docs.kb_cli rebuild`). See specs/AVNI_DOCS_KB_SDD.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import config  # noqa: F401 — imported for the .env-loading side effect
from domain.docs.knowledge_base import DocsKnowledgeBase
from domain.docs.relevance_calibration import load_questions, suggest_min_relevance

log = logging.getLogger("docs.kb")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS_ROOT = _REPO_ROOT / "resources" / "docs"
_CALIBRATION_QUESTIONS = _DOCS_ROOT / "relevance_calibration.json"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="avni-docs-kb", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rebuild = sub.add_parser("rebuild", help="Re-embed changed entries and write the cache.")
    p_rebuild.add_argument("--docs-root", type=Path, default=_DOCS_ROOT,
                           help="Override resources/docs/ root.")

    p_calibrate = sub.add_parser("calibrate", help="Suggest the DOCS_MIN_RELEVANCE cut-off.")
    p_calibrate.add_argument("--docs-root", type=Path, default=_DOCS_ROOT,
                             help="Override resources/docs/ root.")
    p_calibrate.add_argument("--questions", type=Path, default=_CALIBRATION_QUESTIONS,
                             help="Labelled question set (JSON list).")

    args = parser.parse_args(argv)

    if args.cmd == "rebuild":
        kb = DocsKnowledgeBase(root=args.docs_root)
        kb.rebuild()
    elif args.cmd == "calibrate":
        return _calibrate(DocsKnowledgeBase(root=args.docs_root), args.questions)

    return 0


def _calibrate(kb: DocsKnowledgeBase, questions_path: Path) -> int:
    """Print each question's score and the suggested cut-off; 1 when it cannot."""
    questions = load_questions(questions_path, {e.key for e in kb.entries})
    unscored = sorted({q.expected_key for q in questions if q.expected_key} - kb.embedded_keys())
    if unscored:
        print(
            "These labelled entries have no embedding, so they cannot be scored: "
            + ", ".join(unscored)
            + ". Run `avni-docs-kb rebuild`, then calibrate again."
        )
        return 1
    ranked = kb.search([q.question for q in questions], top_k=len(kb.entries))

    covered: list[float] = []
    uncovered: list[float] = []
    for question, results in zip(questions, ranked):
        best_score, best_entry = results[0]
        if question.expected_key is None:
            uncovered.append(best_score)
            print(f"uncovered  top={best_score:.4f} ({best_entry.key})  {question.question}")
            continue
        rank, score = next(
            (i, s) for i, (s, e) in enumerate(results, 1) if e.key == question.expected_key
        )
        covered.append(score)
        print(f"covered    expected={score:.4f} rank={rank} top={best_score:.4f}  {question.question}")

    outcome = suggest_min_relevance(covered, uncovered)
    if outcome.min_relevance is None:
        print(
            "\nNo cut-off helps: none rejects an uncovered question without losing "
            "more covered ones. Leave DOCS_MIN_RELEVANCE unset."
        )
        return 0
    print(
        f"\nSuggested DOCS_MIN_RELEVANCE={outcome.min_relevance:.4f}: keeps "
        f"{outcome.covered_kept}/{outcome.covered_total} covered, rejects "
        f"{outcome.uncovered_rejected}/{outcome.uncovered_total} uncovered."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

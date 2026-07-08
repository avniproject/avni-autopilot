"""Concept-answer allowlist assembly for rule grounding.

Both rule-generation entry paths (the pipeline's
`orchestrator._collect_concept_answers` over `FormSpec`s and the chat
path's `bundle_editor._collect_concept_answers_for` over bundle JSON)
traverse their own form representation into per-form answer maps, then
combine them here so the combining behaviour has exactly one home.
"""

from __future__ import annotations


def merge_answer_scopes(
    scopes: list[dict[str, list[str]]],
) -> dict[str, list[str]]:
    """Combine per-form coded-answer maps into one grounding allowlist.

    Every in-scope form's answers are included — a rule can reference a
    concept on its registration/enrolment sibling (e.g. an encounter rule
    checking the enrolment's risk level), so sibling answers must be
    groundable, never just the target form's.

    Same concept name on multiple forms with different answer lists is
    rare (parsing resolves it via a user-confirmed rename) and is not
    handled specially: lists are never unioned, and the first list
    encountered wins.
    """
    merged: dict[str, list[str]] = {}
    for scope in scopes:
        for name, options in scope.items():
            merged.setdefault(name, list(options))
    return merged

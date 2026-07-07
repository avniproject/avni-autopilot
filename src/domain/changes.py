"""
Apply user-confirmed `Change` objects (from the LLM enrichment pass) onto
FormSpec lists in place. Pure domain logic — no LangGraph imports.

Called from the `apply_user_decisions` node after the user has resolved
each pending change via `interrupt()`.

The enricher (domain/enricher.py) produces `Change`s; this module consumes
them.
"""

from __future__ import annotations

import logging

from models import Change

log = logging.getLogger(__name__)


def apply_resolutions(
    forms: list,
    pending: list[Change],
    resolutions: dict[str, str],
) -> tuple[list[Change], list[str]]:
    """Apply user-confirmed Changes to the FormSpec list.

    Each resolution is one of:
        "yes"               — apply Claude's `after` payload as-is
        "no"                — skip this change
        "edit:<value>"      — apply with the user's value substituted into `after`
                              (format depends on kind; see _parse_edit)

    Mutates the FormSpec list in place. Returns (applied_changes, warnings).
    """
    applied: list[Change] = []
    warnings: list[str] = []
    by_id = {c.change_id: c for c in pending}
    forms_by_name = {f.name: f for f in forms}

    for change_id, decision in resolutions.items():
        change = by_id.get(change_id)
        if change is None:
            warnings.append(f"Resolution for unknown change_id '{change_id}'")
            continue
        if decision == "no" or decision is False:
            continue
        target_form = forms_by_name.get(change.form)
        if target_form is None:
            warnings.append(f"Change {change_id} targets unknown form '{change.form}'")
            continue

        # Resolve `after` — either Claude's proposal or the user's edit override.
        after = dict(change.after or {})
        if isinstance(decision, str) and decision.startswith("edit:"):
            after = _parse_edit(decision[len("edit:"):], change.kind, after, warnings, change_id)

        try:
            ok = _apply_one(target_form, change, after, warnings)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Apply {change_id} ({change.kind}) raised: {exc}")
            continue
        if ok:
            applied.append(change.model_copy(update={"after": after}))
            log.info("[apply] %s renamed to %r", change_id, after.get("name"))
    return applied, warnings


def _all_fields_in(form) -> list:
    """Yield every FieldSpec in a form, walking sections in order."""
    out: list = []
    for section in form.sections or []:
        out.extend(section.fields)
    return out


def _find_field(form, name: str, section: str | None = None):
    """Locate a FieldSpec by name; if `section` is given, prefer that section's copy.

    Used by duplicate_field to disambiguate two same-named occurrences.
    """
    name_l = name.strip().lower()
    if section:
        section_l = section.strip().lower()
        for sec in form.sections or []:
            if sec.name.strip().lower() == section_l:
                for f in sec.fields:
                    if f.name.strip().lower() == name_l:
                        return f
        # Section hint didn't match; fall through to any match.
    for f in _all_fields_in(form):
        if f.name.strip().lower() == name_l:
            return f
    return None


def _apply_one(form, change: Change, after: dict, warnings: list[str]) -> bool:
    """Mutate the form per `change.kind`. Returns True if mutation happened.

    Only `long_name` and `duplicate_field` are currently handled — those are
    the only kinds the LLM is allowed to emit (see `domain.llm._SYSTEM_PROMPT`
    and the `ChangeKind` Literal in `models.py`).
    """
    kind = change.kind
    field_name = change.field
    new_name = after.get("name")
    if not new_name:
        return False

    if kind == "long_name":
        f = _find_field(form, field_name)
        if f is None:
            warnings.append(
                f"long_name: field '{field_name[:80]}…' not found in '{form.name}'"
            )
            return False
        f.name = new_name
        return True

    if kind in ("duplicate_field", "conflicting_concept"):
        # `before.section` disambiguates which field instance to rename.
        before = change.before or {}
        section = before.get("section") if isinstance(before, dict) else None
        f = _find_field(form, field_name, section=section)
        if f is None:
            warnings.append(
                f"{kind}: '{field_name}' not found in section '{section}' of '{form.name}'"
            )
            return False
        f.name = new_name
        return True

    warnings.append(f"Unknown change kind '{kind}' for {change.change_id}")
    return False


def _parse_edit(raw: str, kind: str, fallback: dict, warnings: list[str], change_id: str) -> dict:
    """Translate a user 'edit:<new name>' string into an `after` dict.

    Both supported kinds (long_name, duplicate_field) take the raw value as
    the new field name.
    """
    out = dict(fallback)
    if kind in ("long_name", "duplicate_field", "conflicting_concept"):
        out["name"] = raw.strip()
    else:
        warnings.append(f"edit not supported for kind={kind} on {change_id}; using LLM proposal")
    return out

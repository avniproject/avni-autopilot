"""Rules / Form Rules tab → `FormSpec.rule_intents`, end to end.

The tab is detected by `_classify_sheet` when a sheet has a form-name column
(`Form name`, `Form`, or `Name`) and at least one rule-column alias. Each row
corresponds to one form; cancellation and exit forms get their own rows so
they can carry intents distinct from the parent encounter type's row.

A blank cell means "no rule". Intents are extracted as `{form_name:
{rule_field: intent}}` dicts where `rule_field` is the Avni form-JSON
top-level field name (e.g. `visitScheduleRule`) — not the `RuleKind` enum.
This lets the parser read rule columns the generator hasn't been wired to
handle yet; those keys ride along on `FormSpec.rule_intents` until the
generator catches up.

Attachment runs in two passes. `apply_intents_to_forms` places rows whose
name matches a form exactly (deterministic, at parse time) and returns the
leftovers — row names are typed by hand and routinely diverge from sheet
names (`Pregnancy Enrolment` for a sheet named `Pregnancy`, typos,
truncation). `attach_unmatched_rule_intents` then resolves those against
the real form list with one Haiku call in the form-link pipeline node,
rejecting any name outside the allowlist.

See SDD §9.1.
"""

from __future__ import annotations

import json
import logging

import pandas as pd
from pydantic import BaseModel

log = logging.getLogger(__name__)


# Maps each Avni rule-field name to the column-header aliases the parser will
# match (case-insensitive). New aliases for an existing field can be added
# without touching any other module.
_COLUMN_ALIASES_BY_FIELD: dict[str, tuple[str, ...]] = {
    "visitScheduleRule": (
        "visit schedule rule",
        "scheduling rule",
        "vs rule",
    ),
    "validationRule": (
        "validation rule",
        "form validation rule",
    ),
    "encounterEligibilityCheckRule": (
        "encounter eligibility rule",
        "encounter eligibility check rule",
        "eligibility rule",
    ),
    "enrolmentEligibilityCheckRule": (
        "enrolment eligibility rule",
        "enrolment eligibility check rule",
    ),
    "subjectSummaryRule": (
        "subject summary rule",
    ),
    "enrolmentSummaryRule": (
        "enrolment summary rule",
    ),
    "decisionRule": (
        "decision rule",
    ),
    "editFormRule": (
        "edit form rule",
        "edit form",
    ),
}

# Headers (case-insensitive) that identify the name column on a "Rules" /
# "Form Rules" tab — the column whose cell value names the form a row's
# intents target. Tried in order.
_FORM_NAME_COLUMN_ALIASES: tuple[str, ...] = (
    "form name",
    "form",
    "name",
)


def detect_rule_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map each rule field to the matching column in the DataFrame.

    Returns `{rule_field: actual_column_name}` for every rule field whose
    alias appears in the DataFrame's headers. Empty dict when none match.

    Used both by `extract_rule_intents` (to know which columns to read) and
    by the sheet classifier in `domain.parser` (to recognise a Rules tab).
    """
    if df.empty or df.shape[1] < 2:
        return {}
    cols_by_lower = {str(col).strip().lower(): col for col in df.columns}
    matches: dict[str, str] = {}
    for field, aliases in _COLUMN_ALIASES_BY_FIELD.items():
        for alias in aliases:
            if alias in cols_by_lower:
                matches[field] = cols_by_lower[alias]
                break
    return matches


def find_form_name_column(df: pd.DataFrame) -> str | None:
    """Return the column header used as the form-name key on a Rules tab."""
    if df.empty or df.shape[1] < 1:
        return None
    cols_by_lower = {str(col).strip().lower(): col for col in df.columns}
    for alias in _FORM_NAME_COLUMN_ALIASES:
        if alias in cols_by_lower:
            return cols_by_lower[alias]
    return None


def extract_rule_intents(
    df: pd.DataFrame,
    name_column: str,
) -> dict[str, dict[str, str]]:
    """Return `{form_name: {rule_field: intent}}` for every populated row.

    Args:
        df: A Rules / Form Rules tab DataFrame.
        name_column: The column whose value identifies each row's form —
            typically 'Form name', 'Form', or 'Name' (resolved by
            `find_form_name_column`).

    Returns:
        Dict keyed by the trimmed form name. Rows whose name cell is blank, or
        whose rule columns are all blank, are not included. Rule keys are raw
        Avni field names (e.g. `visitScheduleRule`).
    """
    if df.empty or name_column not in df.columns:
        return {}

    column_for_field = detect_rule_columns(df)
    if not column_for_field:
        return {}

    out: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        raw_name = row.get(name_column)
        if raw_name is None or pd.isna(raw_name):
            continue
        name = str(raw_name).strip()
        if not name or name.startswith("---"):
            continue

        intents: dict[str, str] = {}
        for field, col in column_for_field.items():
            value = row.get(col)
            if value is None or pd.isna(value):
                continue
            text = str(value).strip()
            if text:
                intents[field] = text

        if intents:
            out[name] = intents
    return out


def apply_intents_to_forms(
    forms,
    intents_by_form_name: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Mutate `FormSpec.rule_intents` in place to attach harvested intents.

    Matches each form to its row in the Rules tab by exact form name. Forms
    without a matching row are left untouched.

    Returns the rows whose name matched no form — the Rules tab routinely
    names forms with variants the sheets don't use (`Pregnancy Enrolment`
    for a sheet named `Pregnancy`, typos, truncation). Those rows are
    resolved later by `attach_unmatched_rule_intents` (LLM pass).
    """
    matched_rows: set[str] = set()
    for form in forms:
        match = intents_by_form_name.get(form.name)
        if match:
            form.rule_intents.update(match)
            matched_rows.add(form.name)
    return {
        row: intents
        for row, intents in intents_by_form_name.items()
        if row not in matched_rows
    }


# ── LLM matching for rows the exact pass could not place ─────────────────────

_MATCH_SYSTEM_PROMPT = """\
You match rows from a spreadsheet's form-rules tab to the form each row \
targets. Row names are typed by hand and may differ from the real form name \
by spelling mistakes, extra or missing words, abbreviations, truncation, or \
spacing (e.g. row "Pregnancy Enrolment" targets the form "Pregnancy"; row \
"Refferal Followup" targets "Referral Follow Up").

Rules:
- `form` MUST be copied verbatim from the provided form-name list, or null \
when no listed form is a plausible target.
- Match each row to at most one form. Do not invent or modify form names.
- Prefer null over a doubtful match — a wrong match attaches a rule to the \
wrong form."""


class RuleRowMatch(BaseModel):
    """One Rules-tab row resolved to a form name (or null when unmatched)."""

    row: str
    form: str | None = None


class RuleRowMatches(BaseModel):
    matches: list[RuleRowMatch] = []


def match_rule_rows_to_forms(
    row_names: list[str],
    form_names: list[str],
    llm_helper,
) -> tuple[dict[str, str], list[str]]:
    """Resolve Rules-tab row names to form names with one Haiku call.

    Returns `({row_name: form_name}, warnings)`. Only rows resolved to a
    form present in `form_names` are included; hallucinated names are
    dropped with a warning, and rows the LLM left null are reported so the
    operator can fix the source sheet.
    """
    if not row_names or not form_names:
        return {}, []
    if not llm_helper.is_available():
        return {}, [
            f"rule-intent rows dropped (no LLM to match them): {sorted(row_names)}"
        ]

    user_prompt = json.dumps(
        {"forms": sorted(form_names), "rows": sorted(row_names)},
        ensure_ascii=False,
    )
    try:
        response = llm_helper.classify(_MATCH_SYSTEM_PROMPT, user_prompt, RuleRowMatches)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"rule-intent row matching failed: {exc}"]

    valid_forms = set(form_names)
    rows_pending = set(row_names)
    resolved: dict[str, str] = {}
    warnings: list[str] = []
    for match in response.matches:
        if match.row not in rows_pending:
            continue
        if match.form is None:
            continue
        if match.form not in valid_forms:
            warnings.append(
                f"rule-intent match rejected: row {match.row!r} -> "
                f"unknown form {match.form!r}"
            )
            continue
        resolved[match.row] = match.form
        rows_pending.discard(match.row)

    unresolved = rows_pending - set(resolved)
    if unresolved:
        warnings.append(
            f"rule-intent rows matched no form: {sorted(unresolved)}"
        )
    return resolved, warnings


def attach_unmatched_rule_intents(spec, llm_helper) -> list[str]:
    """Resolve `spec.unmatched_rule_intents` and attach them to their forms.

    Mutates the matched `FormSpec.rule_intents` in place and clears the
    pending dict. Returns warnings for anything that could not be placed.
    """
    pending = spec.unmatched_rule_intents
    if not pending:
        return []

    form_names = [f.name for f in spec.forms]
    resolved, warnings = match_rule_rows_to_forms(list(pending), form_names, llm_helper)

    forms_by_name = {f.name: f for f in spec.forms}
    for row, form_name in resolved.items():
        forms_by_name[form_name].rule_intents.update(pending[row])
        log.info("rule-intent row %r matched to form %r", row, form_name)

    spec.unmatched_rule_intents = {}
    return warnings

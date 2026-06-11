"""Column-level extraction of rule intents from a Rules / Form Rules tab.

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

See SDD §9.1.
"""

from __future__ import annotations

import logging

import pandas as pd

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
) -> None:
    """Mutate `FormSpec.rule_intents` in place to attach harvested intents.

    Matches each form to its row in the Rules tab by exact form name. Forms
    without a matching row are left untouched.
    """
    for form in forms:
        match = intents_by_form_name.get(form.name)
        if match:
            form.rule_intents.update(match)

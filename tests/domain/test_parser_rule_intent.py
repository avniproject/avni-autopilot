"""Unit tests for the field-rule intent composition in ``parse_form_df``.

Covers specs/FIELD_AND_PAGE_VISIBILITY_RULES_SDD.md §6.1:

- All five behaviour columns concatenate in fixed order with the
  documented connector phrases.
- Blank cells contribute nothing and a field whose every behaviour cell
  is empty receives ``rule_intent=None``.
- Header matching is position-independent (columns can move).
- ``_detect_visibility_patterns`` adds a natural-language visibility
  sentence for the three legacy idioms (Others / Sub-type / Yes-No
  detail), but only when the field doesn't already carry an explicit
  visibility-show fragment.

Usage:
    PYTHONPATH=src pytest tests/domain/test_parser_rule_intent.py
"""

from __future__ import annotations

import pandas as pd
import pytest

from domain.parser import parse_form_df
from models import FieldSpec


# ── DataFrame helpers ────────────────────────────────────────────────────────


_HEADERS = [
    "Page",
    "Field",
    "Data Type",
    "Mandatory",
    "Options",
    "When to show",
    "When not to show",
    "default_value",
    "validation",
    "option condition",
]


def _row(
    page: str = "",
    field: str = "",
    dtype: str = "Text",
    mandatory: str = "No",
    options: str = "",
    show: str = "",
    hide: str = "",
    default_value: str = "",
    validation: str = "",
    option_condition: str = "",
) -> list[str]:
    return [
        page, field, dtype, mandatory, options,
        show, hide, default_value, validation, option_condition,
    ]


def _df(rows: list[list[str]]) -> pd.DataFrame:
    return pd.DataFrame([_HEADERS, *rows])


def _fields(df: pd.DataFrame) -> list[FieldSpec]:
    form = parse_form_df(df, "TestForm", None)
    assert form is not None, "parse_form_df returned None"
    return [f for section in form.sections for f in section.fields]


# ── Single-cell composition ──────────────────────────────────────────────────


def test_visibility_show_only_cell_appears_at_start() -> None:
    df = _df([_row(field="Age", show="'HIV' is Negative")])
    [f] = _fields(df)
    assert f.rule_intent == "'HIV' is Negative"


def test_visibility_hide_only_cell_uses_hide_connector() -> None:
    df = _df([_row(field="Outcome", hide="outcome is Abortion")])
    [f] = _fields(df)
    assert f.rule_intent == "Hide this field when: outcome is Abortion"


def test_default_value_only_cell_uses_value_connector() -> None:
    df = _df([_row(field="Phone", default_value="copy from registration")])
    [f] = _fields(df)
    assert f.rule_intent == "Pre-fill / compute the value as: copy from registration"


def test_validation_only_cell_uses_block_save_connector() -> None:
    df = _df([_row(field="Age", validation="must be between 18 and 60")])
    [f] = _fields(df)
    assert f.rule_intent == "Block save when: must be between 18 and 60"


def test_option_condition_only_cell_uses_restrict_connector() -> None:
    df = _df([_row(field="Mode", options="A,B,C",
                   option_condition="only A when consent is Yes")])
    [f] = _fields(df)
    assert f.rule_intent == (
        "Restrict the answer options as follows: only A when consent is Yes"
    )


# ── Multi-cell composition ───────────────────────────────────────────────────


def test_three_populated_cells_concatenate_in_fixed_order() -> None:
    df = _df([_row(
        field="Visit date",
        show="'Outcome' is Abortion",
        default_value="copy from 'LMP'",
        validation="date is after today",
    )])
    [f] = _fields(df)
    assert f.rule_intent == (
        "'Outcome' is Abortion\n"
        "Pre-fill / compute the value as: copy from 'LMP'\n"
        "Block save when: date is after today"
    )


def test_all_blank_cells_yield_none() -> None:
    df = _df([_row(field="Plain")])
    [f] = _fields(df)
    assert f.rule_intent is None


def test_show_and_hide_both_populated_keeps_both_in_order() -> None:
    df = _df([_row(field="Bizarre",
                   show="A is X", hide="B is Y")])
    [f] = _fields(df)
    assert f.rule_intent == "A is X\nHide this field when: B is Y"


# ── Header-driven column matching ────────────────────────────────────────────


def test_columns_can_be_reordered_and_still_match() -> None:
    """`_col_idx` matches by header text, so swapping columns 5 and 8
    must not break the intent composition."""
    headers = [
        "Page", "Field", "Data Type", "Mandatory", "Options",
        "validation", "When not to show", "default_value", "When to show",
        "option condition",
    ]
    data = [
        "Demographics", "Age", "Number", "Yes", "",
        "must be between 18 and 60",
        "",
        "",
        "",
        "",
    ]
    df = pd.DataFrame([headers, data])
    form = parse_form_df(df, "TestForm", None)
    assert form is not None
    [section] = form.sections
    [field] = section.fields
    assert field.rule_intent == "Block save when: must be between 18 and 60"


# ── Auto-detected visibility patterns ────────────────────────────────────────


def test_others_pattern_adds_show_only_when_sentence() -> None:
    """Coded field with 'Others' option + a 'Specify' field afterwards →
    the spec-less specify field gains a visibility-show fragment."""
    df = _df([
        _row(field="Reason for refusal", dtype="Coded", options="Yes,No,Other"),
        _row(field="If others, specify",  dtype="Text"),
    ])
    fields = _fields(df)
    assert fields[0].rule_intent is None
    assert fields[1].rule_intent == "show only when 'Reason for refusal' is 'Other'"


def test_explicit_visibility_suppresses_auto_detected_show() -> None:
    """A modeller-provided 'When to show' cell wins over the auto-detect."""
    df = _df([
        _row(field="Reason for refusal", dtype="Coded", options="Yes,No,Other"),
        _row(field="If others, specify", dtype="Text",
             show="user explicitly authored this"),
    ])
    fields = _fields(df)
    assert fields[1].rule_intent == "user explicitly authored this"


def test_yes_no_detail_pattern_adds_show_only_when_sentence() -> None:
    df = _df([
        _row(field="Smoking", dtype="Coded", options="Yes,No"),
        _row(field="Smoking details", dtype="Text"),
    ])
    fields = _fields(df)
    assert fields[1].rule_intent == "show only when 'Smoking' is 'Yes'"


def test_auto_detect_can_layer_on_secondary_only_intent() -> None:
    """A field with a validation-only intent and no explicit visibility
    still receives the auto-detected visibility-show prepended."""
    df = _df([
        _row(field="Reason for refusal", dtype="Coded", options="Yes,No,Other"),
        _row(field="If others, specify", dtype="Text",
             validation="cannot be empty"),
    ])
    fields = _fields(df)
    assert fields[1].rule_intent == (
        "show only when 'Reason for refusal' is 'Other'\n"
        "Block save when: cannot be empty"
    )

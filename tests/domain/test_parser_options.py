"""Option parsing and numbering-prefix stripping (`domain.parser`)."""

import pytest

from domain.parser import _clean_field_name, _parse_options


class TestNumberPrefixStripping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1. Name", "Name"),
            ("2) Age", "Age"),
            ("A. Gender", "Gender"),
            ("1.2 Weight", "Weight"),
            ("1.2.3 Height", "Height"),
        ],
    )
    def test_numbering_prefixes_are_stripped(self, raw, expected):
        assert _clean_field_name(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            # Leading number/letter followed by a bare space is content
            # (real cells from the Astitva and Durga India workbooks).
            "5 Above and school children",
            "I don't know",
        ],
    )
    def test_bare_number_or_letter_prefix_is_kept(self, raw):
        assert _clean_field_name(raw) == raw


class TestParseOptions:
    def test_comma_separated_cell(self):
        # Astitva Beneficiary Registration — "Beneficiary Type" options cell.
        raw = "Child (0–5 yrs), AN Mother, 5 Above and school children"
        assert _parse_options(raw) == [
            "Child (0–5 yrs)",
            "AN Mother",
            "5 Above and school children",
        ]

    def test_newline_separated_cell(self):
        assert _parse_options("Yes\nNo\nI don't know") == ["Yes", "No", "I don't know"]

    def test_numbered_options_are_cleaned(self):
        assert _parse_options("1. Yes\n2. No") == ["Yes", "No"]

    def test_empty_cell(self):
        assert _parse_options(None) == []
        assert _parse_options("") == []


# ── Selection type from the data-type cell ───────────────────────────────────


def _parse_selection(rows: list[list[str]]) -> dict[str, str | None]:
    import pandas as pd

    from domain.parser import parse_form_df

    form = parse_form_df(pd.DataFrame(rows), "Form", None)
    return {f.name: f.selectionType for s in form.sections for f in s.fields}


def test_multi_select_data_type_makes_a_multi_select_field() -> None:
    selection = _parse_selection([
        ["Field Name", "Data Type", "Options"],
        ["Danger signs", "Multi Select", "Bleeding\nFever"],
        ["Blood group", "Single Select", "A\nB"],
        ["Remarks", "Text", ""],
    ])
    assert selection == {"Danger signs": "MultiSelect", "Blood group": "SingleSelect", "Remarks": None}


def test_a_filled_selection_type_cell_overrides_the_data_type() -> None:
    selection = _parse_selection([
        ["Field Name", "Data Type", "Options", "Selection Type"],
        ["Smoker", "Multi Select", "Yes\nNo", "Single"],
        ["Conditions", "Coded", "Anaemia\nDiabetes", "Multi"],
        ["Danger signs", "Multi-select", "Bleeding\nFever", ""],
    ])
    assert selection == {"Smoker": "SingleSelect", "Conditions": "MultiSelect", "Danger signs": "MultiSelect"}

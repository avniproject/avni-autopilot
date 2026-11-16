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


def test_a_page_name_that_comes_back_later_adds_to_the_earlier_page() -> None:
    import pandas as pd

    from domain.parser import parse_form_df

    form = parse_form_df(pd.DataFrame([
        ["Page Name", "Field Name", "Data Type"],
        ["Mother", "Age", "Numeric"],
        ["Mother", "Weight", "Numeric"],
        ["Child", "Birth weight", "Numeric"],
        ["Mother", "Height", "Numeric"],
    ]), "Form", None)
    assert [(s.name, [f.name for f in s.fields]) for s in form.sections] == [
        ("Mother", ["Age", "Weight", "Height"]),
        ("Child", ["Birth weight"]),
    ]


def test_a_page_name_that_comes_back_in_other_capitals_adds_to_the_earlier_page() -> None:
    import pandas as pd

    from domain.parser import parse_form_df

    form = parse_form_df(pd.DataFrame([
        ["Page Name", "Field Name", "Data Type"],
        ["Session Details", "Topic", "Text"],
        ["Observation", "Notes", "Text"],
        ["session details ", "Venue", "Text"],
    ]), "Form", None)
    assert [(s.name, [f.name for f in s.fields]) for s in form.sections] == [
        ("Session Details", ["Topic", "Venue"]),
        ("Observation", ["Notes"]),
    ]


@pytest.mark.parametrize("cell, expected", [
    ("Multi", "MultiSelect"), ("multi-select", "MultiSelect"), ("Multiple Select", "MultiSelect"),
    ("MULTISELECT", "MultiSelect"), ("Single", "SingleSelect"), ("single select", "SingleSelect"),
    ("Not multi", None), ("Single/Multi", None), ("Mutli Select", None), ("multi or single", None),
])
def test_only_whole_selection_type_spellings_are_read(cell: str, expected: str | None) -> None:
    import pandas as pd

    from domain.parser import parse_form_df

    form = parse_form_df(pd.DataFrame([
        ["Field Name", "Data Type", "Options", "Selection Type"],
        ["Conditions", "Coded", "Anaemia\nDiabetes", cell],
    ]), "Form", None)
    assert form.sections[0].fields[0].selectionType == expected
    assert form.unrecognised_selection_types == ({} if expected else {"Conditions": cell})


def test_an_unread_selection_type_is_warned_about() -> None:
    from domain.parser import _unrecognised_selection_type_warnings
    from models import FormSpec

    form = FormSpec(name="ANC", formType="Encounter",
                    unrecognised_selection_types={"Conditions": "Mutli Select"})
    assert _unrecognised_selection_type_warnings([form]) == [
        "Form 'ANC', field 'Conditions': Selection Type 'Mutli Select' is not one the builder "
        "reads (Single Select or Multi Select), so the field was read as single select."
    ]


@pytest.mark.parametrize("text, expected", [
    ("MultiSelect", "MultiSelect"), ("Multi Select", "MultiSelect"), ("multiple", "MultiSelect"),
    ("not multi", "SingleSelect"), ("Single/Multi", "SingleSelect"), ("Mutli", "SingleSelect"),
    (None, "SingleSelect"),
])
def test_form_element_type_reads_whole_spellings(text: str | None, expected: str) -> None:
    from domain.generators import form_element_type

    assert form_element_type(text) == expected


@pytest.mark.parametrize("data_type, cell, expected, warned", [
    ("Multi Select", "Multiple answers", "MultiSelect", False),  # the data type decided it
    ("Text", "NA", None, False),                                  # the setting means nothing
    ("Coded", "-", None, True),
])
def test_a_selection_type_warning_only_where_it_left_the_field_single(
    data_type: str, cell: str, expected: str | None, warned: bool,
) -> None:
    import pandas as pd

    from domain.parser import parse_form_df

    form = parse_form_df(pd.DataFrame([
        ["Field Name", "Data Type", "Options", "Selection Type"],
        ["Conditions", data_type, "Anaemia\nDiabetes" if data_type != "Text" else "", cell],
    ]), "Form", None)
    assert form.sections[0].fields[0].selectionType == expected
    assert bool(form.unrecognised_selection_types) is warned

"""A field the build renamed keeps its spreadsheet name, so an update finds it again."""
from __future__ import annotations

import copy

from domain.diff import diff
from domain.enricher import enrich_forms
from domain.generators import SPREADSHEET_NAME_KEY, make_forms_and_concepts
from domain.recorded_names import match_recorded_names, record_spreadsheet_names
from models import FieldSpec, FormSpec, SectionSpec

SHEET_NAME = "What is the main purpose of the PoSH Act at the workplace? (Baseline)"
BUILD_NAME = "Main purpose of the PoSH Act (Baseline)"


def _sheet(*extra: str) -> list[FormSpec]:
    """The forms as parsed from the spreadsheet, before any rename."""
    fields = [FieldSpec(name="Age", dataType="Numeric"), FieldSpec(name=SHEET_NAME), *(FieldSpec(name=n) for n in extra)]
    forms = [FormSpec(name="Baseline", formType="IndividualProfile", sections=[SectionSpec(name="Details", fields=fields)])]
    record_spreadsheet_names(forms)
    return forms


def _built(forms: list[FormSpec]) -> dict:
    """A bundle snapshot generated from the forms."""
    result = make_forms_and_concepts(forms)
    return {"forms": result["forms"], "concepts": result["concepts"]}


def _build_with_rename() -> dict:
    forms = _sheet()
    forms[0].sections[0].fields[1].name = BUILD_NAME  # the name review's rename, accepted
    return _built(forms)


def _elements(bundle: dict) -> list[dict]:
    return [e for f in bundle["forms"] for g in f["content"]["formElementGroups"] for e in g["formElements"]]


def _app_forms(bundle: dict) -> list[dict]:
    return [f["content"] for f in bundle["forms"]]


def _update(app: dict, forms: list[FormSpec]) -> tuple[set, object]:
    matched = match_recorded_names(forms, _app_forms(app))
    return matched, diff(_built(forms), app)


def test_a_build_records_the_spreadsheet_name_only_on_renamed_fields() -> None:
    elements = {e["name"]: e for e in _elements(_build_with_rename())}

    assert {"key": SPREADSHEET_NAME_KEY, "value": SHEET_NAME} in elements[BUILD_NAME]["keyValues"]
    assert all(kv["key"] != SPREADSHEET_NAME_KEY for kv in elements["Age"]["keyValues"])


def test_an_update_keeps_the_renamed_field_and_does_not_review_it_again() -> None:
    forms = _sheet()

    matched, update = _update(_build_with_rename(), forms)

    assert matched == {("Baseline", BUILD_NAME)}
    assert update.ops == []
    assert forms[0].sections[0].fields[1].name == BUILD_NAME


def test_a_field_renamed_again_in_app_designer_is_still_found() -> None:
    app = _build_with_rename()
    element = next(e for e in _elements(app) if e["name"] == BUILD_NAME)
    element["name"] = "PoSH Act purpose"
    element["concept"]["name"] = "PoSH Act purpose"  # concept UUID stays

    matched, update = _update(app, _sheet())

    assert matched == {("Baseline", "PoSH Act purpose")}
    assert [op["kind"] for op in update.ops] == []


def test_a_new_row_resembling_a_renamed_field_is_added_not_matched() -> None:
    forms = _sheet("What is the main purpose of the POCSO Act? (Baseline)")

    matched, update = _update(_build_with_rename(), forms)

    assert matched == {("Baseline", BUILD_NAME)}
    assert [(op["kind"], op["payload"].get("name")) for op in update.ops if op["kind"] != "field.reorder"] == [
        ("field.add", "What is the main purpose of the POCSO Act? (Baseline)")]


def test_an_app_built_before_names_were_recorded_updates_as_before() -> None:
    app = _build_with_rename()
    for element in _elements(app):
        element["keyValues"] = [kv for kv in element["keyValues"] if kv["key"] != SPREADSHEET_NAME_KEY]
    before = diff(_built(_sheet()), copy.deepcopy(app))

    matched, after = _update(app, _sheet())

    assert matched == set()
    assert after.ops == before.ops


def test_unclear_matches_are_left_to_the_review() -> None:
    twice_in_app = _build_with_rename()
    copy_of_field = copy.deepcopy(next(e for e in _elements(twice_in_app) if e["name"] == BUILD_NAME))
    copy_of_field.update(uuid="another", name="Other copy")
    twice_in_app["forms"][0]["content"]["formElementGroups"][0]["formElements"].append(copy_of_field)
    assert match_recorded_names(_sheet(), _app_forms(twice_in_app)) == set()

    assert match_recorded_names(_sheet(SHEET_NAME), _app_forms(_build_with_rename())) == set()
    assert match_recorded_names(_sheet(BUILD_NAME), _app_forms(_build_with_rename())) == set()


def test_a_voided_field_is_not_matched() -> None:
    app = _build_with_rename()
    next(e for e in _elements(app) if e["name"] == BUILD_NAME)["voided"] = True

    assert match_recorded_names(_sheet(), _app_forms(app)) == set()


def test_a_field_an_update_adds_after_a_rename_records_its_spreadsheet_name() -> None:
    from domain.diff import _add_payload_from_element
    from domain.bundle_editor import _build_key_values

    element = next(e for e in _elements(_build_with_rename()) if e["name"] == BUILD_NAME)

    assert {"key": SPREADSHEET_NAME_KEY, "value": SHEET_NAME} in _build_key_values(
        _add_payload_from_element(element))
    assert _build_key_values({"name": SHEET_NAME, "spreadsheetName": SHEET_NAME}) == []


def test_the_name_review_skips_matched_fields() -> None:
    class _NoModel:
        def is_available(self) -> bool:
            raise AssertionError("no field should need review")

    forms = _sheet()
    forms[0].sections[0].fields[1].name = "x" * 300
    forms[0].sections[0].fields[1].conceptUuid = "app-concept"
    enrich_forms(forms, None, _NoModel())


def test_two_rows_that_would_take_the_same_app_name_are_left_to_the_review() -> None:
    """Two app fields can share a name; each recorded a different spreadsheet name."""
    forms = _sheet("Wt (kg)", "Body weight")
    forms[0].sections[0].fields[2].name = "Weight"
    app = _built(forms)
    weight = next(e for e in _elements(app) if e["name"] == "Weight")
    other = copy.deepcopy(weight)
    other.update(uuid="other-weight")
    other["concept"] = {**other["concept"], "uuid": "other-concept"}
    other["keyValues"] = [{"key": SPREADSHEET_NAME_KEY, "value": "Body weight"}]
    app["forms"][0]["content"]["formElementGroups"][0]["formElements"].append(other)

    sheet = _sheet("Wt (kg)", "Body weight")
    assert match_recorded_names(sheet, _app_forms(app)) == set()
    assert [f.name for f in sheet[0].sections[0].fields][2:] == ["Wt (kg)", "Body weight"]


def _two_pages(first: str, second: str) -> list[FormSpec]:
    forms = [FormSpec(name="Baseline", formType="IndividualProfile", sections=[
        SectionSpec(name="Intake", fields=[FieldSpec(name=first, dataType="Numeric")]),
        SectionSpec(name="Follow-up", fields=[FieldSpec(name=second, dataType="Numeric")]),
    ])]
    record_spreadsheet_names(forms)
    return forms


def test_a_repeated_name_the_build_renamed_on_one_page_is_found_by_its_page() -> None:
    built = _two_pages("Weight", "Weight")
    built[0].sections[1].fields[0].name = "Weight (follow-up)"  # the review's rename of the second copy
    app = _built(built)

    sheet = _two_pages("Weight", "Weight")
    matched = match_recorded_names(sheet, _app_forms(app))

    assert matched == {("Baseline", "Weight (follow-up)")}
    assert [s.fields[0].name for s in sheet[0].sections] == ["Weight", "Weight (follow-up)"]
    assert diff(_built(sheet), app).ops == []


def test_a_repeated_name_renamed_on_both_pages_is_found_on_each_page() -> None:
    built = _two_pages("Weight", "Weight")
    built[0].sections[0].fields[0].name = "Weight (intake)"
    built[0].sections[1].fields[0].name = "Weight (follow-up)"
    app = _built(built)

    sheet = _two_pages("Weight", "Weight")

    assert match_recorded_names(sheet, _app_forms(app)) == {
        ("Baseline", "Weight (intake)"), ("Baseline", "Weight (follow-up)")}
    assert diff(_built(sheet), app).ops == []


def test_an_update_without_a_bundle_leaves_matching_to_the_diff_error() -> None:
    from pipeline.nodes import match_fields_by_recorded_name

    state = {"mode": "edit_from_spec", "bundle_path": "/no/such/bundle.zip",
             "entity_spec": type("Spec", (), {"forms": _sheet()})()}
    assert match_fields_by_recorded_name(state) == {}
    assert match_fields_by_recorded_name({**state, "bundle_path": ""}) == {}


def test_a_rename_that_only_changes_capitals_is_recorded_and_kept() -> None:
    forms = _sheet()
    forms[0].sections[0].fields[1].name = SHEET_NAME.upper()
    app = _built(forms)
    element = next(e for e in _elements(app) if e["name"] == SHEET_NAME.upper())
    assert {"key": SPREADSHEET_NAME_KEY, "value": SHEET_NAME} in element["keyValues"]

    sheet = _sheet()
    assert match_recorded_names(sheet, _app_forms(app)) == {("Baseline", SHEET_NAME.upper())}
    assert diff(_built(sheet), app).ops == []


def test_a_repeated_name_is_still_found_after_its_page_is_renamed_in_app_designer() -> None:
    built = _two_pages("Weight", "Weight")
    built[0].sections[1].fields[0].name = "Weight (follow-up)"
    app = _built(built)
    page = app["forms"][0]["content"]["formElementGroups"][1]
    page["name"] = "Follow up visit"  # App Designer keeps the page UUID

    sheet = _two_pages("Weight", "Weight")

    assert match_recorded_names(sheet, _app_forms(app)) == {("Baseline", "Weight (follow-up)")}

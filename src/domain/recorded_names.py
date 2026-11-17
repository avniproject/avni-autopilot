"""Spreadsheet names recorded on the app's fields, so an update finds a renamed field again.

When the name review renames a field at build, the form element keeps the
spreadsheet's name in the keyValue `SPREADSHEET_NAME_KEY`. An update reads the
same spreadsheet name again; without this record it would see a field the app
does not have, ask about its name again and replace the app's field (with its
saved answers) by a new one.

`record_spreadsheet_names` notes every field's spreadsheet name right after
parsing. `match_recorded_names` runs in an update before the name review: a
spreadsheet row whose name an app field has recorded takes that field's name
and concept, and the name review leaves it alone (it has the app's concept). Apps built before names were recorded
have no such keyValue, so nothing matches and an update runs as before.
"""
from __future__ import annotations

import logging
from collections import Counter

from domain.diff import concept_uuid, is_live
from domain.generators import SPREADSHEET_NAME_KEY, form_uuid, make_uuid, name_key
from models import FormSpec

log = logging.getLogger(__name__)


def record_spreadsheet_names(forms: list[FormSpec]) -> None:
    """Note each field's name as the spreadsheet gives it, before any rename."""
    for form in forms:
        for section in form.sections:
            for field in section.fields:
                if field.spreadsheetName is None:
                    field.spreadsheetName = field.name


def _recorded_name(element: dict) -> str:
    return next((
        str(kv.get("value") or "") for kv in element.get("keyValues") or []
        if kv.get("key") == SPREADSHEET_NAME_KEY
    ), "")


def _live_elements_by_page(form: dict) -> list[tuple[str, dict]]:
    """(page UUID, element) for every live element of the form."""
    return [
        (g.get("uuid"), e)
        for g in form.get("formElementGroups") or [] if is_live(g)
        for e in g.get("formElements") or [] if is_live(e)
    ]


def _page_uuid(form_name: str, page_name: str) -> str:
    """The UUID a build gives a page, which App Designer keeps when it renames the page."""
    return make_uuid(f"formGroup:{form_name}:{page_name}")


def match_recorded_names(forms: list[FormSpec], app_forms: list[dict]) -> set[tuple[str, str]]:
    """Give each spreadsheet row the app field that recorded its name.

    A row matches a live field of the same form (by form UUID) whose recorded
    spreadsheet name equals the row's name, ignoring case and outer spaces.
    When that name is on one row and one field, they match. When it repeats
    (the review renamed one copy of a repeated name), a row matches the one
    field with that name on its own page, found by the page's UUID so a page
    renamed in App Designer still counts. The row then takes the field's name
    and concept, so the update keeps that field.

    Left alone: an unclear match, an app name another row already uses, and
    an app name two matched rows would share.

    Returns (form name, field name) of every matched row.
    """
    app_by_uuid = {f.get("uuid"): f for f in app_forms}
    matched: set[tuple[str, str]] = set()
    for form in forms:
        app_form = app_by_uuid.get(form_uuid(form.name))
        if app_form is None:
            continue
        recorded: list[tuple[str, str, dict]] = [
            (key, page, element)
            for page, element in _live_elements_by_page(app_form)
            if (key := name_key(_recorded_name(element)))
        ]
        if not recorded:
            continue
        rows = [(_page_uuid(form.name, section.name), field) for section in form.sections for field in section.fields]
        rows_by_name = Counter(name_key(field.name) for _, field in rows)
        rows_by_name_and_page = Counter((name_key(field.name), page) for page, field in rows)
        row_names = set(rows_by_name)
        candidates = []
        for page, field in rows:
            key = name_key(field.name)
            same_name = [e for k, _, e in recorded if k == key]
            if not (rows_by_name[key] == 1 and len(same_name) == 1):
                same_name = [e for k, p, e in recorded if k == key and p == page]
                if not (rows_by_name_and_page[(key, page)] == 1 and len(same_name) == 1):
                    continue
            element = same_name[0]
            app_name = element.get("name") or ""
            app_concept = concept_uuid(element)
            if not app_name or not app_concept:
                continue
            if name_key(app_name) != key and name_key(app_name) in row_names:
                continue
            candidates.append((field, app_name, app_concept))
        app_name_counts = Counter(name_key(app_name) for _, app_name, _ in candidates)
        for field, app_name, app_concept in candidates:
            if app_name_counts[name_key(app_name)] > 1:
                continue
            field.name = app_name
            field.conceptUuid = app_concept
            matched.add((form.name, app_name))
    if matched:
        log.info(f"[recorded-names] {len(matched)} spreadsheet row(s) matched to app fields by recorded name")
    return matched

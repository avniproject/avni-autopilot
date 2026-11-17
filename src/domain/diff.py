"""
Diff two bundle JSON snapshots and emit a list of EditOperation dicts that,
when applied to the current bundle, will reshape it to match the desired bundle.

Inputs (both bundle-shaped — see domain.bundle_editor.load_bundle_snapshot):
    {"forms":    [{"file_name": str, "content": <form_dict>}, ...],
     "concepts": [<concept_dict>, ...]}

Output:
    BundleDiff — EditOperation dicts ready for `apply_field_edits`, and what
    the update leaves as it is: fields whose single/multi select setting
    differs, fields the spreadsheets put on another page, and spreadsheet
    rows whose names clash.

Identity rule:
    Within a section, two form elements are the same field when their `uuid`
    matches. UUIDs come from the generator's deterministic seed
    (`formElement:<form>:<field>`), so the diff treats them as opaque match
    keys. Pages are matched by UUID; a page on one side only is logged and
    left as it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from domain.generators import (
    SELECTION_TYPE_DATA_TYPES,
    SPREADSHEET_NAME_KEY,
    form_element_type,
    name_key,
    selection_words,
)

log = logging.getLogger(__name__)


@dataclass
class BundleDiff:
    """What an update does to the current bundle, and what it leaves.

    `selection_type_differences` names, as "Form › Field (<current> in the
    app, <desired> in the spreadsheets)", fields kept single or multi select
    although the desired bundle has the other setting.

    `name_clashes` names desired fields left out because another field of the
    form has the same name apart from capitals: Avni shows them as one
    question, and an update cannot tell them apart.

    `fields_on_another_page` names, as "Form › Field (page '<app>' in the app,
    '<sheet>' in the spreadsheets)", fields left on their page in the app: an
    update does not move fields between pages, as a moved field would be a
    new element without the original's rules and settings.

    `pages_not_added` and `pages_not_removed` name, as "Form › Page", pages
    only in the desired or only in the current bundle; an update leaves them
    as they are.

    `unapplied_changes` names, as "Form › Field (<kinds>)", fields the update
    keeps although the desired bundle adds options or changes their
    mandatory setting, data type, unit or limits, which an update does not
    apply.
    """

    ops: list[dict] = field(default_factory=list)
    unapplied_changes: list[str] = field(default_factory=list)
    selection_type_differences: list[str] = field(default_factory=list)
    name_clashes: list[str] = field(default_factory=list)
    fields_on_another_page: list[str] = field(default_factory=list)
    pages_not_added: list[str] = field(default_factory=list)
    pages_not_removed: list[str] = field(default_factory=list)


def diff(desired_bundle: dict, current_bundle: dict) -> BundleDiff:
    """Produce EditOperation dicts that morph current_bundle → desired_bundle.

    Ops, in the order they run:
      - field.remove            (live in current, not in desired)
      - field.add               (in desired, not live in current)
      - section.reorder_fields  (fields live in both are in another order)

    Only pages in both bundles are diffed; pages and forms on one side only
    are logged and left as they are. A field the desired bundle puts on
    another page of the form stays where it is and is named in
    `fields_on_another_page`.
    A field's single/multi select setting is never switched: avni-server
    refuses that on a field it already has, so the field is named in
    `selection_type_differences` instead.
    """
    result = BundleDiff()
    op_counter = 0

    def _next_id() -> str:
        nonlocal op_counter
        op_counter += 1
        return f"diff-{op_counter}"

    desired_forms = {f["content"].get("uuid"): f for f in desired_bundle.get("forms", [])}
    current_forms = {f["content"].get("uuid"): f for f in current_bundle.get("forms", [])}

    only_in_desired = set(desired_forms) - set(current_forms)
    only_in_current = set(current_forms) - set(desired_forms)
    if only_in_desired:
        log.warning("diff: %d form(s) only in desired (new forms not yet supported): %s",
                    len(only_in_desired),
                    [desired_forms[u]["content"].get("name") for u in only_in_desired])
    if only_in_current:
        log.warning("diff: %d form(s) only in current (form deletion not yet supported): %s",
                    len(only_in_current),
                    [current_forms[u]["content"].get("name") for u in only_in_current])

    # Only diff forms that exist in both.
    for form_uuid in sorted(desired_forms.keys() & current_forms.keys(), key=str):
        desired_form = desired_forms[form_uuid]["content"]
        current_form = current_forms[form_uuid]["content"]
        form_name = desired_form.get("name")
        _diff_form(desired_form, current_form, form_name, result, _next_id)

    return result


def unmatched_desired_forms(desired_bundle: dict, current_bundle: dict) -> list[str]:
    """Names of desired forms with no form of the same UUID in current_bundle.

    `diff` cannot add whole forms, so these are left out of an update.
    """
    current_uuids = {f["content"].get("uuid") for f in current_bundle.get("forms", [])}
    return sorted({
        f["content"].get("name", "")
        for f in desired_bundle.get("forms", [])
        if f["content"].get("uuid") not in current_uuids
    })


def entities_not_in_bundle(desired: dict[str, list[str]], current: dict[str, set[str]]) -> list[str]:
    """The desired subject types, programs and visit types the current bundle
    lacks, as "<kind> '<name>'"; an update does not add them. A kind missing
    from `current` (the bundle lacks its file) is not checked."""
    found: list[str] = []
    for kind, names in desired.items():
        if kind not in current:
            continue
        named: set[str] = set()
        for name in names:
            key = name_key(name)
            if key and key not in current[kind] and key not in named:
                named.add(key)
                found.append(f"{kind} {name!r}")
    return found


def current_forms_not_in_desired(desired_bundle: dict, current_bundle: dict) -> list[str]:
    """Names of live forms in current_bundle with no form of the same UUID in desired_bundle.

    `diff` does not remove whole forms, so an update leaves these as they are.
    """
    desired_uuids = {f["content"].get("uuid") for f in desired_bundle.get("forms", [])}
    return sorted({
        f["content"].get("name", "")
        for f in current_bundle.get("forms", [])
        if f["content"].get("uuid") not in desired_uuids and not f["content"].get("voided", False)
    })


# ── Per-form diff ─────────────────────────────────────────────────────────────


def _diff_form(desired_form: dict, current_form: dict, form_name: str,
               result: BundleDiff, next_id) -> None:
    desired_groups = _without_name_clashes(desired_form, current_form, form_name, result)
    desired_sections = {g.get("uuid"): g for g in desired_groups}
    # A voided page counts as missing: fields added to it would never show.
    current_sections = {
        g.get("uuid"): g for g in current_form.get("formElementGroups", []) if is_live(g)
    }

    only_in_desired = [g for u, g in desired_sections.items() if u not in current_sections]
    only_in_current = [g for u, g in current_sections.items() if u not in desired_sections]
    result.pages_not_added.extend(f"{form_name} › {g.get('name')}" for g in only_in_desired)
    result.pages_not_removed.extend(f"{form_name} › {g.get('name')}" for g in only_in_current)
    if only_in_desired or only_in_current:
        log.warning(f"diff: form {form_name!r} pages left as they are: new "
                    f"{[g.get('name') for g in only_in_desired]}, not in the spreadsheets "
                    f"{[g.get('name') for g in only_in_current]}")

    shared = [(d, current_sections[u]) for u, d in desired_sections.items() if u in current_sections]
    pairings = [(d, c, _pair_elements(d, c)) for d, c in shared]
    on_other_page = _fields_on_another_page(desired_groups, current_form, pairings, form_name, result)

    for d, current_section, pairs in pairings:
        _emit_removals(current_section, pairs, form_name, result, next_id, keep=on_other_page)
    for d, current_section, pairs in pairings:
        elsewhere = {
            e["uuid"]: g for g in current_form.get("formElementGroups", [])
            if g is not current_section for e in g.get("formElements", [])
        }
        _diff_section(d, current_section, pairs, _page_target(form_name, current_section),
                      result, next_id, keep=on_other_page, elsewhere=elsewhere)


def _fields_on_another_page(desired_groups: list[dict], current_form: dict, pairings: list,
                            form_name: str, result: BundleDiff) -> set[str]:
    """Concepts of fields live on one page in the app and placed on another
    page in the desired form; they are neither removed nor added."""
    live_page_of: dict[str, dict] = {}
    for group in current_form.get("formElementGroups", []):
        if not is_live(group):
            continue
        for e in group.get("formElements", []):
            if is_live(e) and _field_key(e):
                live_page_of.setdefault(_field_key(e), group)
    stays = {
        _field_key(e)
        for _, current_section, pairs in pairings
        for e in current_section.get("formElements", [])
        if is_live(e) and any(p is e for p in pairs.values())
    }
    kept: set[str] = set()
    for group in desired_groups:
        for e in group.get("formElements", []):
            concept = _field_key(e)
            app_page = live_page_of.get(concept)
            if concept in stays or app_page is None or app_page.get("uuid") == group.get("uuid"):
                continue
            kept.add(concept)
            if name_key(app_page.get("name")) == name_key(group.get("name")):
                continue  # the same page as far as the user can tell, e.g. one made in App Designer
            result.fields_on_another_page.append(
                f"{form_name} › {e.get('name')} (page {app_page.get('name')!r} in the app, "
                f"{group.get('name')!r} in the spreadsheets)"
            )
    return kept


def _page_target(form_name: str, page: dict) -> dict:
    return {"form": form_name, "section": page.get("name"), "section_uuid": page.get("uuid")}


def _without_name_clashes(desired_form: dict, current_form: dict, form_name: str,
                          result: BundleDiff) -> list[dict]:
    """The desired pages, less each field whose name repeats another field of
    the form, ignoring capitals. Of each such set the one the app has live is
    kept, else the first."""
    live_in_app = {
        e["uuid"] for g in current_form.get("formElementGroups", [])
        for e in g.get("formElements", []) if is_live(e)
    }
    placed = [
        (e, group.get("name"))
        for group in desired_form.get("formElementGroups", [])
        for e in group.get("formElements", [])
    ]
    keeper: dict[str, tuple[dict, str]] = {}
    for e, page in placed:
        key = name_key(e.get("name"))
        if key not in keeper or (e["uuid"] in live_in_app and keeper[key][0]["uuid"] not in live_in_app):
            keeper[key] = (e, page)
    left_out = set()
    for e, page in placed:
        kept, kept_page = keeper[name_key(e.get("name"))]
        if e is kept:
            continue
        left_out.add(id(e))
        apart = " apart from capitals" if e.get("name") != kept.get("name") else ""
        result.name_clashes.append(
            f"{form_name} › {e.get('name')!r} (page {page!r}) has the same name as "
            f"{kept.get('name')!r} (page {kept_page!r}){apart}"
        )
    return [
        {**group, "formElements": [e for e in group.get("formElements", []) if id(e) not in left_out]}
        for group in desired_form.get("formElementGroups", [])
    ]


# ── Per-section diff ──────────────────────────────────────────────────────────


def concept_uuid(element: dict) -> str | None:
    return (element.get("concept") or {}).get("uuid")


def _field_key(element: dict) -> str | None:
    """What makes a field the same one on another page: its concept, else its element UUID."""
    return concept_uuid(element) or element.get("uuid")


def _pair_elements(desired_section: dict, current_section: dict) -> dict[str, dict]:
    """Current element for each desired element UUID, where the section has one.

    A desired element pairs with the live element of its UUID; failing that,
    with an unpaired live element of the same concept (one added in App
    Designer, or given a page-tied UUID by an earlier build); failing that,
    with the voided element of its UUID, to be reinstated.
    """
    desired_elements = desired_section.get("formElements", [])
    current_elements = current_section.get("formElements", [])
    current_by_uuid = {e["uuid"]: e for e in current_elements}
    pairs: dict[str, dict] = {
        e["uuid"]: current_by_uuid[e["uuid"]]
        for e in desired_elements if is_live(current_by_uuid.get(e["uuid"]))
    }
    desired_uuids = {e["uuid"] for e in desired_elements}
    unpaired_by_concept: dict[str, list[dict]] = {}
    for e in current_elements:
        if is_live(e) and e["uuid"] not in desired_uuids and concept_uuid(e):
            unpaired_by_concept.setdefault(concept_uuid(e), []).append(e)
    for desired_e in desired_elements:
        if desired_e["uuid"] in pairs:
            continue
        same_concept = unpaired_by_concept.get(concept_uuid(desired_e))
        if same_concept:
            pairs[desired_e["uuid"]] = same_concept.pop(0)
        elif desired_e["uuid"] in current_by_uuid:
            pairs[desired_e["uuid"]] = current_by_uuid[desired_e["uuid"]]
    return pairs


def is_live(item: dict | None) -> bool:
    """True for a present, non-voided element or page; `{}` stands for none."""
    return bool(item) and not item.get("voided", False)


def _emit_removals(current_section: dict, pairs: dict[str, dict], form_name: str,
                   result: BundleDiff, next_id, keep: set[str]) -> None:
    """field.remove for each live current element no desired element pairs
    with, except fields the desired form puts on another page.

    The target carries the element's UUID, as two live elements of a section
    can share a name.
    """
    paired_ids = {id(e) for e in pairs.values()}
    for e in current_section.get("formElements", []):
        if not is_live(e) or id(e) in paired_ids or _field_key(e) in keep:
            continue
        result.ops.append({
            "op_id": next_id(),
            "kind": "field.remove",
            "target": {**_page_target(form_name, current_section),
                       "field": e.get("name"), "field_uuid": e["uuid"]},
            "payload": {},
        })


def _diff_section(desired_section: dict, current_section: dict, pairs: dict[str, dict],
                  target: dict, result: BundleDiff, next_id, keep: set[str],
                  elsewhere: dict[str, dict]) -> None:
    form_name = target["form"]
    ops = result.ops

    # Desired order is the order in which form elements appear in the desired
    # section's formElements list (the spec's intent for ordering).
    desired_elements = []
    for e in desired_section.get("formElements", []):
        if not is_live(pairs.get(e["uuid"])):
            if _field_key(e) in keep:
                continue
            other_page = elsewhere.get(e["uuid"])
            if other_page is not None and e["uuid"] not in pairs:
                # Removed from another page earlier; its element cannot come back here.
                result.fields_on_another_page.append(
                    f"{form_name} › {e.get('name')} (removed from page {other_page.get('name')!r} "
                    f"in the app, on {desired_section.get('name')!r} in the spreadsheets)"
                )
                continue
        desired_elements.append(e)
    desired_by_uuid = {e["uuid"]: e for e in desired_elements}
    desired_order = [e["uuid"] for e in desired_elements]

    # ── SELECTION SETTING: kept on every field current already has ────────
    for uuid in desired_order:
        if uuid in pairs:
            _note_selection_difference(desired_by_uuid[uuid], pairs[uuid], form_name, result)
        if is_live(pairs.get(uuid)):
            _note_unapplied_change(desired_by_uuid[uuid], pairs[uuid], form_name, result)

    # ── ADD: in desired, with no live current element ─────────────────────
    for uuid in desired_order:
        current_e = pairs.get(uuid)
        if is_live(current_e):
            continue
        payload = _add_payload_from_element(desired_by_uuid[uuid])
        if current_e is not None and payload["dataType"] in SELECTION_TYPE_DATA_TYPES:
            # Reinstating keeps the element's single/multi select setting.
            payload["selectionType"] = form_element_type(current_e.get("type"))
        ops.append({
            "op_id": next_id(),
            "kind": "field.add",
            "target": dict(target),
            "payload": payload,
        })

    # ── REORDER: emitted only if the fields live in both are in another
    # relative order than the desired one.
    surviving = [u for u in desired_order if is_live(pairs.get(u))]
    position = {id(pairs[u]): u for u in surviving}
    current_order = [
        position[id(e)]
        for e in sorted(current_section.get("formElements", []),
                        key=lambda x: x.get("displayOrder") or 0)
        if id(e) in position
    ]
    if current_order != surviving:
        order = [
            (pairs[u] if is_live(pairs.get(u)) else desired_by_uuid[u]).get("name")
            for u in desired_order
        ]
        _place_fields_left_on_the_page(order, current_section, pairs, keep)
        ops.append({
            "op_id": next_id(),
            "kind": "section.reorder_fields",
            "target": dict(target),
            # A surviving field goes by its name in the app, which a rename
            # there can have changed; an added one by its spreadsheet name.
            "payload": {"order": order},
        })


def _place_fields_left_on_the_page(order: list[str], current_section: dict,
                                   pairs: dict[str, dict], keep: set[str]) -> None:
    """Insert the live fields kept on this page (placed on another page in the
    spreadsheets) into `order`, each right after the field before it on the
    page, as a reorder must name every live field."""
    paired = {id(e) for e in pairs.values()}
    previous: str | None = None
    for e in sorted(current_section.get("formElements", []), key=lambda x: x.get("displayOrder") or 0):
        if not is_live(e):
            continue
        if id(e) in paired:
            previous = e.get("name")
        elif _field_key(e) in keep:
            at = order.index(previous) + 1 if previous in order else 0
            order.insert(at, e.get("name"))
            previous = e.get("name")


def _note_unapplied_change(desired_e: dict, current_e: dict, form_name: str,
                           result: BundleDiff) -> None:
    """Name a live field whose options, mandatory setting, data type, unit or
    limits differ, as an update keeps the field as it is."""
    desired_concept = desired_e.get("concept") or {}
    current_concept = current_e.get("concept") or {}
    kinds = []
    if _data_type_differs(desired_concept.get("dataType"), current_concept.get("dataType")):
        kinds.append("data type")
    elif desired_concept.get("dataType") == "Coded" and \
            _answer_names(desired_concept) - _answer_names(current_concept):
        # Only options the app lacks: the app's concept can hold more, shared
        # with other forms or excluded on this one, and a blank cell keeps them.
        kinds.append("options")
    # A blank cell in the spreadsheet keeps the app's setting, as with options:
    # a blank Mandatory cell reads the same as "No", so only "Yes" is told,
    # and a blank unit or limit (or one side of a range) is not compared.
    if desired_e.get("mandatory") and not current_e.get("mandatory"):
        kinds.append("mandatory")
    if _differs_where_given(desired_e, current_e, "unit", ignore_case=True):
        kinds.append("unit")
    if any(_differs_where_given(desired_e, current_e, k) for k in ("min", "max")):
        kinds.append("limits")
    if kinds:
        result.unapplied_changes.append(f"{form_name} › {desired_e.get('name')} ({', '.join(kinds)})")


def _answer_names(concept: dict) -> set[str]:
    return {
        name_key(answer.get("name"))
        for answer in concept.get("answers") or []
        if not answer.get("voided", False)
    }


# Where a field's concept holds each setting; App Designer sets them there.
_CONCEPT_SETTINGS = {"unit": "unit", "min": "lowAbsolute", "max": "highAbsolute"}


def _setting(element: dict, key: str) -> str | None:
    """The field's unit, min or max as comparable text: the concept's value
    when it has one, else the field's own key-value."""
    concept_value = _comparable((element.get("concept") or {}).get(_CONCEPT_SETTINGS[key]))
    if concept_value is not None:
        return concept_value
    return _comparable(next((kv.get("value") for kv in element.get("keyValues") or [] if kv.get("key") == key), None))


# The same data type under an older and a newer name.
_SAME_DATA_TYPE = {"Image": "ImageV2"}


def _data_type_differs(wanted: str | None, current: str | None) -> bool:
    """Whether the spreadsheet gives a data type the app field does not have.
    A blank or unknown Data Type cell is read as "Text", so "Text" is not
    told, as with a blank cell for the other settings."""
    if not wanted or wanted == "Text":
        return False
    return _SAME_DATA_TYPE.get(wanted, wanted) != _SAME_DATA_TYPE.get(current, current)


def _differs_where_given(desired_e: dict, current_e: dict, key: str, ignore_case: bool = False) -> bool:
    """Whether the spreadsheet gives this setting and it differs from the app's."""
    wanted, current = _setting(desired_e, key), _setting(current_e, key)
    if ignore_case:
        wanted, current = name_key(wanted) or None, name_key(current) or None
    return wanted is not None and wanted != current


def _comparable(value: object) -> str | None:
    """A setting as comparable text: numbers as numbers, so 10 and "10.0" agree."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return str(value).strip()


def _note_selection_difference(desired_e: dict, current_e: dict, form_name: str,
                               result: BundleDiff) -> None:
    """Name a field whose setting differs, live or about to be reinstated.

    Only incoming data types that take the setting count, as avni-server
    checks only those.
    """
    if (desired_e.get("concept") or {}).get("dataType") not in SELECTION_TYPE_DATA_TYPES:
        return
    if form_element_type(current_e.get("type")) == form_element_type(desired_e.get("type")):
        return
    current_data_type = (current_e.get("concept") or {}).get("dataType")
    in_the_app = (
        selection_words(current_e.get("type"))
        if current_data_type in SELECTION_TYPE_DATA_TYPES
        else f"a {current_data_type} field"
    )
    result.selection_type_differences.append(
        f"{form_name} › {desired_e.get('name')} "
        f"({in_the_app} in the app, "
        f"{selection_words(desired_e.get('type'))} in the spreadsheets)"
    )


def _add_payload_from_element(elem: dict) -> dict:
    """Extract a field.add payload from a desired form-element dict."""
    concept = elem.get("concept", {}) or {}
    payload: dict = {
        "name": elem.get("name") or concept.get("name"),
        "dataType": concept.get("dataType", "Text"),
        "mandatory": bool(elem.get("mandatory", False)),
    }
    if form_element_type(elem.get("type")) == "MultiSelect":
        payload["selectionType"] = "MultiSelect"
    # keyValues → unit/min/max
    for kv in elem.get("keyValues", []):
        k, v = kv.get("key"), kv.get("value")
        if k == "unit":
            payload["unit"] = v
        elif k == "min":
            payload["min"] = v
        elif k == "max":
            payload["max"] = v
        elif k == SPREADSHEET_NAME_KEY:
            payload["spreadsheetName"] = v
    # Coded options come back from concept.answers
    answers = concept.get("answers") or []
    if concept.get("dataType") == "Coded" and answers:
        payload["options"] = [a.get("name") for a in answers if a.get("name")]
    return payload

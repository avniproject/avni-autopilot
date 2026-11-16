"""
Diff two bundle JSON snapshots and emit a list of EditOperation dicts that,
when applied to the current bundle, will reshape it to match the desired bundle.

Inputs (both bundle-shaped — see domain.bundle_editor.load_bundle_snapshot):
    {"forms":    [{"file_name": str, "content": <form_dict>}, ...],
     "concepts": [<concept_dict>, ...]}

Output:
    BundleDiff — EditOperation dicts ready for `apply_field_edits`, the
    fields whose single/multi select setting the update leaves as it is, and
    the spreadsheet rows it leaves out because their names clash.

Identity rule:
    Within a section, two form elements are the same field when their `uuid`
    matches. UUIDs come from the generator's deterministic seed
    (`formElement:<form>:<field>`), so the diff treats them as opaque match
    keys. A field moved to another page gets a UUID tied to that page
    (see bundle_editor) and keeps its concept, so on its page it also
    matches a live element of the same concept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from domain.generators import (
    SELECTION_TYPE_DATA_TYPES,
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
    """

    ops: list[dict] = field(default_factory=list)
    selection_type_differences: list[str] = field(default_factory=list)
    name_clashes: list[str] = field(default_factory=list)


def diff(desired_bundle: dict, current_bundle: dict) -> BundleDiff:
    """Produce EditOperation dicts that morph current_bundle → desired_bundle.

    Ops, in the order they run:
      - field.remove            (live in current, paired with no desired field,
                                 including every field of a page the desired form lacks)
      - section.remove          (live page the desired form lacks)
      - section.add             (desired page with no live page of its UUID)
      - field.add               (desired field with no live current field)
      - section.reorder_fields  (a page's fields would not end in the desired order)
      - form.reorder_sections   (the pages would not end in the desired order)

    Every removal runs before any add, so a field moved to another page is
    gone from its old page when it is added to the new one. Forms only on one
    side are logged; an update neither adds nor removes forms.
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
    page_pairs = _pair_pages(desired_groups, current_form.get("formElementGroups", []))
    paired_ids = {id(g) for g in page_pairs.values() if _is_live(g)}
    gone_pages = [
        g for g in current_form.get("formElementGroups", [])
        if _is_live(g) and id(g) not in paired_ids
    ]
    new_pages = [d for d in desired_groups if not _is_live(page_pairs.get(d.get("uuid")))]

    # A new page may be one the app has voided; its elements can be reinstated.
    pairings = [
        (d, page_pairs.get(d.get("uuid")) or {}, _pair_elements(d, page_pairs.get(d.get("uuid")) or {}))
        for d in desired_groups
    ]
    for d, current_section, pairs in pairings:
        if _is_live(current_section):
            _emit_removals(current_section, pairs, form_name, result, next_id)
    for page in gone_pages:
        _emit_removals(page, {}, form_name, result, next_id)
        result.ops.append(_op(next_id, "section.remove", _page_target(form_name, page)))
    for page in new_pages:
        result.ops.append(_op(next_id, "section.add", {"form": form_name},
                              {"name": page.get("name")}))
    for d, current_section, pairs in pairings:
        # The page the field ops address: the app's live page, else the one just added.
        page = current_section if _is_live(current_section) else d
        _diff_section(d, current_section, pairs, _page_target(form_name, page), result, next_id)

    kept = [
        d.get("uuid") for d in sorted(
            (d for d in desired_groups if _is_live(page_pairs.get(d.get("uuid")))),
            key=lambda d: page_pairs[d.get("uuid")].get("displayOrder") or 0,
        )
    ]
    desired_order = [d.get("uuid") for d in desired_groups]
    if kept + [p.get("uuid") for p in new_pages] != desired_order:
        result.ops.append(_op(next_id, "form.reorder_sections", {"form": form_name}, {"order": [
            (page_pairs[d.get("uuid")] if _is_live(page_pairs.get(d.get("uuid"))) else d).get("name")
            for d in desired_groups
        ]}))


def _pair_pages(desired_groups: list[dict], current_groups: list[dict]) -> dict[str, dict]:
    """Current page for each desired page UUID, where the form has one.

    A desired page pairs with the live page of its UUID; failing that, with an
    unpaired live page of the same name (as one added in the Avni app, with
    its own UUID); failing that, with the voided page of its UUID, to be
    brought back.
    """
    by_uuid = {g.get("uuid"): g for g in current_groups}
    pairs = {
        d.get("uuid"): by_uuid[d.get("uuid")]
        for d in desired_groups if _is_live(by_uuid.get(d.get("uuid")))
    }
    paired = {id(g) for g in pairs.values()}
    unpaired_by_name: dict[str, list[dict]] = {}
    for g in current_groups:
        if _is_live(g) and id(g) not in paired:
            unpaired_by_name.setdefault(name_key(g.get("name")), []).append(g)
    for d in desired_groups:
        if d.get("uuid") in pairs:
            continue
        same_name = unpaired_by_name.get(name_key(d.get("name")))
        if same_name:
            pairs[d.get("uuid")] = same_name.pop(0)
        elif d.get("uuid") in by_uuid:
            pairs[d.get("uuid")] = by_uuid[d.get("uuid")]
    return pairs


def _page_target(form_name: str, page: dict) -> dict:
    return {"form": form_name, "section": page.get("name"), "section_uuid": page.get("uuid")}


def _without_name_clashes(desired_form: dict, current_form: dict, form_name: str,
                          result: BundleDiff) -> list[dict]:
    """The desired pages, less each field whose name repeats another field of
    the form, ignoring capitals. Of each such set the one the app has live is
    kept, else the first."""
    live_in_app = {
        e["uuid"] for g in current_form.get("formElementGroups", [])
        for e in g.get("formElements", []) if _is_live(e)
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


def _op(next_id, kind: str, target: dict, payload: dict | None = None) -> dict:
    return {"op_id": next_id(), "kind": kind, "target": target, "payload": payload or {}}


# ── Per-section diff ──────────────────────────────────────────────────────────


def _concept_uuid(element: dict) -> str | None:
    return (element.get("concept") or {}).get("uuid")


def _pair_elements(desired_section: dict, current_section: dict) -> dict[str, dict]:
    """Current element for each desired element UUID, where the section has one.

    A desired element pairs with the current element of its UUID when that one
    is live; failing that, with an unpaired live element of the same concept,
    as after a field moved between pages gets a UUID tied to its new page; and
    failing that, with the voided element of its UUID, to be reinstated.
    """
    desired_elements = desired_section.get("formElements", [])
    current_elements = current_section.get("formElements", [])
    current_by_uuid = {e["uuid"]: e for e in current_elements}

    pairs: dict[str, dict] = {
        e["uuid"]: current_by_uuid[e["uuid"]]
        for e in desired_elements
        if _is_live(current_by_uuid.get(e["uuid"]))
    }
    desired_uuids = {e["uuid"] for e in desired_elements}
    unpaired_by_concept: dict[str | None, list[dict]] = {}
    for e in current_elements:
        if _is_live(e) and e["uuid"] not in desired_uuids:
            unpaired_by_concept.setdefault(_concept_uuid(e), []).append(e)

    for desired_e in desired_elements:
        if desired_e["uuid"] in pairs:
            continue
        same_concept = unpaired_by_concept.get(_concept_uuid(desired_e))
        if same_concept:
            pairs[desired_e["uuid"]] = same_concept.pop(0)
        elif desired_e["uuid"] in current_by_uuid:
            pairs[desired_e["uuid"]] = current_by_uuid[desired_e["uuid"]]
    return pairs


def _is_live(item: dict | None) -> bool:
    """True for a present, non-voided element or page; `{}` stands for none."""
    return bool(item) and not item.get("voided", False)


def _emit_removals(current_section: dict, pairs: dict[str, dict], form_name: str,
                   result: BundleDiff, next_id) -> None:
    """field.remove for each live current element no desired element pairs with.

    The target carries the element's UUID, as two live elements of a section
    can share a name.
    """
    paired_ids = {id(e) for e in pairs.values()}
    for e in current_section.get("formElements", []):
        if not _is_live(e) or id(e) in paired_ids:
            continue
        result.ops.append({
            "op_id": next_id(),
            "kind": "field.remove",
            "target": {**_page_target(form_name, current_section),
                       "field": e.get("name"), "field_uuid": e["uuid"]},
            "payload": {},
        })


def _diff_section(desired_section: dict, current_section: dict, pairs: dict[str, dict],
                  target: dict, result: BundleDiff, next_id) -> None:
    form_name = target["form"]
    ops = result.ops

    # Desired order is the order in which form elements appear in the desired
    # section's formElements list (the spec's intent for ordering).
    desired_elements = desired_section.get("formElements", [])
    desired_by_uuid = {e["uuid"]: e for e in desired_elements}
    desired_order = [e["uuid"] for e in desired_elements]

    # ── SELECTION SETTING: kept on every field current already has ────────
    for uuid in desired_order:
        if uuid in pairs:
            _note_selection_difference(desired_by_uuid[uuid], pairs[uuid], form_name, result)

    # ── ADD: in desired, with no live current element ─────────────────────
    for uuid in desired_order:
        current_e = pairs.get(uuid)
        if _is_live(current_e):
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

    # ── REORDER: added and reinstated fields go to the end of the page, so
    # the order after the adds is the surviving fields in their current order
    # followed by the added ones; reorder when that is not the desired order.
    position = {id(pairs[u]): u for u in desired_order if _is_live(pairs.get(u))}
    survivors = [
        position[id(e)]
        for e in sorted(current_section.get("formElements", []),
                        key=lambda x: x.get("displayOrder") or 0)
        if id(e) in position
    ]
    added = [u for u in desired_order if not _is_live(pairs.get(u))]
    if survivors + added != desired_order:
        ops.append({
            "op_id": next_id(),
            "kind": "section.reorder_fields",
            "target": dict(target),
            # A surviving field goes by its name in the app, which a rename
            # there can have changed; an added one by its spreadsheet name.
            "payload": {"order": [
                (pairs[u] if _is_live(pairs.get(u)) else desired_by_uuid[u]).get("name")
                for u in desired_order
            ]},
        })


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
    # Coded options come back from concept.answers
    answers = concept.get("answers") or []
    if concept.get("dataType") == "Coded" and answers:
        payload["options"] = [a.get("name") for a in answers if a.get("name")]
    return payload

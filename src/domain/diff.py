"""
Diff two bundle JSON snapshots and emit a list of EditOperation dicts that,
when applied to the current bundle, will reshape it to match the desired bundle.

Inputs (both bundle-shaped — see domain.bundle_editor.load_bundle_snapshot):
    {"forms":    [{"file_name": str, "content": <form_dict>}, ...],
     "concepts": [<concept_dict>, ...]}

Output:
    list[dict] — EditOperation dicts ready for `apply_field_edits`.

Identity rule:
    Two form elements are the same field iff their `uuid` matches. UUIDs
    come from the generator's deterministic seed (`formElement:<form>:<field>`),
    so the diff treats them as opaque match keys — no recomputation here.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def diff(desired_bundle: dict, current_bundle: dict) -> list[dict]:
    """Produce EditOperation dicts that morph current_bundle → desired_bundle.

    Only form-element-level changes are emitted today:
      - field.add               (UUID in desired, not in current; or voided in current)
      - field.remove            (UUID live in current, not in desired)
      - section.reorder_fields  (same UUIDs, different positions)

    Section/form mismatches are logged as warnings; no ops are emitted for them.
    """
    ops: list[dict] = []
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
    for form_uuid in desired_forms.keys() & current_forms.keys():
        desired_form = desired_forms[form_uuid]["content"]
        current_form = current_forms[form_uuid]["content"]
        form_name = desired_form.get("name")
        _diff_form(desired_form, current_form, form_name, ops, _next_id)

    return ops


# ── Per-form diff ─────────────────────────────────────────────────────────────


def _diff_form(desired_form: dict, current_form: dict, form_name: str,
               ops: list[dict], next_id) -> None:
    desired_sections = {g.get("uuid"): g for g in desired_form.get("formElementGroups", [])}
    current_sections = {g.get("uuid"): g for g in current_form.get("formElementGroups", [])}

    only_in_desired = set(desired_sections) - set(current_sections)
    only_in_current = set(current_sections) - set(desired_sections)
    if only_in_desired:
        log.warning("diff: form %r has %d new section(s) not yet supported: %s",
                    form_name, len(only_in_desired),
                    [desired_sections[u].get("name") for u in only_in_desired])
    if only_in_current:
        log.warning("diff: form %r has %d removed section(s) not yet supported: %s",
                    form_name, len(only_in_current),
                    [current_sections[u].get("name") for u in only_in_current])

    for sec_uuid in desired_sections.keys() & current_sections.keys():
        _diff_section(desired_sections[sec_uuid], current_sections[sec_uuid],
                      form_name, ops, next_id)


# ── Per-section diff ──────────────────────────────────────────────────────────


def _diff_section(desired_section: dict, current_section: dict, form_name: str,
                  ops: list[dict], next_id) -> None:
    section_name = desired_section.get("name")

    desired_elements = desired_section.get("formElements", [])
    current_elements = current_section.get("formElements", [])

    # Desired order is the order in which form elements appear in the desired
    # section's formElements list (the spec's intent for ordering).
    desired_by_uuid = {e["uuid"]: e for e in desired_elements}
    desired_order = [e["uuid"] for e in desired_elements]

    current_by_uuid = {e["uuid"]: e for e in current_elements}

    # ── REMOVE: live in current, not in desired ───────────────────────────
    for uuid in current_by_uuid:
        if uuid in desired_by_uuid:
            continue
        e = current_by_uuid[uuid]
        if e.get("voided", False):
            continue
        ops.append({
            "op_id": next_id(),
            "kind": "field.remove",
            "target": {"form": form_name, "section": section_name,
                       "field": e.get("name")},
            "payload": {},
        })

    # ── ADD: in desired, missing or voided in current ─────────────────────
    for uuid in desired_order:
        desired_e = desired_by_uuid[uuid]
        current_e = current_by_uuid.get(uuid)
        if current_e is not None and not current_e.get("voided", False):
            continue  # already live in current — no add needed
        ops.append({
            "op_id": next_id(),
            "kind": "field.add",
            "target": {"form": form_name, "section": section_name},
            "payload": _add_payload_from_element(desired_e),
        })

    # ── REORDER: every live element in desired must end up at its desired
    # position. We only emit a reorder if the relative order of fields that
    # exist in BOTH (and are/will-be live) differs from the desired order.
    surviving_uuids = [
        uuid for uuid in desired_order
        if uuid in current_by_uuid and not current_by_uuid[uuid].get("voided", False)
    ]
    # Compare desired sequence of survivors against their current ordering.
    if surviving_uuids:
        current_live_in_section = [
            e["uuid"] for e in sorted(current_elements, key=lambda x: x.get("displayOrder", 0))
            if not e.get("voided", False) and e["uuid"] in set(surviving_uuids)
        ]
        if current_live_in_section != surviving_uuids:
            # Use the desired full order (including newly-added) for the reorder
            # payload — the names come from desired so newly-added field names
            # are valid after the add ops above run first.
            reorder_names = [desired_by_uuid[u].get("name") for u in desired_order]
            ops.append({
                "op_id": next_id(),
                "kind": "section.reorder_fields",
                "target": {"form": form_name, "section": section_name},
                "payload": {"order": reorder_names},
            })


def _add_payload_from_element(elem: dict) -> dict:
    """Extract a field.add payload from a desired form-element dict."""
    concept = elem.get("concept", {}) or {}
    payload: dict = {
        "name": elem.get("name") or concept.get("name"),
        "dataType": concept.get("dataType", "Text"),
        "mandatory": bool(elem.get("mandatory", False)),
    }
    # keyValues → unit/min/max/selectionType
    for kv in elem.get("keyValues", []):
        k, v = kv.get("key"), kv.get("value")
        if k == "unit":
            payload["unit"] = v
        elif k == "min":
            payload["min"] = v
        elif k == "max":
            payload["max"] = v
        elif k == "multiSelect" and v:
            payload["selectionType"] = "MultiSelect"
    # Coded options come back from concept.answers
    answers = concept.get("answers") or []
    if concept.get("dataType") == "Coded" and answers:
        payload["options"] = [a.get("name") for a in answers if a.get("name")]
    return payload

"""
LangGraph state schema for the bundle-generation pipeline.

Owns the shape of state that flows through every node, plus the
`initial_state` factory. No graph wiring, no node implementations.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


Mode = Literal["generate", "edit_from_spec"]


class BundleState(TypedDict):
    org_name: str
    input_dir: str
    output_dir: str
    file_paths: list[str]
    entity_spec: Any                        # EntitySpec — populated by parse_documents
    parse_warnings: list[str]
    # Mode-driven branching (BUNDLE_EDIT_FROM_SPEC_SDD §3.2)
    mode: Mode                              # "generate" (default) | "edit_from_spec"
    bundle_path: str                        # only used in edit_from_spec mode
    diff_ops: list[dict]                    # computed by diff_against_bundle
    edit_result: dict                       # set by apply_diff_edits (an EditResult dump)
    # Form ↔ entity linking (FORM_ENTITY_LINKING_SDD)
    form_link_warnings: list[str]
    # Low-confidence / junk-on-form-like classifications awaiting HITL review.
    # Each entry is a card dict (see `domain.form_links.build_pending_cards`).
    pending_form_links: list[dict]
    # LLM enrichment
    user_instructions: str | None           # passed through from chat tool
    pending_changes: list[dict]             # surfaced via interrupt(); empty after resume
    applied_changes: list[dict]             # for the run summary + audit log
    enrich_warnings: list[str]
    # Generated JSON
    subject_types_json: list[dict]
    operational_subject_types_json: dict
    programs_json: list[dict]
    operational_programs_json: dict
    encounter_types_json: list[dict]
    operational_encounter_types_json: dict
    address_level_types_json: list[dict]
    organisation_config_json: dict
    forms_json: list[dict]                  # [{file_name, content}, ...]
    concepts_json: list[dict]
    mapping_specs: list[dict]               # intermediate metadata for mapping resolution
    form_mappings_json: list[dict]
    zip_path: str
    errors: list[str]


def initial_state(
    org_name: str,
    input_dir: str,
    output_dir: str,
    user_instructions: str | None = None,
    mode: Mode = "generate",
    bundle_path: str = "",
) -> BundleState:
    return {
        "org_name": org_name,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "file_paths": [],
        "entity_spec": None,
        "parse_warnings": [],
        "form_link_warnings": [],
        "pending_form_links": [],
        "mode": mode,
        "bundle_path": bundle_path,
        "diff_ops": [],
        "edit_result": {},
        "user_instructions": user_instructions,
        "pending_changes": [],
        "applied_changes": [],
        "enrich_warnings": [],
        "subject_types_json": [],
        "operational_subject_types_json": {},
        "programs_json": [],
        "operational_programs_json": {},
        "encounter_types_json": [],
        "operational_encounter_types_json": {},
        "address_level_types_json": [],
        "organisation_config_json": {},
        "forms_json": [],
        "concepts_json": [],
        "mapping_specs": [],
        "form_mappings_json": [],
        "zip_path": "",
        "errors": [],
    }

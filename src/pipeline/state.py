"""
LangGraph state schema for the bundle-generation pipeline.

Owns the shape of state that flows through every node, plus the
`initial_state` factory. No graph wiring, no node implementations.
"""

from __future__ import annotations

from typing import Any, TypedDict


class BundleState(TypedDict):
    org_name: str
    input_dir: str
    output_dir: str
    file_paths: list[str]
    entity_spec: Any                        # EntitySpec — populated by parse_documents
    parse_warnings: list[str]
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
) -> BundleState:
    return {
        "org_name": org_name,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "file_paths": [],
        "entity_spec": None,
        "parse_warnings": [],
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

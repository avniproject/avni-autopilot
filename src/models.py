"""
bundle_models.py — Pydantic request/response schemas for bundle and spec endpoints.

Used by:
  /generate-bundle, /validate-entities, /apply-entity-corrections,
  /generate-spec, /validate-spec, /spec-to-entities, /bundle-to-spec
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_log = logging.getLogger(__name__)


# ── Field-level ────────────────────────────────────────────────────────────────


class SkipLogicSpec(BaseModel):
    dependsOn: str
    condition: str = "="
    value: str = ""


class FieldSpec(BaseModel):
    name: str
    dataType: str = "Text"
    mandatory: bool = False
    readOnly: bool = False
    group: str | None = None
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    options: list[str] | None = None
    selectionType: str | None = None  # "SingleSelect" or "MultiSelect"
    skipLogic: SkipLogicSpec | None = None
    keyValues: dict[str, Any] | None = None
    isQuestionGroup: bool = False
    isRepeatable: bool = False  # Only meaningful when isQuestionGroup=True
    children: list["FieldSpec"] | None = None  # Child fields for QG/RQG
    # Composed natural-language intent driving `formElement.rule` generation —
    # visibility / value / validation / answer-filter fragments concatenated
    # by the parser. None when no behaviour columns were populated for this
    # field. See specs/FIELD_AND_PAGE_VISIBILITY_RULES_SDD.md §6.1.
    rule_intent: str | None = None


# ── Form-level ─────────────────────────────────────────────────────────────────


class SectionSpec(BaseModel):
    name: str = "Details"
    fields: list[FieldSpec] = Field(default_factory=list)


class FormSpec(BaseModel):
    name: str
    formType: str
    subjectType: str | None = None
    program: str | None = None
    encounterType: str | None = None
    sections: list[SectionSpec] = Field(default_factory=list)
    # Natural-language rule intents keyed by RuleKind value (e.g. "visitScheduleRule").
    # Populated from the modelling-doc Scheduling Rule column or via the chat tool.
    rule_intents: dict[str, str] = Field(default_factory=dict)


# ── Entity-level ───────────────────────────────────────────────────────────────


class AddressLevelSpec(BaseModel):
    name: str
    level: int = 1
    parent: str | None = None


class SubjectTypeSpec(BaseModel):
    name: str
    type: str = "Person"
    allowProfilePicture: bool = False
    uniqueName: bool = False
    lastNameOptional: bool = True
    # Raw value of the "Registration Form" column from the Subject Types tab.
    # Consumed by `link_forms_to_entities` as a hint when matching the subject
    # to a sheet in the forms xlsx.
    registration_form_source: str | None = None


class ProgramSpec(BaseModel):
    name: str
    target_subject_type: str = ""
    colour: str = "#4A148C"
    allow_multiple_enrolments: bool = False
    # Raw values from the Program tab's "Enrolment Form" / "Exit Form" columns.
    enrolment_form_source: str | None = None
    exit_form_source: str | None = None


class EncounterTypeSpec(BaseModel):
    name: str
    program_name: str = ""
    subject_type: str = ""
    is_program_encounter: bool = False
    is_scheduled: bool = True
    # Raw values from the source row's form-reference columns. Same field on
    # both Program Encounter rows and standalone Encounter rows (the two tabs
    # use the same column semantics).
    form_source: str | None = None
    cancellation_form_source: str | None = None

    @field_validator("program_name", mode="before")
    @classmethod
    def coerce_none_to_empty(cls, v: object) -> str:
        return "" if v is None else v


class GroupSpec(BaseModel):
    name: str
    has_all_privileges: bool = False


# ── Top-level entity bundle ────────────────────────────────────────────────────


class EntitySpec(BaseModel):
    subject_types: list[SubjectTypeSpec] = Field(default_factory=list)
    programs: list[ProgramSpec] = Field(default_factory=list)
    encounter_types: list[EncounterTypeSpec] = Field(default_factory=list)
    address_levels: list[AddressLevelSpec] = Field(default_factory=list)
    groups: list[GroupSpec] = Field(default_factory=list)
    forms: list[FormSpec] = Field(default_factory=list)

    # Cross-ref issues stored as warnings instead of raising ValueError.
    # Populated by the validator; downstream code can inspect these.
    validation_warnings: list[str] = Field(default_factory=list, exclude=True)

    # Rules-tab rows whose form name did not exactly match any form.
    # `{row_name: {rule_field: intent}}` — resolved by an LLM pass in the
    # form-link pipeline node, which tolerates typos and naming variants.
    unmatched_rule_intents: dict[str, dict[str, str]] = Field(
        default_factory=dict, exclude=True,
    )

    # Set to True to raise on cross-ref errors (strict mode).
    # Default False = lenient mode (store as warnings).
    _strict_validation: bool = False

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def create_strict(cls, **kwargs) -> "EntitySpec":
        """Create an EntitySpec that raises on cross-ref errors."""
        spec = cls(**kwargs)
        spec._strict_validation = True
        spec._run_cross_ref_checks()
        return spec

    @model_validator(mode="after")
    def validate_no_duplicates_and_cross_refs(self) -> "EntitySpec":
        self._run_cross_ref_checks()
        return self

    def _run_cross_ref_checks(self) -> None:
        errors: list[str] = []

        # ── Duplicate checks ──────────────────────────────────────────────────
        def _check_dupes(items: list, label: str) -> set[str]:
            seen: set[str] = set()
            for item in items:
                name = getattr(item, "name", "").strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    errors.append(f"Duplicate {label}: '{name}'")
                seen.add(key)
            return seen

        st_names = _check_dupes(self.subject_types, "subject_type")
        prog_names = _check_dupes(self.programs, "program")
        _check_dupes(self.encounter_types, "encounter_type")
        _check_dupes(self.address_levels, "address_level")

        # ── Cross-reference checks ────────────────────────────────────────────
        for prog in self.programs:
            target = (prog.target_subject_type or "").strip()
            if target and target.lower() not in st_names:
                errors.append(
                    f"Program '{prog.name}' references unknown subject_type '{target}'"
                )

        for enc in self.encounter_types:
            prog_ref = (enc.program_name or "").strip()
            st_ref = (enc.subject_type or "").strip()
            if prog_ref and prog_ref.lower() not in prog_names:
                errors.append(
                    f"EncounterType '{enc.name}' references unknown program '{prog_ref}'"
                )
            if st_ref and st_ref.lower() not in st_names:
                errors.append(
                    f"EncounterType '{enc.name}' references unknown subject_type '{st_ref}'"
                )

        if errors:
            if self._strict_validation:
                raise ValueError(
                    "EntitySpec validation failed:\n"
                    + "\n".join(f"  - {e}" for e in errors)
                )
            self.validation_warnings = errors

    def to_entities_dict(self) -> dict:
        """Return a plain dict compatible with AppConfiguratorFlow.state.entities_jsonl."""
        return {
            "subject_types": [st.model_dump() for st in self.subject_types],
            "programs": [p.model_dump() for p in self.programs],
            "encounter_types": [e.model_dump() for e in self.encounter_types],
            "address_levels": [a.model_dump() for a in self.address_levels],
            "groups": [g.model_dump() for g in self.groups],
            "forms": [f.model_dump() for f in self.forms],
        }


# ── Request bodies ─────────────────────────────────────────────────────────────


class GenerateBundleRequest(BaseModel):
    entities: dict[str, Any]
    org_name: str = ""


class ValidateEntitiesRequest(BaseModel):
    entities: dict[str, Any]


class ApplyEntityCorrectionsRequest(BaseModel):
    entities: dict[str, Any]
    corrections: list[dict[str, Any]] = Field(default_factory=list)


class GenerateSpecRequest(BaseModel):
    entities: dict[str, Any]
    org_name: str = ""


class ValidateSpecRequest(BaseModel):
    spec_yaml: str


class SpecToEntitiesRequest(BaseModel):
    spec_yaml: str


class BundleToSpecRequest(BaseModel):
    bundle: dict[str, Any]
    org_name: str = ""


# ── Response shapes ────────────────────────────────────────────────────────────


class ValidationIssue(BaseModel):
    severity: str
    entity_type: str
    message: str


class ValidateEntitiesResponse(BaseModel):
    entities: dict[str, Any]
    issues: list[ValidationIssue]
    error_count: int
    warning_count: int
    issues_summary: str
    has_errors: bool
    has_warnings: bool


class ValidateSpecResponse(BaseModel):
    valid: bool
    errors: list[str]
    warnings: list[str]
    suggestions: list[str]


# ── LLM enrichment types (SDD §4.2, §3) ──────────────────────────────────────


ChangeKind = Literal[
    "long_name",
    "duplicate_field",
    "conflicting_concept",
]


class Change(BaseModel):
    """One refinement Claude proposes vs. the deterministic parser's output.

    Every Change is presented to the user for confirmation before being
    applied — there is no auto-apply path right now.
    """
    change_id: str
    form: str
    field: str = ""
    kind: ChangeKind
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


# ── Bundle field editing (BUNDLE_EDITING_SDD) ────────────────────────────────


EditOpKind = Literal[
    "field.add",
    "field.rename",
    "field.remove",
    "section.reorder_fields",
]

RejectionKind = Literal[
    "not_found",
    "ambiguous_target",
    "duplicate_name",
    "schema",
    "integrity",
]


class EditOperation(BaseModel):
    """One field-level edit applied to an already-generated bundle."""
    op_id: str
    kind: EditOpKind
    target: dict[str, str] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class RejectedOp(BaseModel):
    op_id: str
    kind: RejectionKind
    reason: str


class EditResult(BaseModel):
    bundle_path: str
    applied: list[str] = Field(default_factory=list)
    rejected: list[RejectedOp] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    forms_modified: list[str] = Field(default_factory=list)
    form_elements_added: int = 0
    form_elements_reinstated: int = 0
    form_elements_voided: int = 0
    form_elements_renamed: int = 0
    concepts_appended: int = 0

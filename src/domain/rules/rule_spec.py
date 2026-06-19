"""Pydantic models for rule generation.

`RuleKind` identifies which Avni rule field a generated JS function is written
to. `RuleSpec` carries the natural-language intent plus bundle-grounded context
into the generator. `RuleResult` carries the generated JS and grounding notes
back out. See specs/VISIT_SCHEDULE_RULE_SDD.md §5.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class RuleKind(str, Enum):
    """The Avni rule field a generated JS function targets.

    Each kind maps to a target JSON field on the form JSON, an entity-param
    the JS receives via `params.entity`, and an expected return type.
    Per-kind metadata (return-type description, return-expression hint,
    return-shape validator) lives in `prompts.py` and `validator.py`.
    See specs/FORM_LEVEL_RULES_SDD.md.
    """

    VISIT_SCHEDULE = "visitScheduleRule"
    VALIDATION = "validationRule"
    EDIT_FORM = "editFormRule"
    DECISION = "decisionRule"
    FORM_ELEMENT = "formElementRule"


class RuleSpec(BaseModel):
    """Inputs to a single rule-generation call.

    Every name under `available_*` is grounded in `EntitySpec` so the generator
    can constrain Claude's vocabulary to symbols that actually exist in this
    bundle. The non-`available_*` fields locate the form being generated for.
    """

    rule_kind: RuleKind
    intent: str
    form_name: str
    form_type: str
    subject_type: str | None = None
    program: str | None = None
    encounter_type: str | None = None
    available_concepts: list[str] = Field(default_factory=list)
    available_encounter_types: list[str] = Field(default_factory=list)
    available_programs: list[str] = Field(default_factory=list)
    # Coded-concept answer allowlist, keyed by concept name and merged across
    # every form a rule on this form can read from at runtime (target form +
    # registration + enrolment when applicable). Empty when no coded fields
    # are in scope. See CONCEPT_ANSWER_GROUNDING_SDD.md §5.
    concept_answers: dict[str, list[str]] = Field(default_factory=dict)


class RuleResult(BaseModel):
    """Output from a single rule-generation call.

    `js` is empty when the validator rejected the model's output. `confidence`
    is telemetry only — it does not gate writes; the validator does. See SDD
    §7.2 and §7.3.
    """

    rule_kind: RuleKind
    js: str = ""
    confidence: Literal["high", "medium", "low"] = "low"
    used_helpers: list[str] = Field(default_factory=list)
    referenced_concepts: list[str] = Field(default_factory=list)
    referenced_encounter_types: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

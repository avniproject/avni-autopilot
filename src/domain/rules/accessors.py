"""Canonical method-name catalogs shared across the rules package.

`CONCEPT_ACCESSORS` lists every avni-models method whose first string-literal
argument is a concept name. `ENCOUNTER_TYPE_ACCESSORS` is the equivalent for
encounter-type names.

Consumers:
- `validator.py` grounds generated JS against the bundle by walking these sets.
- `kb_cli.py` builds its concept-extraction regex from `CONCEPT_ACCESSORS` via
  `concept_accessor_call_regex()`.

Add new entries here when avni-models exposes a new accessor — both consumers
pick it up via the shared catalog.
"""

from __future__ import annotations

import re

CONCEPT_ACCESSORS: frozenset[str] = frozenset({
    "getObservationReadableValue",
    "getObservationReadableValueInEntireEnrolment",
    "findCancelEncounterObservationReadableValue",
    "findObservationInLastEncounter",
    "findLatestObservationInEntireEnrolment",
    "findLatestObservationFromPreviousEncounters",
    "findLatestPreviousEncounterWithValueForConcept",
    "findLatestPreviousEncounterWithObservationForConcept",
    "findObservationValueInEntireEnrolment",
    "findObservationAcrossAllEnrolments",
    "getObservationValue",
    "getObservationsForConceptName",
})


ENCOUNTER_TYPE_ACCESSORS: frozenset[str] = frozenset({
    "hasEncounter",
    "hasEncounterOfType",
    "hasProgramEncounterOfType",
    "hasCompletedEncounterOfType",
    "getEncountersOfType",
    "numberOfEncountersOfType",
    "scheduledEncountersOfType",
    "findEncounter",
})


def concept_accessor_call_regex() -> re.Pattern[str]:
    """Build a regex matching `.<accessor>(...'<concept>'...)` call sites.

    Names are sorted longest-first so prefix collisions (e.g.
    `getObservationValue` vs `getObservationValueInEntireEnrolment`) resolve
    to the longer match without relying on regex backtracking.
    """
    alternation = "|".join(sorted(CONCEPT_ACCESSORS, key=len, reverse=True))
    return re.compile(rf"""\.(?:{alternation})\(\s*['"]([^'"]+)['"]""")

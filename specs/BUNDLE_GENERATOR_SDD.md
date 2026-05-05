# Bundle Generator — Software Design Document

## 1. Objective

Accept a single org name as input, parse all modelling and scoping documents from `resources/input/<org>/`, and produce a bundle ZIP at `resources/output/<org>/<Org>.zip`.

Each ZIP contains exactly:
```
addressLevelTypes.json
subjectTypes.json
operationalSubjectTypes.json
encounterTypes.json
operationalEncounterTypes.json
programs.json
operationalPrograms.json
concepts.json
forms/<FormName>_<uuid>.json   (one file per form, incl. cancellation forms)
formMappings.json
organisationConfig.json
```

All rule fields (`validationRule`, `visitScheduleRule`, `encounterEligibilityCheckRule`, `enrolmentEligibilityCheckRule`, etc.) are set to empty strings.

---

## 2. Input Structure

```
resources/input/
  srijan/
    SRIJAN Modelling - CONSISTENT (1).xlsx   ← modelling doc (entities)
    SRIJAN Forms (1).xlsx                    ← scoping doc (form fields)
  astitva/
    Astitva Modelling (1).xlsx
    Astitva Nourish Program Forms (1).xlsx
  gubbachi/
    Gubbachi Avni Modelling.xlsx
    Gubbachi Program Scoping Document 01Dec.xlsx
```

All `.xlsx` files in an org folder are passed together to the parser. The parser auto-classifies each sheet by content — no assumption about file or sheet names.

---

## 3. Architecture

### 3.1 Parsing layer (reused from avni-ai)

`parse_scoping_docs(file_paths)` from `avni-ai/src/bundle/scoping_parser.py` is the parsing backbone. It:

- Loads every sheet from every file
- Auto-classifies each sheet into: `subject_type | program | encounter | program_encounter | form | location | w3h | unknown`
- Deduplicates across files (same entity in modelling doc and scoping doc → merged)
- Returns an `EntitySpec` (Pydantic model) containing parsed subjects, programs, encounter types, forms, and address levels
- Already implements fuzzy matching for cross-references (e.g. a program's `target_subject_type` is matched against known subject type names even with typos or trailing spaces)

This parser handles `Beneficiary Registration → Beneficiary` name-stripping, `CSA` abbreviation matching, and subject type resolution from program names automatically.

### 3.2 Generation layer (new — this project)

A **LangGraph pipeline** (`bundle_generator.py`) takes one org at a time and runs these nodes sequentially:

```
discover_files → parse_documents → generate_entities → generate_forms → generate_form_mappings → package_zip
```

### 3.3 Runner script (`generate_bundles.py`)

Accepts a single org name as a CLI argument (`--org srijan`), resolves the input/output paths, invokes the LangGraph pipeline once, and prints a summary.

---

## 4. LangGraph Pipeline

### State

```python
class BundleState(TypedDict):
    org_name: str                   # "srijan", "astitva", "gubbachi"
    input_dir: str                  # path to resources/input/<org>/
    output_dir: str                 # path to resources/output/<org>/
    file_paths: list[str]           # all .xlsx files found in input_dir
    entity_spec: EntitySpec | None  # parsed output from parse_scoping_docs
    parse_warnings: list[str]
    # Generated JSON content
    subject_types_json: list[dict]
    operational_subject_types_json: dict
    programs_json: list[dict]
    operational_programs_json: dict
    encounter_types_json: list[dict]
    operational_encounter_types_json: dict
    forms_json: list[dict]                    # {file_name, content}
    concepts_json: list[dict]
    form_mappings_json: list[dict]
    address_level_types_json: list[dict]
    organisation_config_json: dict
    # Output
    zip_path: str
    errors: list[str]
```

### Node 1 — `discover_files`

Scans `input_dir` for all `.xlsx` files. Aborts (→ END) if no files found.

### Node 2 — `parse_documents`

Calls `parse_scoping_docs(state.file_paths)` from avni-ai.  
Stores the resulting `EntitySpec` and any misc/unclassified sheet warnings.

No custom parsing logic lives here — the avni-ai parser owns sheet classification, fuzzy cross-ref resolution, subject name stripping, and form field extraction.

### Node 3 — `generate_entities`

Generates all entity-level JSON: subject types, programs, encounter types, their operational wrappers, address level types, and organisation config.

Converts `EntitySpec` → JSON dicts using **deterministic UUIDs** (`uuid5` seeded by `"subjectType:<name>"`, `"program:<name>"`, etc.). This ensures re-runs produce identical UUIDs and are idempotent on upload.

**subjectTypes.json** (array):
```json
{
  "name": "Farmer",
  "uuid": "<deterministic-uuid>",
  "active": true,
  "type": "Person",            // from SubjectTypeSpec.type
  "subjectSummaryRule": "",
  "programEligibilityCheckRule": "",
  "allowEmptyLocation": false,
  "allowMiddleName": false,
  "lastNameOptional": true,    // default from SubjectTypeSpec
  "allowProfilePicture": false,
  "uniqueName": false,
  "shouldSyncByLocation": true,
  "settings": {"displayRegistrationDetails": true, "displayPlannedEncounters": true},
  "household": false,          // true only for Household type
  "group": false,
  "directlyAssignable": false,
  "voided": false
}
```

**programs.json** (array):
```json
{
  "name": "CSA",
  "uuid": "<deterministic-uuid>",
  "colour": "#4A148C",
  "voided": false,
  "active": true,
  "enrolmentEligibilityCheckRule": "",
  "enrolmentSummaryRule": "",
  "manualEligibilityCheckRequired": false,
  "allowMultipleEnrolments": false
}
```

**encounterTypes.json** (array):
```json
{
  "name": "Lead Farmer Monitoring",
  "uuid": "<deterministic-uuid>",
  "encounterEligibilityCheckRule": "",
  "active": true,
  "immutable": false
}
```

Operational wrappers (`operationalSubjectTypes`, `operationalPrograms`, `operationalEncounterTypes`) follow the same pattern — a wrapper object containing an array, each entry referencing its parent UUID.

**addressLevelTypes.json** (array) — built from `EntitySpec.address_levels`. Each level is linked to its parent by UUID. If no location hierarchy was found in the input docs the parser defaults to `State → District → Village`.

```json
[
  { "uuid": "<deterministic-uuid>", "name": "State",    "level": 3 },
  { "uuid": "<deterministic-uuid>", "name": "District", "level": 2, "parent": { "uuid": "<state-uuid>" } },
  { "uuid": "<deterministic-uuid>", "name": "Village",  "level": 1, "parent": { "uuid": "<district-uuid>" } }
]
```

**organisationConfig.json** (object) — a minimal stub; no org-specific settings are parsed from the input docs at this stage.

```json
{
  "uuid": "<deterministic-uuid: orgConfig:<org_name>>",
  "settings": {
    "languages": ["en"],
    "customRegistrationLocations": [],
    "searchFilters": [],
    "myDashboardFilters": []
  },
  "worklistUpdationRule": ""
}
```

### Node 4 — `generate_forms`

Iterates over `EntitySpec.forms` (each a `FormSpec`). Also collects all concepts referenced by form fields into `concepts_json` — every unique field name across all forms becomes a concept entry.

For each `FormSpec`:

1. **Main form**: produce `forms/<FormName>_<uuid>.json` with `formElementGroups` populated from `FormSpec.sections` → `FormSpec.fields`. Each field maps to a `formElement` with a concept (name + `uuid5("concept:<fieldName>")` + dataType + answers). All rule fields on formElements are empty strings.

2. **Cancellation form** (auto-generated for `ProgramEncounter` and `Encounter` form types):
   - Name: `<FormName> Encounter Cancellation` (ProgramEncounter) or `<FormName> Cancellation` (Encounter)
   - formType: `ProgramEncounterCancellation` / `IndividualEncounterCancellation`
   - Contains a single section with one coded reason-for-cancellation field

File naming: `<safe_form_name>_<uuid>.json` where safe_name replaces `/` with `_`.

**concepts.json** (array) — one entry per unique field name encountered across all forms. Coded concepts include their answer list; all other types have an empty answers array. Deduplication is by concept name (case-insensitive).

```json
[
  {
    "name": "Marital Status",
    "uuid": "<deterministic-uuid: concept:Marital Status>",
    "dataType": "Coded",
    "active": true,
    "answers": [
      { "name": "Married",   "uuid": "<deterministic-uuid: concept:Married>",   "order": 0, "active": true },
      { "name": "Unmarried", "uuid": "<deterministic-uuid: concept:Unmarried>", "order": 1, "active": true }
    ]
  },
  {
    "name": "Date of Birth",
    "uuid": "<deterministic-uuid: concept:Date of Birth>",
    "dataType": "Date",
    "active": true,
    "answers": []
  }
]
```

**Form JSON shape**:
```json
{
  "name": "Lead Farmer Monitoring",
  "uuid": "<deterministic-uuid>",
  "formType": "ProgramEncounter",
  "formElementGroups": [
    {
      "uuid": "<deterministic-uuid>",
      "name": "Section Name",
      "displayOrder": 1,
      "formElements": [
        {
          "uuid": "<deterministic-uuid>",
          "name": "Field Name",
          "displayOrder": 1,
          "mandatory": false,
          "keyValues": [],
          "validationDeclarativeRule": "",
          "rule": "",
          "concept": {
            "name": "Field Name",
            "uuid": "<deterministic-uuid>",
            "dataType": "Text",
            "answers": []
          }
        }
      ]
    }
  ]
}
```

### Node 5 — `generate_form_mappings`

Builds `formMappings.json` (flat array) by resolving each `FormSpec`'s references:

| formType | Keys set |
|---|---|
| `IndividualProfile` | `subjectTypeUUID` |
| `ProgramEnrolment` / `ProgramExit` | `subjectTypeUUID`, `programUUID` |
| `ProgramEncounter` / `ProgramEncounterCancellation` | `subjectTypeUUID`, `programUUID`, `encounterTypeUUID` |
| `Encounter` / `IndividualEncounterCancellation` | `subjectTypeUUID`, `encounterTypeUUID` |

Fuzzy matching (same `_fuzzy_match` from scoping_parser) is used to resolve `FormSpec.subjectType` → subject type UUID and `FormSpec.program` → program UUID when the names don't match exactly (already resolved by `_resolve_form_subject_types` in the parser — this node only looks up UUIDs).

**Entry shape**:
```json
{
  "uuid": "<deterministic-uuid>",
  "formUUID": "<form-uuid>",
  "formType": "ProgramEncounter",
  "formName": "Lead Farmer Monitoring",
  "subjectTypeUUID": "<st-uuid>",
  "programUUID": "<prog-uuid>",
  "encounterTypeUUID": "<et-uuid>",
  "enableApproval": false
}
```

### Node 6 — `package_zip`

Writes all files to `output_dir` and packages them into `<output_dir>/<OrgName>.zip` following the canonical ZIP ordering (mirrors `BundleService.java` and `zip_bundle.js`):

```
addressLevelTypes.json
subjectTypes.json
operationalSubjectTypes.json
encounterTypes.json
operationalEncounterTypes.json
programs.json
operationalPrograms.json
concepts.json
forms/<name>_<uuid>.json   (sorted alphabetically)
formMappings.json
organisationConfig.json
```

---

## 5. Deterministic UUIDs

All UUIDs are generated via UUID v5 seeded with a namespaced string:

| Entity | Seed |
|---|---|
| Subject type | `subjectType:<name>` |
| Operational subject type | `operationalSubjectType:<name>` |
| Program | `program:<name>` |
| Operational program | `operationalProgram:<name>` |
| Encounter type | `encounterType:<name>` |
| Operational encounter type | `operationalEncounterType:<name>` |
| Form | `form:<name>` |
| Cancellation form | `form:<name> Encounter Cancellation` / `form:<name> Cancellation` |
| Form element group | `formGroup:<formName>:<sectionName>` |
| Form element | `formElement:<formName>:<fieldName>` |
| Concept | `concept:<fieldName>` |
| Form mapping | `mapping:<formName>` |

This guarantees identical ZIPs on every run (idempotent re-uploads).

---

## 6. Fuzzy Matching

All fuzzy matching is delegated to `_fuzzy_match()` in `scoping_parser.py`. It runs four passes in order — exact → substring → word Jaccard → character overlap — with a default threshold of 0.5. It is used:

- When resolving a program's `target_subject_type` against known subject type names
- When resolving an encounter type's `subject_type` or `program_name`
- When matching a form sheet name to a W3H entry
- When resolving `FormSpec.subjectType / .program` in `_resolve_form_subject_types`

No additional fuzzy matching is needed in the generation layer — by the time `EntitySpec` is returned, all cross-references are already resolved.

---

## 7. Runner Script (`generate_bundles.py`)

Accepts a single `--org` argument. Resolves input/output paths and invokes the pipeline once.

```
Usage:
  python generate_bundles.py --org srijan
  python generate_bundles.py --org astitva
  python generate_bundles.py --org gubbachi

Paths resolved:
  input_dir  = resources/input/<org>/
  output_dir = resources/output/<org>/
```

Output:
```
Org: srijan
STs: 1  |  Programs: 2  |  Encounter types: 9  |  Forms: 22  |  Concepts: 84
Bundle: resources/output/srijan/Srijan.zip
```

If `resources/input/<org>/` does not exist or contains no `.xlsx` files, the script exits with a clear error message.

---

## 8. Files to Create

| File | Description |
|---|---|
| `bundle_generator.py` | LangGraph pipeline (replaces current draft) |
| `generate_bundles.py` | Runner: accepts `--org`, invokes pipeline once |

The existing `subject_type_parser.py` remains for focused subject-type-only use but is superseded by `bundle_generator.py` for full bundle generation.

Dependencies already declared in `pyproject.toml`: `langgraph`, `openpyxl`, `anthropic`, `httpx`.  
New dependency to add: `pandas` (required by `parse_scoping_docs`), `pydantic` (required by `bundle_models`).

---

## 9. Out of Scope (for now)

- Groups, reports, dashboards, menu items — not in required output
- Rule generation (`validationRule`, `visitScheduleRule`) — empty strings for now
- Skip logic translation to Avni declarative rule format — deferred
- Upload to Avni server — handled separately by `avni_bundle_agent.py`

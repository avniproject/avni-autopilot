# /curate-rules — dedup rules + add intent prompts in the rule spreadsheet

Source: `requirements/Self-service improvement.xlsx`, tab `List to automate`. Columns today: `ORG name`, `Form name`, `Rule`.

Goal: produce a curated tab `List to automate (curated)` in the **same workbook** with one row per **distinct rule pattern** and a one-sentence natural-language `Prompt` for each kept rule. This curated tab becomes the few-shot example corpus consumed by visit-schedule rule generation (see `specs/VISIT_SCHEDULE_RULE_SDD.md` §6.2).

## Workflow

### 1. Read

Open the workbook with `openpyxl`, load the `List to automate` tab, read every non-empty row.

### 2. Cluster by pattern — not by org or form name

Two rules belong to the same cluster when they demonstrate the **same idiom**. Look at:

- The entity-param shape (`individual` / `programEnrolment` / `programEncounter` / `encounter`).
- The date-arithmetic pattern (next non-Sunday, fixed Feb/Aug slots, N days after current encounter, end of month, …).
- Whether filtering / conditioning on observations is involved (`RuleCondition`, `getObservationReadableValue`).
- Cancellation handling (`findCancelEncounterObservationReadableValue`).
- Use of `uniqueByType` or sibling-encounter de-duplication.

Rules from different orgs/forms can belong to the same cluster if the idiom is the same.

### 3. Pick a representative per cluster

Prefer the rule that is clean:
- Uses canonical helpers (e.g. `getRealEventDate` over the inline `encounter.earliestVisitDateTime || encounter.encounterDateTime` idiom).
- Has no obvious dead code, stale comments, or `//SAMPLE RULE EXAMPLE` markers.
- Is short and easy to read.

### 4. Confirm clusters with the user

Print a table:

| # | Cluster summary | Representative (org / form) | Other members | Why grouped |

Wait for the user to accept the picks, or to ask for a swap / split / merge. Don't move on without confirmation.

### 5. Write intent prompts

For each surviving rule (one per confirmed cluster), write a one-sentence `Prompt` in the voice a user would naturally type — concrete and behavioural, not jargon-y:

- ✅ "schedule daily attendance every weekday from the registration date"
- ✅ "schedule albendazole and growth monitoring at the start of each Feb and Aug after enrolment"
- ❌ "use VisitScheduleBuilder with moment.add to compute earliestDate" (describes the implementation, not the intent)

Show all intents in a numbered list. Wait for the user to accept or edit before moving on.

### 6. Write the curated tab

Use `openpyxl` to write a new tab `List to automate (curated)` in the **same workbook** with columns:

| ORG name | Form name | Rule | Prompt |

- **Do not modify the `List to automate` source tab.**
- Preserve cell wrapping / text formatting where reasonable.
- If `List to automate (curated)` already exists: ask before overwriting; offer a numbered variant `List to automate (curated 2)` as an option.

### 7. Report

Print a short summary:

- Clusters found: N
- Rules kept: K (one per cluster)
- Rules dropped as duplicates: D
- Sample of the first 3 prompts.

Suggest the user `git diff` the xlsx before committing (the file is git-tracked).

## Constraints

- Never modify the `List to automate` tab in place — always write to a new tab.
- Don't invent rules or modify rule JS during curation. Picking a representative ≠ rewriting it.
- If a rule looks broken or doesn't compile mentally, flag it in the report but don't drop it silently.

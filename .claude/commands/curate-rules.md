# /curate-rules — dedup rules + add intent prompts in the rule spreadsheet

Source: `requirements/rules_ai_automation.xlsx`. One raw tab per rule kind: `VS rule`, `Validation rule`, `Edit form rule`, `Decision rule`. Columns today: `ORG name`, `Form name` (often with `form_type` between them on raw tabs), `Rule`.

Goal: produce a paired curated tab `<kind> (curated)` in the **same workbook** with one row per **distinct rule pattern** and a one-sentence natural-language `Prompt` for each kept rule. The curated tab becomes the few-shot example corpus consumed by rule generation (see `specs/VISIT_SCHEDULE_RULE_SDD.md` §6.2 and `specs/FORM_LEVEL_RULES_SDD.md`).

## Workflow

### 1. Read

Open the workbook with `openpyxl`, load the rule-kind raw tab (`VS rule` / `Validation rule` / `Edit form rule` / `Decision rule`), and read every non-empty row.

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

**Prompt-writing rules.** The prompt is what the retrieval system embeds; the rule body is what Claude imitates *after* retrieval. So the prompt must match the level of abstraction at which **users describe what they want**, not the level of detail in the rule code. See `specs/VISIT_SCHEDULE_RULE_SDD.md` §6.3 for the retrieval contract.

1. **Match the typical user query.** Users say *"block double entry"*, not *"block when `individual.getEncounters().filter(...).length > 1`"*. Embeddings score the first against natural language; the second competes only with itself.

2. **Use generic placeholders, not specific identifiers.** *"a cutoff year"* not *"2020"*; *"an observation value"* not *"the Weight observation"*; *"the previous completed visit"* not *"`lastFulfilledEncounter()`"*. Specific identifiers narrow the retrieval surface to queries that repeat them verbatim.

3. **One concrete "e.g. …" grounding is OK.** Anchors a generic statement when the abstract form alone is too vague. *"block saving when an observation value falls outside the allowed numeric range (e.g. weight 24–90 kg)"* matches both generic out-of-range queries and weight-specific ones.

4. **Exclude implementation details.** No helper / method names (`getRealEventDate`, `RuleCondition`, `complicationsBuilder`); no JS verbs (`push`, `return`); no internal types (`UUID`, `keyValues`). These belong in the JS body, not the prompt.

5. **State the shape — verb + condition.** *"block X when Y"*, *"schedule X when Y"*, *"set X to Y based on Z"*, *"compute X from Y and Z"*. Verb-led prompts retrieve more predictably than noun-led ones.

6. **One sentence, ≤ 25 words.** Multi-sentence prompts dilute the embedding signal.

7. **For single-member clusters, read the JS before writing the prompt.** The cluster summary an agent produces is a paraphrase; the JS is ground truth. They sometimes disagree (a date-ordering guard summarised as "approval lock"; an incomplete rule summarised as "value-change validator"). Trust the JS, not the summary.

8. **For multi-member clusters, write at the cluster's level of abstraction**, not any specific member's. The prompt should be true of every member; the representative's JS body shows one concrete instantiation.

9. **For placeholder / broken / no-op rules**, prefer to **drop the cluster entirely** over describing it. Teaching the LLM a no-op pattern is rarely useful. If a cluster is kept because it carries a documented placeholder role, label the prompt `(placeholder)` so the embedding signal degrades gracefully.

10. **Kind-specific verbs.** Different Avni rule kinds answer different questions, so they take different natural verbs. Match the verb to the kind:

    | Rule kind | What it does | Natural verbs | Example prompt |
    |---|---|---|---|
    | **Visit Schedule** | Schedules future encounters from current data | *schedule, plan, set next* | "schedule the next follow-up 10 days after the encounter date when risk is high" |
    | **Validation** | Blocks a save when conditions aren't met (data consistency, consent, duplicates, date ordering, …) | *block, prevent, reject* | "block saving when the visit date is before the scheduled date" |
    | **Edit Form** | Controls **when**, **by whom**, and **in what form-state** a saved encounter can be edited (role / user-group, time window from encounter date, approval state, "Reviewed" lock, …) | *allow editing, block editing, restrict editing* | "allow editing only by the assigned role within N days of the encounter date" |
    | **Decision** | Derives conclusions / recommendations and displays them on the form's last page (also saved as observations) | *show, derive, classify, conclude, calculate and display* | "show the subject's BMI category based on recorded height and weight" |

    For decision rules in particular, avoid framings rooted in the JS code (*"push to decisions"*, *"record as a decision"*) — they refer to the data plumbing, not the user-facing conclusion. The retrieving user types something like *"show the BMI category"*, not *"push BMI to encounterDecisions"*.

Show all intents in a numbered list. Wait for the user to accept or edit before moving on.

### 6. Normalise rule JS before writing

Each picked representative is copied **verbatim except for two narrow cleanups** that are pure noise / known transcription bugs in the source spreadsheet. Apply both, deterministically, to every `Rule` cell before writing it to the curated tab:

a. **Strip leading `//SAMPLE RULE EXAMPLE` / `//SAMPLE EDIT FORM RULE` markers.** The marker is metadata about provenance, not part of the rule. Leaving it in teaches the LLM to emit the same marker. Match (case-insensitive) the first line if it matches `^\s*//\s*SAMPLE\s+(RULE\s+EXAMPLE|EDIT\s+FORM\s+RULE)\s*$` and drop it (including its trailing newline).

b. **Repair the `use strict';` directive.** The source corpus consistently has the opening `"` (or `'`) missing — the stored bytes read `use strict';` which is a JS syntax error (`use` and `strict` are two adjacent identifiers). Replace `^[\t ]*use strict';` with `"use strict";` at the start of any line. Do NOT touch already-valid forms (`"use strict";` or `'use strict';`).

These are the only allowed transformations. **No semantic edits**, no helper substitution, no body rewriting. Anything else stays as the cluster representative had it. If a cluster's representative has additional defects beyond these two (e.g. a real logic bug, dead code, an unsupported helper), flag it in the report but leave the cell unchanged.

### 7. Write the curated tab

Use `openpyxl` to write the matching `<kind> (curated)` tab in the **same workbook** with columns:

| ORG name | Form name | Rule | Prompt |

- **Do not modify the raw source tab.**
- Preserve cell wrapping / text formatting where reasonable.
- If the `(curated)` tab already exists: ask before overwriting; offer a numbered variant (e.g. `Validation rule (curated 2)`) as an option.

### 8. Report

Print a short summary:

- Clusters found: N
- Rules kept: K (one per cluster)
- Rules dropped as duplicates: D
- Sample of the first 3 prompts.
- Normalisation counts: `//SAMPLE` markers dropped, `use strict';` directives repaired.

Suggest the user `git diff` the xlsx before committing (the file is git-tracked).

## Constraints

- Never modify a raw tab in place — always write to the matching `(curated)` tab.
- The only allowed JS transformations are the two in §6 (drop the leading `//SAMPLE` marker line; repair `use strict';` → `"use strict";`). No semantic edits, no helper substitution, no body rewriting. Picking a representative ≠ rewriting it.
- If a rule has defects beyond those two, flag them in the §8 report and leave the cell unchanged.

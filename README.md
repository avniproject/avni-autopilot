# Avni Bundle Generator

LangGraph pipeline that turns Avni modelling + scoping Excel documents into a ready-to-upload Avni bundle ZIP. A deterministic parser does the heavy lifting; Claude Haiku 4.5 then takes a second pass at each form to flag two things the parser can't safely fix on its own — field names longer than 255 chars and duplicate field names within a form. Each proposed rename is shown to the user for confirmation before being applied.

Once a bundle is generated:

- Fields inside it can be **added, renamed, or removed** through the same chat agent without re-running the generator (see [Editing fields](#editing-fields-in-an-existing-bundle)).
- **Visit-schedule rules** can be generated from natural-language intents. The pipeline picks them up automatically when the scoping workbook has a `Rules` tab (see [Rule generation as part of the pipeline](#rule-generation-as-part-of-the-pipeline)), and the chat agent can also set or update one on an already-built bundle (see [Setting a visit-schedule rule on an already-built bundle](#setting-a-visit-schedule-rule-on-an-already-built-bundle)). Rules are grounded in the bundle's actual concepts, encounter types, and coded answers; the validator rejects any reference that doesn't exist.

---

## Setup

Requires Python 3.11+.

```bash
git clone git@github.com:avniproject/avni-autopilot.git
cd avni-autopilot
pip install -e .
cp .env.example .env       # then edit .env and set ANTHROPIC_API_KEY
```

`.env.example` documents every supported variable (chat model, bundle I/O paths, LangSmith tracing, log level). Only `ANTHROPIC_API_KEY` is required for bundle generation and field editing.

For **rule generation**, also set:

```
VOYAGE_API_KEY=pa-...    # required when generating rules — embeds the KB catalog + queries
```

Voyage's free tier (3 RPM / 10K TPM) works for first runs but is slow; adding a payment method to the Voyage dashboard unlocks standard limits (200M free tokens included). The embedder retries automatically on rate limits — see `domain/rules/knowledge_base.py` for the env-var overrides if you want to tighten or loosen the throttle.

### Optional: LangSmith tracing

To capture per-call cost and latency for every LLM/graph step (enrichment passes, ReAct turns, tool calls) on the [LangSmith](https://smith.langchain.com) dashboard, add the following to `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_pt_...
LANGSMITH_PROJECT=avni-ai-tools          # any name; groups traces in the UI
# LANGSMITH_ENDPOINT=https://api.smith.langchain.com   # default; override for EU/self-hosted
```

When tracing is on, the REPL prints a `LangSmith: tracing → project '…'` line on startup. Leave `LANGSMITH_TRACING` unset (or empty) to disable — no traces are sent and there is no runtime overhead.

---

## Usage

`src/chat.py` is a conversational front door over the pipeline — a LangGraph ReAct agent (`claude-sonnet-4-6` via `ChatAnthropic`) that exposes tools for: generating a bundle from xlsx (`generate_bundle`), inspecting a built bundle (`list_bundle_fields`), editing fields in place (`edit_bundle_fields`), editing a bundle from updated xlsx (`edit_bundle_from_spec`), setting visit-schedule rules from natural language (`set_visit_schedule_rule`), and resuming a paused run (`resume_bundle`).

Drop your modelling and scoping Excel files into `resources/input/<org>/`, then:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or set in .env
avni-chat
```

Sample session:

```
you> generate srijan
  ⚙ generate_bundle({"org": "srijan"})
agent> Bundle generated successfully. Subject types: 1, programs: 2, encounter types: 9, …
you> for what org did you generate bundle for?
agent> srijan
```

If the LLM enrichment pass finds anything that needs your input, the run pauses and the agent presents each proposed change for confirmation:

```
agent> ### Change 1 of 2 — Long Name Shortening
       Form:  Baseline for Men
       Before: "In many families, women wake up early to cook…"
       After:  "Why do women do most household chores?"
       Reason: Field name exceeds 255 characters.

       Reply: yes / no / edit:<your value>

you> 1. yes 2. yes
agent> ✅ Bundle generated successfully!
```

You can also reply `edit:Some shorter text` for any change to override the LLM's proposed rename.

Slash commands (no token cost): `/quit`, `/clear` (new thread), `/history`, `/help`.

Conversation state is held in-memory by a `MemorySaver` checkpointer keyed by `thread_id`. It does **not** persist across REPL invocations.

---

### Editing fields in an existing bundle

A bundle ZIP can be edited directly through the chat agent.

Sample session:

```
you> list the fields in resources/output/ekam/Ekam.zip for the ANC form
  ⚙ list_bundle_fields({"bundle_path": "resources/output/ekam/Ekam.zip"})
agent> ANC has 6 sections, including "Pregnancy Follow-Up Details" with 14 fields …

you> rename 'Mode of Visit' in that section to 'Visit Mode'
  ⚙ edit_bundle_fields({"bundle_path": "...", "operations": [{"op_id":"op-1","kind":"field.rename", …}]})
agent> Renamed. Forms modified: ANC_<uuid>.json. 1 form element renamed, 1 concept appended.
```

Matching is **exact** (case-folded and whitespace-stripped, no fuzzy match). The agent should call `list_bundle_fields` before constructing operations so the names line up.

---

### Setting a visit-schedule rule on an already-built bundle

`set_visit_schedule_rule` is the chat tool counterpart for rules. Use it when you want to add or change a visit-schedule rule on a bundle you've already generated — same shape as field editing.

```
you> set the visit schedule rule on Baseline for Women in
     resources/output/durga_india/Durga_india.zip — schedule the endline
     2 days later if the answer to 'why do you want to work' is supporting
     family; 4 days if financial independence; 6 days if more confident.
     Don't schedule on Sundays.
  ⚙ list_bundle_fields({"bundle_path": "..."})
agent> I'll match 'supporting family' → 'can support my family',
       'financial independence' → 'It gives me financial independence',
       'more confident' → 'It makes me feel more confident and independent'.
       Confirm?
you> yes
  ⚙ set_visit_schedule_rule({"bundle_path": "...", "form_name": "Baseline for Women", "intent": "..."})
agent> Rule written. Confidence: high. Used helpers: VisitScheduleBuilder.add, RuleCondition.valueInRegistration, …
```

The agent's `list_bundle_fields` call surfaces exact coded-concept answers so the confirmation echoes bundle wording back to the user. The downstream generator and validator independently re-ground against the same vocabulary — so off-bundle references can't slip through even if the agent skips its pre-flight.

---

### Rule generation as part of the pipeline

When the scoping workbook includes a `Rules` (or `Form Rules`) tab, `generate_rules` (the new node after `generate_form_mappings` — see the pipeline graph below) generates each form's `visitScheduleRule` JS as part of the normal bundle build. No extra commands; running `generate <org>` in the chat picks it up automatically.

Tab format — one row per form, columns named like:

| Form name | Visit Schedule Rule |
|---|---|
| `ANC Followup` | "schedule next visit 30 days later" |
| `ANC Followup Cancellation` | "reschedule unless cancel reason is exit" |
| `Pregnancy Exit` | "return empty" |

Optional columns `Encounter Eligibility Rule` / `Validation Rule` / others are parsed today but not generated yet — the parser drops them onto `FormSpec.rule_intents`; the generator skips kinds it doesn't yet know how to write.

Cells are natural-language intent — no syntax required. Forms not listed in the tab keep their `visitScheduleRule` as `""` (today's behaviour for every rule field).

**Knowledge base CLI** (`avni-rules-kb`) maintains the helper + example catalog the rule generator consults. Two everyday commands cover the common cases:

```bash
avni-rules-kb helpers     # after pulling new avni-models source or editing helper files
                          # runs: sync → enrich-use-when → rebuild
                          # add --skip-enrich to skip the Haiku annotation cost

avni-rules-kb examples    # after editing the curated rules xlsx tab
                          # runs: ingest-examples → rebuild
```

The catalog lives at `resources/rules/`; the embedding cache is content-hash-invalidated, so subsequent runs only re-embed entries that changed.

The four underlying sub-commands — `sync`, `enrich-use-when`, `ingest-examples`, `rebuild` — remain available for surgical / CI use (`avni-rules-kb --help`).

---

## Pipeline graphs

### Chat ReAct agent (`src/chat.py`)

The outer LangGraph that hosts the conversation, routes tool calls, and streams responses.

```mermaid
flowchart TD
  start([start]) --> agent
  agent -. end_turn .-> stop([end])
  agent -. tool_calls .-> tools
  tools --> agent
```

### Bundle pipeline (`src/pipeline/`)

A single inner LangGraph that handles both **generate** (`.xlsx` → fresh bundle ZIP) and **edit-from-spec** (`.xlsx` → diff & patch an existing bundle, preserving UUIDs). The two modes share the entire parse + enrich + entity-generation trunk and only diverge after `generate_form_mappings`, where `state.mode` decides the terminal branch.

Two nodes are visited unconditionally but short-circuit internally:

- **`enrich_with_llm`** only calls Claude on forms that have a real issue to fix (a field name longer than 255 chars, or duplicate field names within the same form). Clean forms pass through with zero LLM cost. The whole node is also skipped if `ANTHROPIC_API_KEY` isn't set.
- **`apply_user_decisions`** only fires LangGraph's `interrupt()` if `enrich_with_llm` produced pending changes. When the list is empty (clean spec or LLM had nothing to propose) it returns immediately — no human pause. When it does interrupt, the caller resumes via `Command(resume=resolutions)`.

```mermaid
flowchart TD
  enrich_with_llm["enrich_with_llm<br/>(skipped when spec is clean<br/>or no API key)"]
  apply_user_decisions["apply_user_decisions<br/>(no-op when no pending changes)"]
  generate_rules["generate_rules<br/>(no-op when no Rules tab<br/>or no rule intents)"]

  start([start]) --> discover_files
  discover_files -. abort .-> stop([end])
  discover_files -. continue .-> parse_documents
  parse_documents -. abort .-> stop
  parse_documents -. continue .-> enrich_with_llm
  enrich_with_llm --> apply_user_decisions
  apply_user_decisions -. pauses on interrupt .-> apply_user_decisions
  apply_user_decisions --> generate_entities
  generate_entities --> generate_forms
  generate_forms --> generate_form_mappings
  generate_form_mappings --> generate_rules
  generate_rules -. mode=generate .-> package_zip
  generate_rules -. mode=edit_from_spec .-> diff_against_bundle
  package_zip --> stop
  diff_against_bundle --> apply_diff_edits
  apply_diff_edits --> stop
```

### Editing fields via chat (`src/bundle_editor.py`)

Operates on a bundle ZIP (or unpacked directory) and writes back atomically.

---
## Notes

- Skip-logic translation into Avni's declarative rule format is out of scope.
- **Sheet classification is content-driven**, not name-driven; the parser inspects column headers and the first column's contents rather than relying on sheet names matching a fixed list. The `Rules` / `Form Rules` tab is detected the same way (`Form name` column + at least one rule-column alias).
- **UUIDs are deterministic** (UUID v5 over a fixed namespace + a name-derived seed). Re-running the generator with the same input produces identical UUIDs, so re-uploads are idempotent. The bundle editor uses the same scheme — see [`specs/BUNDLE_EDITING_SDD.md`](specs/BUNDLE_EDITING_SDD.md) §6.

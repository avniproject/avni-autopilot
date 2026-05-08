# Avni Bundle Generator

LangGraph pipeline that turns Avni modelling + scoping Excel documents into a ready-to-upload Avni bundle ZIP. A deterministic parser does the heavy lifting; Claude Haiku 4.5 then takes a second pass at each form to flag two things the parser can't safely fix on its own — field names longer than 255 chars and duplicate field names within a form. Each proposed rename is shown to the user for confirmation before being applied.

---

## Setup

Requires Python 3.11+.

```bash
git clone git@github.com:avniproject/avni-autopilot.git
cd avni-autopilot
pip install -e .
```

---

## Usage

`src/chat.py` is a conversational front door over the pipeline — a LangGraph ReAct agent (`claude-sonnet-4-6` via `ChatAnthropic`) that exposes `generate_bundle` as a tool.

Drop your modelling and scoping Excel files into `resources/input/<org>/`, then:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or set in .env
python src/chat.py
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

Slash commands (no token cost): `/quit`, `/clear` (new thread), `/history`, `/graph`, `/help`.

Conversation state is held in-memory by a `MemorySaver` checkpointer keyed by `thread_id`. It does **not** persist across `python src/chat.py` invocations.

---

## Pipeline

```
discover_files → parse_documents → enrich_with_llm → apply_user_decisions
                                                           │
                                                           ▼
                                                  generate_entities → generate_forms
                                                  → generate_form_mappings → package_zip
```

---

## Notes

- **Rule fields** (`validationRule`, `visitScheduleRule`, `enrolmentEligibilityCheckRule`, etc.) are emitted as empty strings — translating skip logic into Avni's declarative rule format is out of scope for this version.
- **Sheet classification is content-driven**, not name-driven; the parser inspects column headers and the first column's contents rather than relying on sheet names matching a fixed list.

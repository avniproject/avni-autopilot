# Avni Bundle Generator

LangGraph pipeline that turns Avni modelling + scoping Excel documents into a ready-to-upload Avni bundle ZIP.

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

Slash commands (no token cost): `/quit`, `/clear` (new thread), `/history`, `/graph`, `/help`.

Conversation state is held in-memory by a `MemorySaver` checkpointer keyed by `thread_id`. It does **not** persist across `python src/chat.py` invocations.

---

## Notes

- **Rule fields** (`validationRule`, `visitScheduleRule`, `enrolmentEligibilityCheckRule`, etc.) are emitted as empty strings — translating skip logic into Avni's declarative rule format is out of scope for this version.
- **Sheet classification is content-driven**, not name-driven; the parser inspects column headers and the first column's contents rather than relying on sheet names matching a fixed list.

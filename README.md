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
### Option 1: Command line

Drop your modelling and scoping Excel files into `resources/input/<org>/`, then run:

```bash
python src/cli.py --org <org>
```

Example:

```bash
python src/cli.py --org srijan
```

Output:

```
Org        : srijan
Input dir  : resources/input/srijan
Output dir : resources/output/srijan

Subject types  : 1
Programs       : 2
Encounter types: 9
Forms          : 22 main + 13 cancellation
Concepts       : 84
Form mappings  : 35

Bundle ZIP     : resources/output/srijan/Srijan.zip
```

If `resources/input/<org>/` is missing or contains no `.xlsx` files, the script exits with an error.

---

### Option 2: Interactive chat

`src/chat.py` is a conversational front door over the same pipeline — a LangGraph ReAct agent (`claude-sonnet-4-6` via `ChatAnthropic`). Useful when you don't want to type CLI flags or want follow-up questions.

Setup:
```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or set in .env
python src/chat.py
```

Sample Output:
```
you> generate srijan
  ⚙ generate_bundle({"org": "srijan"})
agent> Bundle generated successfully. Subject types: 1, programs: 2, encounter types: 9, …
you> for what org did you generate bundle for?
agent> srijan org
```

Slash commands (no token cost): `/quit`, `/clear` (new thread), `/history`, `/help`.

Conversation state is held in-memory by a `MemorySaver` checkpointer keyed by `thread_id`. It does **not** persist across `python src/chat.py` invocations.

---

## Notes

- **Rule fields** (`validationRule`, `visitScheduleRule`, `enrolmentEligibilityCheckRule`, etc.) are emitted as empty strings — translating skip logic into Avni's declarative rule format is out of scope for this version.
- **Sheet classification is content-driven**, not name-driven; the parser inspects column headers and the first column's contents rather than relying on sheet names matching a fixed list.

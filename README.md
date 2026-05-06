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

Drop your modelling and scoping Excel files into `resources/input/<org>/`, then run:

```bash
python src/main.py --org <org>
```

Example:

```bash
python src/main.py --org srijan
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

## Notes

- **Rule fields** (`validationRule`, `visitScheduleRule`, `enrolmentEligibilityCheckRule`, etc.) are emitted as empty strings — translating skip logic into Avni's declarative rule format is out of scope for this version.
- **Sheet classification is content-driven**, not name-driven; the parser inspects column headers and the first column's contents rather than relying on sheet names matching a fixed list.

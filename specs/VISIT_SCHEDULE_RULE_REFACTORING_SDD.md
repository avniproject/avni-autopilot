# Visit Schedule Rule — Refactoring SDD

## 1. Objective

Apply targeted refactors to the visit-schedule rule code shipped in commit `03faff7` (48 files, ~6800 LOC) so that:

- `kb_cli.py` (731 lines) is split along its natural seams.
- Catalog data shared between modules has one source of truth (no parallel hand-maintained lists).
- Per-rule-kind machinery built for kinds not yet shipped is trimmed back to what `visitScheduleRule` actually needs (the rest is added when the second kind lands).
- The handful of obvious correctness / consistency bugs (silent JSON swallow, prompt-cache misses, re-reads, duplicated decisions) are fixed.

Behavior must not change. Outputs (`visitScheduleRule` JSON content, warning text, embedding cache) must be byte-identical before and after.

---

## 2. Scope

### In scope

- Code reorganisation within `src/domain/rules/` and `src/domain/bundle_editor.py`.
- New module(s) under `src/domain/rules/` for shared accessors and shared CLI sub-modules.
- Read-once / module-level caching where the current code re-instantiates or re-reads.
- Swapping the ad-hoc YAML parser/writer for `yaml.safe_load` / `yaml.safe_dump`.

### Out of scope

- Anything that changes generated JS, warnings, or the on-disk catalog format.
- Wiring a second `RuleKind` (`validationRule` / eligibility / summary). This refactor only trims unused machinery; it does not remove `RuleKind` itself.
- Re-running `kb sync` / `kb enrich-use-when` against a fresh checkout of avni-models.
- Migrating `bundle_editor.py` between `os.path` and `pathlib`. The package boundary is acceptable.
- New tests beyond regression coverage for the changes here.

### Precondition

`pytest` passes on `main` at commit `03faff7`. The refactor is verified by `pytest` still passing and by a before/after diff of the generated `visitScheduleRule` strings on the existing `requirements/rules_ai_automation.xlsx` example corpus.

---

## 3. Refactor items

Items are grouped into four phases; phases are sequential, items within a phase are independent.

### Phase A — Split `kb_cli.py`

**A1. Split `kb_cli.py` (731 lines) into four files.**
- Location: `src/domain/rules/kb_cli.py:1-731`
- Problem: One file mixes argparse dispatch, JS regex extraction (sync), xlsx ingestion (`ingest-examples`), Voyage rebuild, and Haiku-based `enrich-use-when`. Each section drags its own helpers, regexes, and dataclasses.
- Change:
  - `kb_cli.py` — argparse + dispatch only (~80 lines).
  - `kb_sync.py` — `_extract_public_methods`, `_merge_helpers`, `_ENTITY_CLASSES`, MVP annotations.
  - `kb_ingest.py` — xlsx → markdown (`cmd_ingest_examples`, `_row_to_example`, YAML writer).
  - `kb_enrich.py` — Haiku batch (`cmd_enrich_use_when`, `_extract_method_bodies`, `_collect_balanced_body`, `_net_brace_delta`, `_MethodAnnotation`).
- Effort: Medium.

**A2. Extract shared method-declaration iterator.**
- Location: `kb_cli.py:169-191` (`_extract_public_methods`) and `kb_cli.py:638-676` (`_extract_method_bodies`).
- Problem: Both walk `_METHOD_PATTERNS`, both apply `_SKIP_NAMES` / `_RESERVED_WORDS`, with subtly different brace tracking.
- Change: Extract `_iter_method_declarations(lines) -> Iterator[(name, params, line_index)]` and let each caller consume what it needs (one ignores `line_index`, the other feeds it into `_collect_balanced_body`).
- Effort: Medium (lands cleanly after A1 because the two helpers end up in `kb_sync.py` + `kb_enrich.py`).

### Phase B — Pin shared catalog data and trim premature machinery

**B1. Lift duplicated accessor name lists.**
- Location: `kb_cli.py:399-407` (`_CONCEPT_ACCESSOR_RE`) and `validator.py:27-53` (`_CONCEPT_ACCESSORS`, `_ENCOUNTER_TYPE_ACCESSORS`).
- Problem: Same catalog of method names is hand-maintained twice — as a regex group on one side, a `frozenset` on the other. Either side can drift silently.
- Change: New `src/domain/rules/accessors.py` exporting:
  ```python
  CONCEPT_ACCESSORS: frozenset[str] = frozenset({...})
  ENCOUNTER_TYPE_ACCESSORS: frozenset[str] = frozenset({...})
  ```
  - `validator.py` imports the frozensets directly.
  - `kb_cli.py` (post-A1: `kb_ingest.py`) builds its regex from `CONCEPT_ACCESSORS` at module load.
- Effort: Low.

**B2. Trim per-`RuleKind` machinery to what ships.**
- Location: `rule_spec.py:17-26`, `prompts.py:25-32` (`_KIND_META`), `prompts.py:94-96` (`_RETURN_EXPRESSION_BY_KIND`), `parser.py:30-58` (`_COLUMN_ALIASES_BY_FIELD`).
- Problem: Only `VISIT_SCHEDULE` is wired; the rest is dead code paying maintenance interest.
- Change:
  - Keep `RuleKind` enum but reduce to `VISIT_SCHEDULE = "visitScheduleRule"`.
  - Inline the single-entry `_KIND_META` / `_RETURN_EXPRESSION_BY_KIND` dicts directly into `build_system_prompt`.
  - Trim `_COLUMN_ALIASES_BY_FIELD` to the `visitScheduleRule` entry only.
  - Drop `applies_to: list[str]` from helper-catalog entries (every entry currently lists every kind — noise without a consumer).
- Effort: Low.
- Note: Keep §8 of `VISIT_SCHEDULE_RULE_SDD.md` honest by leaving a one-line comment in `rule_spec.py` noting where to extend the enum when the next kind lands.

**B3. Single source of truth for `_ENTITY_CLASSES`.**
- Location: `kb_cli.py:105-112` (used at 125 and 554).
- Problem: After A1 the tuple lives in `kb_sync.py` but `kb_enrich.py` also iterates it (silently). If a future class is added to one site and not the other, enrichment skips it.
- Change: Move `_ENTITY_CLASSES` to `accessors.py` (alongside the frozensets — single shared catalog module). Both `kb_sync.py` and `kb_enrich.py` import it; add a runtime assertion that the on-disk entity catalog files match the tuple's first column on each command run.
- Effort: Low.

### Phase C — Fix correctness + consistency bugs

**C1. Shared `validate_and_decide` gate.**
- Location: `validator.py.check()`; gates at `nodes.py:332-348` and `chat/tools.py:301-318`.
- Problem: `check()` returns warnings, then two call sites re-implement the "join warnings, write only if ok" decision separately. Drift risk between pipeline and chat tool.
- Change: Add `validate_and_decide(result, spec) -> tuple[bool, list[str]]` next to `check()`. Both call sites use it.
- Effort: Low.

**C2. Module-level `_GENERATOR` for prompt-cache hits.**
- Location: `generator.py:135-142` (`generate_rule`) called from `chat/tools.py:301`.
- Problem: Each invocation instantiates a fresh `RuleGenerator` (= fresh `ChatAnthropic`), defeating the prompt cache the docstring promises.
- Change: Module-level `_GENERATOR = RuleGenerator()` in `chat/tools.py`, matching the existing `_pipeline_graph = build_graph()` pattern at `chat/tools.py:34`. Alternatively memoize inside `generate_rule`.
- Effort: Low.

**C3. Read each bundle JSON file once.**
- Location: `bundle_editor.py:671-732` (`load_form_rule_context`).
- Problem: Calls `_load_entity_names` then `_load_entity_uuid_map` for `subjectTypes.json`, `programs.json`, `encounterTypes.json` — each file opened and parsed twice.
- Change: New `_load_entity_index(workdir, filename) -> (names: list[str], uuid_to_name: dict[str, str])` called once per file; both downstream consumers use its outputs.
- Effort: Low.

**C4. Log on JSON decode error in `_load_existing_helpers`.**
- Location: `kb_cli.py:282-292` (post-A1: `kb_sync.py`).
- Problem: On `JSONDecodeError` returns `{}` silently — a corrupted catalog file is silently overwritten on next sync.
- Change: Log a warning with the file path, matching the pattern at `knowledge_base.py:210`.
- Effort: Low.

**C5. Pydantic / dataclass consistency inside `rules/`.**
- Location: `rule_spec.py` (Pydantic), `knowledge_base.py:65-108` (`@dataclass(frozen=True)`), `kb_cli.py:494-504` (Pydantic again).
- Problem: Three different choices in one package with no documented rule.
- Change: Document the rule and apply it:
  - Pydantic — cross-module contracts (`RuleSpec`, `RuleResult`, helper/example catalog entries the LLM or other modules consume).
  - `@dataclass(frozen=True)` — internal-only types (`_IngestStats`, `RetrievedContext` if it doesn't cross a module boundary).
  - Convert `HelperEntry` / `ExampleEntry` to Pydantic; keep `_MethodAnnotation` as Pydantic (it crosses to the LLM); leave internal-only dataclasses as they are.
- Effort: Low.

### Phase D — Quick wins

**D1. Replace ad-hoc YAML parser with `pyyaml`.**
- Location: `knowledge_base.py:559-574` (`_parse_yaml_scalar`) and `kb_cli.py:439-461` (`_render_example` YAML write).
- Problem: Custom parser doesn't handle multi-line values; writer's `_escape` helper duplicates `yaml.safe_dump` poorly. `pyyaml` is already a transitive dep via langchain.
- Change: Use `yaml.safe_load` on the frontmatter block; `yaml.safe_dump(..., sort_keys=False, allow_unicode=True)` to render. Delete `_parse_yaml_scalar` and `_escape`.
- Effort: Low.

**D2. Drop unused parameter.**
- Location: `bundle_editor.py:328` (`_build_element_from_captured` — `form_name` never referenced).
- Change: Remove the parameter and update the call site.
- Effort: Low.

**D3. De-duplicate query-text vs. prompt FORM block.**
- Location: `knowledge_base.py:481-495` (`_query_text` writes `formType=… subject=… program=… encounter=…` into the embedding text) and `prompts.py:142-148` (same fields in the FORM block of the user prompt).
- Problem: The embed query needs the context for retrieval; the prompt already shows the same fields to the LLM. The LLM-prompt side is the cheaper one to drop, since the context the LLM actually consumes is `AVAILABLE_CONCEPTS` / `AVAILABLE_ENCOUNTER_TYPES` / `AVAILABLE_PROGRAMS` — `formType` etc. are scene-setting only.
- Change: Drop the FORM block from the user prompt; keep it in the query embedding. Note in the prompt template that retrieval has already scored on form context.
- Effort: Low.
- Note: This one changes the prompt — verify with a regenerate-on-corpus diff that the output JS doesn't change materially. If it does, revert this item and keep both sides.

---

## 4. Files to create / change

| File | Status | Description |
|---|---|---|
| `src/domain/rules/kb_cli.py` | edit | Reduced to argparse + dispatch (~80 lines). |
| `src/domain/rules/kb_sync.py` | new | `_extract_public_methods`, `_merge_helpers`, MVP annotations. |
| `src/domain/rules/kb_ingest.py` | new | xlsx → markdown (`cmd_ingest_examples`, `_row_to_example`). |
| `src/domain/rules/kb_enrich.py` | new | Haiku batch (`cmd_enrich_use_when`, brace-balanced body extraction). |
| `src/domain/rules/accessors.py` | new | `CONCEPT_ACCESSORS`, `ENCOUNTER_TYPE_ACCESSORS`, `_ENTITY_CLASSES`. |
| `src/domain/rules/validator.py` | edit | Import accessor frozensets; add `validate_and_decide`. |
| `src/domain/rules/rule_spec.py` | edit | Trim `RuleKind` to `VISIT_SCHEDULE`. |
| `src/domain/rules/prompts.py` | edit | Inline single-entry dicts; drop FORM block from user prompt (D3). |
| `src/domain/rules/parser.py` | edit | Trim `_COLUMN_ALIASES_BY_FIELD` to `visitScheduleRule`. |
| `src/domain/rules/generator.py` | edit | (Optional) memoize `_GENERATOR` — or do this in `chat/tools.py`. |
| `src/domain/rules/knowledge_base.py` | edit | Use `pyyaml`; drop `_parse_yaml_scalar`; remove `applies_to` reads; convert `HelperEntry` / `ExampleEntry` to Pydantic per C5. |
| `src/domain/bundle_editor.py` | edit | `_load_entity_index` helper; drop unused `form_name` param. |
| `src/chat/tools.py` | edit | Module-level `_GENERATOR`; use `validate_and_decide`. |
| `src/pipeline/nodes.py` | edit | Use `validate_and_decide`. |

No new runtime dependency. `pyyaml` is already a transitive dep.

---

## 5. Verification

Run after each phase:

1. `pytest` — all existing tests pass.
2. **Regression harness:** regenerate `visitScheduleRule` against every row of the `VS rule (curated)` tab in `requirements/rules_ai_automation.xlsx`. Diff before vs after — must be byte-identical for Phases A–C. For Phase D3, manually review the diff (drop FORM block); revert D3 if the JS content changes substantively.
3. **Catalog re-sync sanity:** `python -m rules.kb sync --dry-run` against an unchanged avni-models checkout should produce zero changes to `resources/rules/helpers/entities/*.json`.

---

## 6. Order of execution

A → B → C → D, **in that order**, with verification between phases. Within a phase the items are independent.

Rationale:
- A is mechanical and large; landing it first means every later phase edits the post-split files.
- B depends on A's file layout (B3 lands `_ENTITY_CLASSES` in the new `accessors.py`).
- C is small and surgical; do it once the package shape has settled.
- D is risk-isolated (D3 has the only behaviour-adjacent change) — easy to revert.

---

## 7. Out of scope (recap)

- Adding a second `RuleKind`.
- New tests.
- `os.path` → `pathlib` migration in `bundle_editor.py`.
- Changing helper-catalog or example-catalog file formats.
- Re-embedding the catalog.

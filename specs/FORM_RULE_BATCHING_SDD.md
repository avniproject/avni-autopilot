# Form-Level Rule Batching — Software Design Document

Delivered in **two phases**:

- **Phase 1 — Concurrent execution (infrastructure only).** All rule
  generation calls that `generate_rules` makes today — one call per
  (form, rule kind) for form-level rules, one field batch per form — are
  submitted to one bounded thread pool at once. No prompt, schema, or
  generator change; quality-neutral by construction. Ships with
  path-attributable telemetry that doubles as the quality baseline for
  Phase 2.
- **Phase 2 — Form-level batching (prompt change, quality-gated).** One
  Sonnet call per form covering all of that form's `rule_intents`
  (visit-schedule / validation / edit-form / decision), replacing the
  per-(form, kind) calls inside the Phase 1 pool. Extends the batching
  pattern introduced for field rules
  (`RuleGenerator.generate_field_batch`). Gated on the Phase 1 validator
  rejection-rate baseline (§7): rule-generation quality currently has
  only basic test coverage, so the prompt-surface change must be
  measurable before it ships.

The form-level and field-level batches stay separate calls. Merging them
into one per-form call was considered and rejected (§3.2).

---

## 1. Objective

Today `generate_rules` makes, **sequentially**:

- one LLM call per (form, rule kind) for form-level rules — up to 4 per
  form, each re-sending the same form context (concepts, encounter types,
  programs, concept answers) and its own retrieved helpers/examples;
- one batched LLM call per form for field-level rules (existing).

After **Phase 1**:

- the same calls, unchanged in content, run concurrently in one bounded
  pool — bundle wall time approaches the slowest call instead of the sum
  of all calls;
- every generation logs its path, giving a per-kind validator
  rejection-rate baseline from real bundles.

After **Phase 2**:

- **one** LLM call per form for all its form-level rule kinds, sharing
  one context block and kind-grouped HELPERS/EXAMPLES sections;
- the single-rule path `RuleGenerator.generate(spec)` is untouched — the
  chat `set_form_rule` tool keeps using it, and it remains the batch
  fallback path (§5).

All rule specs for one form share identical `available_concepts`,
`available_encounter_types`, `available_programs`, and `concept_answers`
(they are built from the same `form_spec` by `_build_rule_spec`), which is
the same property that made the field batch clean.

---

## 2. Scope

### In scope — Phase 1 (concurrency)

- `generate_rules` node changes: submit **all** generation calls
  (today's per-(form, kind) form-level calls and per-form field batches,
  across every form) to one bounded `ThreadPoolExecutor` at once (§6);
  apply results to `forms_json` sequentially after joining.
- Path-attributable telemetry (`path=single` / `path=field_batch` in log
  lines) with per-kind validator rejection counts — the Phase 2 quality
  baseline (§7).
- `AVNI_RULES_MAX_CONCURRENCY` env var (default 20; `1` restores today's
  sequential behaviour).
- No prompt, schema, generator, or KB change of any kind.

### In scope — Phase 2 (form-level batching)

**Gate: a Phase 1 rejection-rate baseline from real bundles exists**, so
§9.9 has numbers to compare against.

- `_LLMFormRuleItem` / `_LLMFormBatchOutput` structured-output schemas in
  `src/domain/rules/generator.py`, joined by `rule_kind` (a `Literal` of
  the four form-level kind values — not free text).
- `RuleGenerator.generate_form_batch(entries)` mirroring
  `generate_field_batch`: merge per-entry `RetrievedContext`s, one
  invoke, map results back by kind.
- `build_form_batch_system_prompt(entity_param)` and
  `build_form_batch_user_prompt(...)` in `src/domain/rules/prompts.py`,
  with per-entry inline return contracts and kind-grouped examples and
  helpers (§4).
- **Fallback to single calls** on batch failure or missing kinds (§5) —
  batch failure degrades to the Phase 1 per-rule path, never to empty
  rules.
- Grouping `pending_form` by form name; the form batch replaces that
  form's per-kind tasks in the Phase 1 pool (§6).
- Batch-sized `max_tokens` for the form batch (§4.4).
- `path=batch` / `path=fallback` telemetry values and an env-var kill
  switch `AVNI_RULES_FORM_BATCH=0` reverting to the Phase 1 per-rule
  calls (§7).

### Out of scope

- **Merging form-level and field-level batches into one call per form.**
  Rejected: it converts a bounded duplicate-context cost into truncation
  blast radius (one `max_tokens` cutoff loses every rule of the form),
  tail degradation on the long mixed output, and rubric dilution across
  5 return contracts. See §3.2.
- **Batching one rule kind across forms.** Higher batching ceiling but
  requires per-entry concept/answer blocks and invites cross-form symbol
  leakage. Rejected.
- Changes to `RuleGenerator.generate(spec)`, the chat tools, the
  validator, the writer, or KB retrieval (`retrieve_batch` already
  covers all specs in one Voyage call).
- Anthropic Message Batches API (async, minutes-scale latency — wrong
  fit for the interactive pipeline).
- Field-batch prompt or schema changes, beyond running the call
  concurrently with the form batch.

### Precondition

- `FORM_LEVEL_RULES_SDD.md` and the field-batch path
  (`generate_field_batch`, `build_field_batch_*` prompts) are implemented
  and in production.

---

## 3. Batching axis — decisions

### 3.1 Batch by form, across kinds

`FormSpec.rule_intents` is a dict keyed by kind value, so kinds are
unique within a form — the batch join key is exact, with no fuzzy text
matching. Specs within a form share the full context block. The win is
bounded by intents-per-form (≤ 4); the concurrency change (§6) carries
the wall-time reduction for forms with few intents.

### 3.2 Two batches per form, not one

The only cost of keeping form-level and field-level as separate calls is
the shared context block sent twice per form (prompt caching does not
help — the system prompts differ and the Anthropic cache is
prefix-based). Merging would:

1. put a form's many short field rules and its long visit-schedule /
   decision rules in one response, so a single truncation or
   structured-output parse failure loses everything;
2. degrade the complex rules generated late in a long response;
3. soften the field batch's tight single-contract system prompt
   ("every IIFE returns `FormElementStatus`") into a 5-contract rubric.

Concurrent dispatch (§6) recovers the latency; the duplicate context
block is accepted.

---

## 4. Prompts

### 4.1 System prompt — `build_form_batch_system_prompt(entity_param)`

Parametrised **only on `entity_param`** (≤ 4 variants — better prompt-
cache reuse than today's per-(kind, entity_param) prompts). Contains:

- the IIFE skeleton with `{entity_param}`;
- a per-kind contract table rendered from the existing `_KIND_META`
  return-type descriptions and `_RETURN_EXPRESSION_BY_KIND`
  (`src/domain/rules/prompts.py:141`) — one row per form-level kind;
- the hard constraints 3–7 from the existing single-rule prompt
  (helper / concept / encounter-type allowlists, `CONCEPT_ANSWERS`
  exact-match, empty-`js`-on-inexpressible) verbatim;
- batch instructions: "return exactly one item per numbered entry, with
  `rule_kind` matching that entry's RULE_KIND"; "use only the examples
  under your entry's kind heading"; "prefer the helpers under your
  entry's kind heading — they were retrieved for your intent — but any
  helper listed under HELPERS may be used";
- the same per-item confidence rubric as the field batch.

### 4.2 User prompt — entries block with inline contracts

The binding return contract is repeated **inline per entry**, where the
model reads it at generation time, not only in the system-prompt table:

```
RULES
[1] RULE_KIND: VisitSchedule — MUST return scheduleBuilder.getAll()
    INTENT: <intent text>

[2] RULE_KIND: Validation — MUST return validationResults (array of
    createValidationError results; empty array when nothing is wrong)
    INTENT: <intent text>
```

The inline fragment is derived from `_RETURN_EXPRESSION_BY_KIND` plus a
one-line condensation per kind — no new source of truth.

**Entry order is complex-first**: `VisitSchedule`, `Decision`,
`EditForm`, `Validation` (fixed order, filtered to kinds present). The
hardest bodies are generated early in the response, ahead of any tail
sloppiness.

The shared context sections (`AVAILABLE_CONCEPTS`,
`AVAILABLE_ENCOUNTER_TYPES`, `AVAILABLE_PROGRAMS`, `CONCEPT_ANSWERS`)
are rendered once from the first spec, exactly like
`build_field_batch_user_prompt`.

### 4.3 Examples and helpers — grouped by kind, not flat union

Retrieval is already kind-pure per spec (`KnowledgeBase._rank` filters
`examples` by `spec.rule_kind`), so the merge preserves grouping instead
of the field batch's flat union:

```
EXAMPLES — VisitSchedule
<rendered examples for the VisitSchedule entry>

EXAMPLES — Validation
<rendered examples for the Validation entry>
```

`ExampleEntry.rule_kind` (`src/domain/rules/knowledge_base.py:101`)
supplies the heading — no ingest change.

**Helpers are grouped the same way, but with softer semantics.** Most
helpers carry no `applies_to` restriction (they apply to every kind), so
the kind signal lives in *retrieval relevance* — each entry's context
holds the helpers ranked against that entry's intent. The field batch's
flat union discards that signal; the form batch instead renders one
`HELPERS — <Kind>` section per entry from that entry's retrieved
context:

```
HELPERS — VisitSchedule
<helpers retrieved for the VisitSchedule entry>

HELPERS — Validation
<helpers retrieved for the Validation entry>
```

A helper retrieved for multiple entries appears under each of their
headings (per-entry top-k keeps sections small; duplication is bounded
and accepted). Unlike examples, helpers are **not** hard-scoped: the
system prompt says entries should *prefer* helpers under their own
heading but may use any helper listed anywhere in HELPERS. Helpers are
genuinely kind-agnostic (an accessor like
`getObservationReadableValue` is valid in any rule body), and a hard
scope would manufacture grounding failures whenever a rule needs a
helper that happened to surface under a sibling entry's heading.

### 4.4 `max_tokens`

Form-level bodies (visit schedules especially) run far longer than field
rules. Budget scales with entry count:

```
form_batch_max_tokens = min(4096 + 3072 * n_entries, 16384)
```

Constructor knob `form_batch_max_tokens` on `RuleGenerator`, defaulted
from the formula's cap, mirroring `field_batch_max_tokens`.

---

## 5. Generator — `generate_form_batch` and fallback

```python
def generate_form_batch(
    self,
    entries: list[tuple[RuleKind, RuleSpec, RetrievedContext | None]],
) -> list[RuleResult]:
```

Behaviour mirrors `generate_field_batch` with two deliberate deviations:

1. **Join by kind.** Output items carry
   `rule_kind: Literal["VisitSchedule", "Validation", "EditForm",
   "Decision"]` (the enum's value strings). Results map back by kind;
   duplicates keep the first item and warn.
2. **Fallback instead of empty.** `generate_field_batch` returns
   `_empty(...)` for every entry when the batch call throws. The form
   batch instead falls back to the existing single-rule path — with ≤ 4
   entries the retry is cheap and reuses a trusted code path:

   - batch invoke raises (includes structured-output parse failures,
     which is how truncation typically surfaces) → call
     `self.generate(spec, context=ctx)` once per entry;
   - batch succeeds but a kind is missing from `output.rules` →
     single-call fallback for the missing kinds only.

   One fallback attempt per entry; a failed fallback returns the
   canonical `_empty` result as today. Every fallback logs at warning
   level with the form name and kind (§7).

A single-entry batch is still sent through the batch path (not special-
cased to `generate()`) so the pipeline exercises one code path; the
env-var kill switch (§7) is the escape hatch, not a size heuristic.

---

## 6. Pipeline node — grouping and concurrency

### 6.1 Grouping

`pending_form` collection is unchanged. Before generation it is grouped
by `form_spec.name` (same shape as the existing field-level `by_form`
grouping at `src/pipeline/nodes.py:441`), each group ordered
complex-first per §4.2. `retrieve_batch` and the
`form_contexts` / `field_contexts` split are unchanged.

### 6.2 Concurrent dispatch — full fan-out across forms

The node is a synchronous function invoked from an async-from-thread
runtime (`src/pipeline/graph.py` docstring), so concurrency uses
`concurrent.futures.ThreadPoolExecutor`, not asyncio.
`ChatAnthropic.invoke` is a thread-safe blocking HTTP call.

**All generation calls for the bundle are submitted at once.** In
Phase 1 that is one task per pending (form, kind) form-level rule plus
one per form's field-level batch; in Phase 2 each form's per-kind tasks
collapse into one `generate_form_batch` task:

```
with ThreadPoolExecutor(max_workers=AVNI_RULES_MAX_CONCURRENCY) as pool:
    futures = {}
    for form_name in all forms with any pending rules:
        # Phase 1: one task per (form, kind) via generator.generate(spec, ctx)
        # Phase 2: one task per form via generator.generate_form_batch(entries)
        for task in form-level tasks for this form:
            futures[pool.submit(task)] = ...
        if form has field-level entries:
            futures[pool.submit(generator.generate_field_batch, batch_input)] = ...
    join all futures
# then, on the node thread, in deterministic form order:
#   validate_and_decide + write into forms_json + append warnings
```

Rules:

- **Generation is concurrent; mutation is not.** Worker tasks only call
  the generator and return `RuleResult` / `list[RuleResult]`. All
  `forms_json` writes and `warnings` appends happen on the node thread
  after joining, in the original form order, so warning ordering stays
  deterministic and no shared state is mutated across threads.
  Namespacing of warnings (`rules.<kind>.<form>[...]`) is unchanged.
- **Bounded pool.** `AVNI_RULES_MAX_CONCURRENCY` env var, default `20`.
  The Anthropic API has no per-connection concurrency cap — limits are
  per-minute budgets (RPM / uncached-input TPM / output TPM, token-bucket
  enforced per model). Sizing against `claude-sonnet-4-6` (Sonnet 4.x
  bucket) at the lowest ("Start") tier — 1,000 RPM / 2,000,000 ITPM /
  400,000 OTPM — for the Phase 1 worst case (10 forms → ~40 form-level
  singles at ~5k uncached input / ~0.5k output each, plus ~10 field
  batches at ~12k input / ~3k output each): ~320k ITPM and ~90k OTPM if
  all 50 fired simultaneously — under a fifth of budget. Phase 2 drops
  the call count to ~20/bundle and the margins widen further. OTPM
  counts only tokens actually generated (`max_tokens` does not reserve
  budget, so the batch-sized 16384 knob is free). The pool bound of 20
  therefore exists for the API's *acceleration* limits (sharp usage
  spikes can 429) and to cap worst-case memory on very large bundles,
  not because the per-minute budgets require it — 50 Phase 1 calls run
  in ~3 waves. A 429 is retried by the SDK automatically honouring
  `retry-after` (`max_retries` default 2).
- **Fallback runs inside the worker task** (§5), so a degraded form does
  not serialise the others.
- A worker raising unexpectedly is caught at join; its entries get
  `_empty` results plus a namespaced warning — one form's failure never
  aborts the node.

**Prompt-cache note.** Concurrent first calls sharing a system-prompt
variant race the cache write and each pay the uncached read — a cache
entry only becomes readable after the first response begins. This is
accepted: the shared system prompts here are small, and Sonnet 4.6's
minimum cacheable prefix is 2,048 tokens, so these prompts may not cache
at all regardless of ordering. No warm-up ordering is added.

### 6.3 Chat path

`set_form_rule` (single rule, `context=None`) and
`set_form_element_rule` are unaffected.

---

## 7. Telemetry and kill switch

- Every generation logs its path with form name and kind: Phase 1 emits
  `path=single` (form-level) and `path=field_batch`; Phase 2 adds
  `path=batch` and `path=fallback`. Validator rejections are thereby
  attributable per path — **the Phase 1 logs are the quality baseline
  the Phase 2 gate compares against** (§9.9).
- `AVNI_RULES_FORM_BATCH=0` (Phase 2) reverts the node to the Phase 1
  one-call-per-(form, kind) loop (still inside the thread pool —
  concurrency and batching are independently switchable via
  `AVNI_RULES_MAX_CONCURRENCY=1`).
- The node summary log line gains per-path counts:
  `N batch, M fallback, K single`.

---

## 8. Files to create / change

| File | Phase | Status | Description |
|---|---|---|---|
| `src/domain/rules/orchestrator.py` | 1 | new | `collect_pending_rules` / `generate_all` (`ThreadPoolExecutor` fan-out of all per-(form, kind) calls and field batches) / `apply_results` (sequential validated writes), `path=` telemetry. Absorbs the rule-spec and form-element helpers formerly private to `pipeline.nodes`. |
| `src/pipeline/nodes.py` | 1 | edit | `generate_rules` becomes a thin state adapter over the orchestrator; reads `AVNI_RULES_MAX_CONCURRENCY`. |
| `src/domain/rules/orchestrator.py` | 2 | edit | `generate_all`: group form-level tasks by form; form batch replaces per-kind tasks; `AVNI_RULES_FORM_BATCH` kill switch; `path=batch`/`fallback`. |
| `src/domain/rules/generator.py` | 2 | edit | `_LLMFormRuleItem` (`rule_kind` Literal join key), `_LLMFormBatchOutput`, `generate_form_batch` with per-entry single-call fallback, `form_batch_max_tokens` knob. |
| `src/domain/rules/prompts.py` | 2 | edit | `build_form_batch_system_prompt(entity_param)` (per-kind contract table from `_KIND_META` / `_RETURN_EXPRESSION_BY_KIND`), `build_form_batch_user_prompt` (inline per-entry contracts, complex-first ordering, kind-grouped examples and helpers). |
| `src/domain/rules/knowledge_base.py` | 2 | edit (minor) | Kind-grouped rendering for both blocks: per-kind sections built from each entry's retrieved context via the existing `render_examples` / `render_helpers` (a thin wrapper suffices; `ExampleEntry.rule_kind` is the examples heading source, the owning entry's kind is the helpers heading source). |

No new modules, no new dependencies (`concurrent.futures` is stdlib), no
parser / validator / writer / KB-ingest changes, no corpus changes.

---

## 9. Verification

### Phase 1

1. **Concurrency safety.** Node-level test with a stub generator that
   records thread identity and sleeps: calls across multiple forms
   (form-level singles and field batches) overlap in time; `forms_json`
   mutations and `parse_warnings` ordering are identical to a
   `AVNI_RULES_MAX_CONCURRENCY=1` run.
2. **Prompt equivalence.** With a stub capturing invocations, the set of
   (system, user) prompts sent under the pool is byte-identical to the
   pre-change sequential code.
3. **Worker failure isolation.** One stubbed call raising yields
   `_empty` + namespaced warning for that rule only; every other form's
   results land normally.
4. **Telemetry.** Logs carry `path=single` / `path=field_batch` with
   form and kind; the summary line reports per-path counts.

### Phase 2

5. **Prompt snapshot.** For a fixture form with all four kinds,
   `build_form_batch_user_prompt` renders: complex-first entry order,
   inline contract per entry, one shared context block, examples and
   helpers under per-kind headings (a helper retrieved for two entries
   appears under both). Snapshot-tested.
6. **Join correctness.** `generate_form_batch` with a stubbed model
   returning items out of order / with a missing kind / with a duplicate
   kind maps results to the right specs, falls back for the missing
   kind, and warns on the duplicate.
7. **Fallback.** Stub the batch model to raise; assert every entry is
   retried through `generate(spec, context=ctx)` exactly once and
   `path=fallback` is logged. Stub both to fail; assert canonical
   `_empty` results and namespaced warnings identical in shape to
   today's.
8. **Kill switch.** `AVNI_RULES_FORM_BATCH=0` produces per-(form, kind)
   calls with byte-identical prompts to the Phase 1 code (assert via
   stub capture).
9. **End-to-end (live) — the gate.** Compare the batch path's validator
   rejection rate against the Phase 1 `path=single` baseline collected
   from real bundles. Rejection-rate regression beyond noise blocks
   rollout — flip the kill switch and investigate prompts before
   proceeding.

---

## 10. Out of scope (recap)

- One merged per-form call spanning field-level and form-level rules
  (§3.2 — truncation blast radius, tail degradation, rubric dilution).
- Cross-form per-kind batching (cross-form symbol leakage).
- Anthropic Message Batches API.
- Any change to single-rule generation, chat tools, validator,
  `bundle_editor`, KB ingest, or the curated corpora.
- Retry-with-repair loops on validator rejection (a rejected batch item
  stays rejected; only transport/parse failures trigger fallback).

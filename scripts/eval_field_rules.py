"""Eval: compare batched Sonnet vs batched Haiku for field-level rule generation.

Both use the same batch-per-form structure and the same KB contexts.
Scores each generated rule against expected JS from rules_ai_automation.xlsx
using heuristic checks (parse, validator, concept overlap) and LLM-as-judge.

Usage:
    python scripts/eval_field_rules.py \\
        "resources/input/astitva/Astitva Modelling (1).xlsx" \\
        "resources/input/astitva/Astitva Nourish Program Forms (1).xlsx"
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import esprima
import openpyxl
from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.outputs import LLMResult
from pydantic import BaseModel, Field as PField

from domain.parser import parse_scoping_docs
from domain.rules.generator import RuleGenerator, MODEL, _FIELD_BATCH_MODEL
from domain.rules.rule_spec import RuleSpec, RuleKind
from domain.rules.validator import validate_and_decide, _walk
from pipeline.nodes import _build_rule_spec

logging.basicConfig(level=logging.WARNING)

_RULES_XLSX = Path("resources/rules/rules_ai_automation.xlsx")
_SHEET = "Field rules"
_JUDGE_MODEL = _FIELD_BATCH_MODEL  # Haiku — cheap, fast, adequate for comparison

# Cost per 1M tokens (input, output) — update if pricing changes
_MODEL_COST: dict[str, tuple[float, float]] = {
    MODEL:              (3.00, 15.00),   # Sonnet 4.6
    _FIELD_BATCH_MODEL: (0.25,  1.25),   # Haiku 4.5
}


class _TokenTracker(BaseCallbackHandler):
    """Accumulates input/output token counts across all LLM calls."""

    def __init__(self) -> None:
        super().__init__()
        self.input_tokens = 0
        self.output_tokens = 0

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        for generations in response.generations:
            for gen in generations:
                usage = getattr(gen, "generation_info", None) or {}
                self.input_tokens  += usage.get("input_tokens",  0)
                self.output_tokens += usage.get("output_tokens", 0)

    def cost_usd(self, model: str) -> float:
        input_price, output_price = _MODEL_COST.get(model, (0.0, 0.0))
        return (
            self.input_tokens  / 1_000_000 * input_price
            + self.output_tokens / 1_000_000 * output_price
        )


# ── Expected-output loading & matching ────────────────────────────────────────

def _normalise(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _load_expected(org: str) -> dict[tuple[str, str], str]:
    """Load (form_name, field_name) → expected_js for one org."""
    wb = openpyxl.load_workbook(_RULES_XLSX, read_only=True, data_only=True)
    ws = wb[_SHEET]
    out: dict[tuple[str, str], str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] != org:
            continue
        _, _, form, fname, js = row
        if form and fname and js:
            out[(str(form).strip(), str(fname).strip())] = str(js).strip()
    wb.close()
    return out


def _word_set(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def _match_form(generated_name: str, expected_names: list[str]) -> str | None:
    """Fuzzy-match a parsed form name to an expected form name.

    Handles Excel sheet name truncation (31 chars) and minor wording differences.
    Scores by word-level Jaccard so 'HCCM Distribution for Child' correctly
    prefers 'HCCM Distribution Child' over 'HCCM Distribution'.
    """
    gn = _normalise(generated_name)
    gw = _word_set(generated_name)

    for en in expected_names:
        if _normalise(en) == gn:
            return en

    best, best_score = None, 0.0
    for en in expected_names:
        ew = _word_set(en)
        union = gw | ew
        if not union:
            continue
        score = len(gw & ew) / len(union)
        if score > best_score:
            best, best_score = en, score

    return best if best_score >= 0.4 else None


def _extract_concepts(js: str) -> set[str]:
    if not js:
        return set()
    try:
        tree = esprima.parseScript(js)
    except Exception:
        return set()
    concepts: set[str] = set()
    _walk(tree, concepts, set())
    return concepts


# ── LLM-as-judge ─────────────────────────────────────────────────────────────

class _JudgeItem(BaseModel):
    logic_direction: Literal["correct", "inverted", "different"]
    condition_accuracy: Literal["exact", "close", "wrong"]
    completeness: Literal["full", "partial", "missing"]
    verdict: Literal["correct", "partial", "wrong"]
    reason: str


class _JudgeBatchOutput(BaseModel):
    verdicts: list[_JudgeItem] = PField(default_factory=list)


_JUDGE_SYSTEM = """\
You are evaluating auto-generated Avni field-visibility rules in JavaScript.

You will be given the bundle context (form type, entity param, available concepts
and their answer lists) that the generator had access to. Use this to judge whether
the generated rule is correct — not just structurally similar to the reference.

Key context rules:
- The entity param must match the form type:
    IndividualProfile / Encounter           → individual / encounter
    ProgramEnrolment / ProgramExit          → programEnrolment
    ProgramEncounter / ProgramEncounterCancellation → programEncounter
- Concept name accessors must match the entity chain:
    valueInEncounter / valueInRegistration / valueInEnrolment / valueInExit
  A rule that uses the wrong accessor for the form type is WRONG even if the
  concept name is correct.
- Answer strings must match CONCEPT_ANSWERS exactly. A close-but-wrong answer
  string is condition_accuracy=close, not exact.

Score each field:

logic_direction:
  correct   — show/hide condition points in the same direction as the reference
  inverted  — condition is logically inverted (shows when should hide, or vice versa)
  different — fundamentally different condition or entity

condition_accuracy:
  exact — same concept name(s), same answer value(s), correct accessor for form type
  close — right concept and direction, but wrong answer string or minor accessor mismatch
  wrong — different concept, wrong entity chain, or completely different condition

completeness:
  full    — handles all cases the reference handles
  partial — handles the main case but misses edge cases or secondary conditions
  missing — core logic is absent or JS is empty

verdict:
  correct — logic_direction=correct AND condition_accuracy=exact/close AND completeness=full/partial
  partial — logic_direction=correct but condition_accuracy=wrong OR completeness=missing
  wrong   — logic_direction=inverted/different OR entity param mismatch

Return one entry per field in the same order as the FIELDS block.
"""

_JUDGE_USER_TEMPLATE = """\
FORM CONTEXT
Form type  : {form_type}
Entity param: {entity_param}
Subject type: {subject_type}
Program     : {program}
Encounter   : {encounter_type}

AVAILABLE_CONCEPTS
{available_concepts}

CONCEPT_ANSWERS
{concept_answers}

FIELDS
{fields_block}
"""


def _format_concept_answers(answers: dict[str, list[str]]) -> str:
    if not answers:
        return "(none)"
    lines: list[str] = []
    for concept, opts in answers.items():
        lines.append(f"- {concept}")
        for opt in opts:
            lines.append(f'    * "{opt}"')
    return "\n".join(lines)


def _build_judge_prompt(
    entries: list[tuple[str, str, str, str]],
    shared_spec: RuleSpec,
) -> str:
    """entries: list of (field_name, intent, reference_js, generated_js)."""
    from domain.rules.prompts import entity_param_for_form_type
    blocks: list[str] = []
    for i, (fname, intent, ref_js, gen_js) in enumerate(entries, 1):
        blocks.append(
            f"[{i}] {fname}\n"
            f"INTENT: {intent}\n\n"
            f"REFERENCE:\n{ref_js}\n\n"
            f"GENERATED:\n{gen_js or '(empty)'}"
        )
    return _JUDGE_USER_TEMPLATE.format(
        form_type=shared_spec.form_type,
        entity_param=entity_param_for_form_type(shared_spec.form_type),
        subject_type=shared_spec.subject_type or "-",
        program=shared_spec.program or "-",
        encounter_type=shared_spec.encounter_type or "-",
        available_concepts="\n".join(f"- {c}" for c in shared_spec.available_concepts) or "(none)",
        concept_answers=_format_concept_answers(shared_spec.concept_answers),
        fields_block="\n\n---\n\n".join(blocks),
    )


@dataclass
class JudgeVerdict:
    logic_direction: str
    condition_accuracy: str
    completeness: str
    verdict: str
    reason: str


def run_judge(
    sonnet_stats: RunStats,
    haiku_stats: RunStats,
    spec_by_key: dict[tuple[str, str], RuleSpec],
) -> tuple[dict[tuple[str, str], JudgeVerdict], dict[tuple[str, str], JudgeVerdict]]:
    """Judge both models' outputs against the reference, batched per form.

    Returns (sonnet_verdicts, haiku_verdicts) keyed by (form, field_name).
    Only fields with a reference rule are judged.
    """
    judge_model = ChatAnthropic(
        model=_JUDGE_MODEL, max_tokens=4096,
    ).with_structured_output(_JudgeBatchOutput)

    system_msg = SystemMessage(content=_JUDGE_SYSTEM)

    sonnet_by_key = {(r.form, r.field): r for r in sonnet_stats.results}
    haiku_by_key  = {(r.form, r.field): r for r in haiku_stats.results}

    forms: dict[str, list[str]] = {}
    for r in sonnet_stats.results:
        if r.has_reference:
            forms.setdefault(r.form, []).append(r.field)

    sonnet_verdicts: dict[tuple[str, str], JudgeVerdict] = {}
    haiku_verdicts:  dict[tuple[str, str], JudgeVerdict] = {}

    for form, field_names in forms.items():
        # Use the RuleSpec of the first matched field as shared form context
        shared_spec = next(
            (spec_by_key[(form, fn)] for fn in field_names if (form, fn) in spec_by_key),
            None,
        )
        if shared_spec is None:
            continue

        for model_label, by_key, verdicts in [
            ("Sonnet", sonnet_by_key, sonnet_verdicts),
            ("Haiku",  haiku_by_key,  haiku_verdicts),
        ]:
            entries = []
            for fname in field_names:
                r = by_key.get((form, fname))
                if r is None or not r.has_reference:
                    continue
                entries.append((fname, r.intent, r.expected_js, r.js))

            if not entries:
                continue

            try:
                output: _JudgeBatchOutput = judge_model.invoke([
                    system_msg,
                    HumanMessage(content=_build_judge_prompt(entries, shared_spec)),
                ])
            except Exception as exc:
                logging.warning(f"Judge call failed for {form} / {model_label}: {exc}")
                continue

            for (fname, _, _, _), item in zip(entries, output.verdicts):
                verdicts[(form, fname)] = JudgeVerdict(
                    logic_direction=item.logic_direction,
                    condition_accuracy=item.condition_accuracy,
                    completeness=item.completeness,
                    verdict=item.verdict,
                    reason=item.reason,
                )

    return sonnet_verdicts, haiku_verdicts


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class FieldResult:
    form: str
    section: str
    field: str
    intent: str
    js: str
    parse_ok: bool
    validator_ok: bool
    confidence: str
    notes: str
    elapsed_s: float
    expected_js: str = ""
    judge: JudgeVerdict | None = None

    @property
    def has_reference(self) -> bool:
        return bool(self.expected_js)

    @property
    def concept_overlap(self) -> float | None:
        if not self.has_reference:
            return None
        gen = _extract_concepts(self.js)
        exp = _extract_concepts(self.expected_js)
        if not exp:
            return None
        return len(gen & exp) / len(exp) if gen else 0.0


@dataclass
class RunStats:
    model: str
    results: list[FieldResult] = field(default_factory=list)
    total_s: float = 0.0
    api_calls: int = 0
    tracker: _TokenTracker = field(default_factory=_TokenTracker)

    @property
    def estimated_cost_usd(self) -> float:
        return self.tracker.cost_usd(self.model)

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def parse_rate(self) -> float:
        return sum(r.parse_ok for r in self.results) / self.n if self.n else 0.0

    @property
    def validator_rate(self) -> float:
        return sum(r.validator_ok for r in self.results) / self.n if self.n else 0.0

    @property
    def js_rate(self) -> float:
        return sum(bool(r.js) for r in self.results) / self.n if self.n else 0.0

    def confidence_counts(self) -> dict[str, int]:
        out: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
        for r in self.results:
            out[r.confidence] = out.get(r.confidence, 0) + 1
        return out

    def avg_js_len(self) -> float:
        lens = [len(r.js) for r in self.results if r.js]
        return sum(lens) / len(lens) if lens else 0.0

    def avg_concept_overlap(self) -> float | None:
        scores = [r.concept_overlap for r in self.results if r.concept_overlap is not None]
        return sum(scores) / len(scores) if scores else None

    def judge_counts(self) -> dict[str, int]:
        out: dict[str, int] = {"correct": 0, "partial": 0, "wrong": 0, "unjudged": 0}
        for r in self.results:
            if not r.has_reference:
                continue
            if r.judge is None:
                out["unjudged"] += 1
            else:
                out[r.judge.verdict] = out.get(r.judge.verdict, 0) + 1
        return out


# ── Run helpers ───────────────────────────────────────────────────────────────

def _parse_ok(js: str) -> bool:
    if not js:
        return False
    try:
        esprima.parseScript(js)
        return True
    except Exception:
        return False


def run_batch(
    generator: RuleGenerator,
    model_label: str,
    pending: list[tuple[str, str, str, RuleSpec]],
    contexts: list,
    expected: dict[tuple[str, str], str],
    form_name_map: dict[str, str],
    tracker: _TokenTracker,
) -> RunStats:
    stats = RunStats(model=model_label, tracker=tracker)

    # Attach tracker to the field batch model so every call is recorded
    generator._field_batch_model = generator._field_batch_model.with_config(
        {"callbacks": [tracker]}
    )

    by_form: dict[str, list[tuple[str, str, str, RuleSpec, object]]] = {}
    for (form, section, fname, spec), ctx in zip(pending, contexts):
        by_form.setdefault(form, []).append((form, section, fname, spec, ctx))

    t0 = time.perf_counter()
    for form_entries in by_form.values():
        batch_input = [
            (fname, section, spec, ctx)
            for (_, section, fname, spec, ctx) in form_entries
        ]
        t_batch = time.perf_counter()
        results = generator.generate_field_batch(batch_input)
        elapsed = time.perf_counter() - t_batch
        per_field = elapsed / len(batch_input) if batch_input else 0.0
        stats.api_calls += 1

        for (form, section, fname, spec, _), result in zip(form_entries, results):
            ok, _ = validate_and_decide(result, spec)
            exp_form = form_name_map.get(form)
            exp_js = expected.get((exp_form, fname), "") if exp_form else ""
            stats.results.append(FieldResult(
                form=form, section=section, field=fname, intent=spec.intent,
                js=result.js, parse_ok=_parse_ok(result.js),
                validator_ok=ok, confidence=result.confidence,
                notes="; ".join(result.warnings), elapsed_s=per_field,
                expected_js=exp_js,
            ))

    stats.total_s = time.perf_counter() - t0
    return stats


def attach_verdicts(
    stats: RunStats,
    verdicts: dict[tuple[str, str], JudgeVerdict],
) -> None:
    for r in stats.results:
        r.judge = verdicts.get((r.form, r.field))


# ── Output ────────────────────────────────────────────────────────────────────

def print_summary(sonnet: RunStats, haiku: RunStats) -> None:
    w = 32

    def row(label: str, sv: str, hv: str) -> None:
        print(f"  {label:<{w}} {sv:<24} {hv}")

    print("\n" + "=" * 80)
    print(f"  {'METRIC':<{w}} {'SONNET (batched)':<24} {'HAIKU (batched)'}")
    print("=" * 80)
    row("Fields evaluated", str(sonnet.n), str(haiku.n))
    row("API calls (generation)", str(sonnet.api_calls), str(haiku.api_calls))
    row("Total time (s)", f"{sonnet.total_s:.1f}", f"{haiku.total_s:.1f}")
    row("Avg time / field (s)",
        f"{sonnet.total_s/sonnet.n:.2f}" if sonnet.n else "-",
        f"{haiku.total_s/haiku.n:.2f}" if haiku.n else "-")
    print("-" * 80)
    row("JS non-empty rate", f"{sonnet.js_rate:.0%}", f"{haiku.js_rate:.0%}")
    row("Parse valid rate", f"{sonnet.parse_rate:.0%}", f"{haiku.parse_rate:.0%}")
    row("Validator pass rate", f"{sonnet.validator_rate:.0%}", f"{haiku.validator_rate:.0%}")
    print("-" * 80)
    sc, hc = sonnet.confidence_counts(), haiku.confidence_counts()
    for level in ("high", "medium", "low"):
        row(f"Confidence: {level}",
            f"{sc[level]} ({sc[level]/sonnet.n:.0%})" if sonnet.n else "-",
            f"{hc[level]} ({hc[level]/haiku.n:.0%})" if haiku.n else "-")
    print("-" * 80)
    matched_s = sum(1 for r in sonnet.results if r.has_reference)
    matched_h = sum(1 for r in haiku.results if r.has_reference)
    so = sonnet.avg_concept_overlap()
    ho = haiku.avg_concept_overlap()
    row("Fields with reference", f"{matched_s}/{sonnet.n}", f"{matched_h}/{haiku.n}")
    row("Concept overlap (vs ref)", f"{so:.0%}" if so is not None else "-",
        f"{ho:.0%}" if ho is not None else "-")
    print("-" * 80)
    sjc, hjc = sonnet.judge_counts(), haiku.judge_counts()
    judged_s = matched_s - sjc.get("unjudged", 0)
    judged_h = matched_h - hjc.get("unjudged", 0)
    for verdict in ("correct", "partial", "wrong"):
        row(f"Judge: {verdict}",
            f"{sjc[verdict]} ({sjc[verdict]/judged_s:.0%})" if judged_s else "-",
            f"{hjc[verdict]} ({hjc[verdict]/judged_h:.0%})" if judged_h else "-")
    row("Avg JS length (chars)", f"{sonnet.avg_js_len():.0f}", f"{haiku.avg_js_len():.0f}")
    print("-" * 80)
    row("Input tokens",  str(sonnet.tracker.input_tokens),  str(haiku.tracker.input_tokens))
    row("Output tokens", str(sonnet.tracker.output_tokens), str(haiku.tracker.output_tokens))
    row("Estimated cost (USD)",
        f"${sonnet.estimated_cost_usd:.4f}", f"${haiku.estimated_cost_usd:.4f}")
    print("=" * 80)


def print_field_table(sonnet: RunStats, haiku: RunStats) -> None:
    by_key = {(r.form, r.field): r for r in sonnet.results}

    print(f"\n{'FORM':<28} {'FIELD':<30} {'S-VAL':<8} {'H-VAL':<8} "
          f"{'S-JUDGE':<10} {'H-JUDGE':<10} {'OVERLAP'}")
    print("-" * 104)
    for hr in haiku.results:
        sr = by_key.get((hr.form, hr.field))
        if sr is None:
            continue
        s_val = "OK" if sr.validator_ok else "FAIL"
        h_val = "OK" if hr.validator_ok else "FAIL"
        s_judge = sr.judge.verdict[0].upper() + sr.judge.verdict[1:] if sr.judge else "-"
        h_judge = hr.judge.verdict[0].upper() + hr.judge.verdict[1:] if hr.judge else "-"
        overlap = (f"{sr.concept_overlap:.0%}" if sr.concept_overlap is not None else "-")
        differ = " <" if (sr.validator_ok != hr.validator_ok or
                          (sr.judge and hr.judge and sr.judge.verdict != hr.judge.verdict)) else ""
        print(f"{sr.form[:28]:<28} {sr.field[:30]:<30} {s_val:<8} {h_val:<8} "
              f"{s_judge:<10} {h_judge:<10} {overlap}{differ}")

    # Fields where judge disagrees — show reasons
    diffs = [
        (sr, hr)
        for hr in haiku.results
        if (sr := by_key.get((hr.form, hr.field))) is not None
        and sr.judge and hr.judge
        and sr.judge.verdict != hr.judge.verdict
    ]
    if diffs:
        print(f"\nJUDGE DISAGREEMENTS ({len(diffs)} fields):")
        print("-" * 104)
        for sr, hr in diffs:
            print(f"  {sr.form[:28]} / {sr.field}")
            print(f"    Sonnet ({sr.judge.verdict}): {sr.judge.reason}")
            print(f"    Haiku  ({hr.judge.verdict}): {hr.judge.reason}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    file_paths = [Path(p) for p in sys.argv[1:]]

    print(f"Parsing {len(file_paths)} file(s)…")
    spec, _ = parse_scoping_docs(file_paths)

    available_encounter_types = [et.name for et in spec.encounter_types]
    available_programs = [p.name for p in spec.programs]

    field_level_forms = [
        f for f in spec.forms
        if any(fld.rule_intent for s in (f.sections or []) for fld in (s.fields or []))
    ]

    pending: list[tuple[str, str, str, RuleSpec]] = []
    spec_by_key: dict[tuple[str, str], RuleSpec] = {}
    for form_spec in field_level_forms:
        for section in (form_spec.sections or []):
            for fld in (section.fields or []):
                if not fld.rule_intent:
                    continue
                rule_spec = _build_rule_spec(
                    form_spec, RuleKind.FORM_ELEMENT, fld.rule_intent,
                    available_encounter_types, available_programs,
                    all_forms=spec.forms,
                )
                pending.append((form_spec.name, section.name, fld.name, rule_spec))
                spec_by_key[(form_spec.name, fld.name)] = rule_spec

    print(f"Found {len(pending)} fields across {len(field_level_forms)} forms.")

    print("Loading expected rules from rules_ai_automation.xlsx…")
    expected = _load_expected("Astitva")
    expected_form_names = sorted(set(k[0] for k in expected))
    generated_form_names = sorted(set(f for f, _, _, _ in pending))

    form_name_map: dict[str, str] = {}
    for gf in generated_form_names:
        matched = _match_form(gf, expected_form_names)
        form_name_map[gf] = matched
        status = f"→ {matched}" if matched else "→ (no match)"
        print(f"  {gf:<40} {status}")

    print(f"\n{len(expected)} expected rules loaded, "
          f"{sum(1 for v in form_name_map.values() if v)} forms matched.")

    print("\nRetrieving KB contexts (one Voyage batch, shared)…")
    sonnet_gen = RuleGenerator(field_batch_model=MODEL)
    if not sonnet_gen.is_available():
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    specs = [s for _, _, _, s in pending]
    try:
        contexts = sonnet_gen.kb.retrieve_batch(specs)
    except Exception as exc:
        print(f"KB retrieve failed: {exc}")
        sys.exit(1)

    haiku_gen = RuleGenerator(kb=sonnet_gen.kb, field_batch_model=_FIELD_BATCH_MODEL)

    sonnet_tracker = _TokenTracker()
    haiku_tracker  = _TokenTracker()

    print(f"\nRunning Sonnet ({MODEL}) batched — {len(field_level_forms)} calls…")
    sonnet_stats = run_batch(
        sonnet_gen, MODEL, pending, contexts, expected, form_name_map, sonnet_tracker,
    )

    print(f"Running Haiku ({_FIELD_BATCH_MODEL}) batched — {len(field_level_forms)} calls…")
    haiku_stats = run_batch(
        haiku_gen, _FIELD_BATCH_MODEL, pending, contexts, expected, form_name_map, haiku_tracker,
    )

    judged_count = sum(1 for r in sonnet_stats.results if r.has_reference)
    print(f"\nRunning LLM-as-judge ({_JUDGE_MODEL}) — "
          f"{judged_count} fields × 2 models, batched per form…")
    sonnet_verdicts, haiku_verdicts = run_judge(sonnet_stats, haiku_stats, spec_by_key)
    attach_verdicts(sonnet_stats, sonnet_verdicts)
    attach_verdicts(haiku_stats, haiku_verdicts)

    print_summary(sonnet_stats, haiku_stats)
    print_field_table(sonnet_stats, haiku_stats)


if __name__ == "__main__":
    main()

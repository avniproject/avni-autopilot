"""
Interactive chat using LangGraph's prebuilt ReAct agent + ChatAnthropic.

Run from project root:
    python src/chat.py

Tools backed by `src/pipeline.py`:
  - generate_bundle(org, user_instructions=None)
        Start a bundle run. Streams the pipeline graph; if the LLM enrichment
        node calls `interrupt()` to ask the user for confirmation, the tool
        returns the pending changes (with a thread_id) and the agent presents
        them. Otherwise returns a successful bundle summary.
  - resume_bundle(thread_id, resolutions)
        Continue a paused bundle run with the user's confirmation answers.
        `resolutions` is a dict keyed by change_id; values are "yes"/"no"/
        "edit:<new_value>".

Per-conversation state is held by a MemorySaver checkpointer keyed by
thread_id (chat agent's own state). The bundle pipeline keeps its own
checkpointer keyed by bundle thread_id so interrupted runs can be resumed.

REPL slash commands (no token cost):
  /quit, /q     exit
  /clear        start a new thread (fresh memory)
  /history      print the message history for the current thread
  /graph        print the chat agent graph + bundle pipeline graph
  /help         show command list
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

# ── Path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Load .env from the project root
_env_file = os.path.join(_ROOT, ".env")
if os.path.exists(_env_file):
    with open(_env_file) as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from langchain_anthropic import ChatAnthropic  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402
from langgraph.types import Command  # noqa: E402

from pipeline import build_graph as build_pipeline_graph  # noqa: E402
from pipeline import initial_state as pipeline_initial_state  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")
# Surface our own pipeline/enricher logs at INFO while keeping noisy 3rd-party
# packages (langchain, anthropic, httpx) at WARNING.
logging.getLogger("pipeline").setLevel(logging.INFO)
logging.getLogger("enricher").setLevel(logging.INFO)
logging.getLogger("llm_helper").setLevel(logging.INFO)

INPUT_ROOT = os.path.join(_ROOT, "resources", "input")
OUTPUT_ROOT = os.path.join(_ROOT, "resources", "output")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192


# ── Pipeline (shared, kept hot across tool calls) ────────────────────────────

# One compiled pipeline graph reused across `generate_bundle` and
# `resume_bundle`. Its in-memory checkpointer keeps per-thread state so a
# paused run can be resumed by thread_id.
_pipeline_graph = build_pipeline_graph()


def _summarize(state: dict) -> dict:
    """Build the success-summary dict the chat agent reports."""
    cancel_count = sum(
        1 for f in state.get("mapping_specs", [])
        if "Cancellation" in f["name"]
    )
    main_forms = len(state.get("forms_json", [])) - cancel_count
    return {
        "status": "done",
        "org": state.get("org_name", ""),
        "subject_types": len(state.get("subject_types_json", [])),
        "programs": len(state.get("programs_json", [])),
        "encounter_types": len(state.get("encounter_types_json", [])),
        "main_forms": main_forms,
        "cancellation_forms": cancel_count,
        "concepts": len(state.get("concepts_json", [])),
        "form_mappings": len(state.get("form_mappings_json", [])),
        "zip_path": state.get("zip_path", ""),
        "applied_changes": state.get("applied_changes", []),
        "enrich_warnings": state.get("enrich_warnings", []),
        "parse_warnings": state.get("parse_warnings", []),
        "errors": state.get("errors", []),
    }


def _run_with_interrupt_handling(input_or_command, config: dict) -> dict:
    """Invoke the pipeline; if it interrupts, return the pending payload.

    The pipeline graph uses LangGraph's `interrupt()` in `enrich_with_llm`.
    On invoke that returns a dict whose `__interrupt__` key carries the
    interrupt info, the caller (the chat agent) presents it to the user.
    Once the user responds, the agent calls `resume_bundle` with the same
    thread_id and the pipeline resumes from the interrupt point.
    """
    result = _pipeline_graph.invoke(input_or_command, config=config)
    interrupts = result.get("__interrupt__")
    if interrupts:
        # interrupts is a tuple of Interrupt objects; grab the first.
        first = interrupts[0]
        payload = getattr(first, "value", None)
        if payload is None and isinstance(first, dict):
            payload = first.get("value")
        return {
            "status": "needs_confirmation",
            "thread_id": config["configurable"]["thread_id"],
            "payload": payload or {},
        }
    return _summarize(result)


# ── Tools ─────────────────────────────────────────────────────────────────────


@tool
def generate_bundle(org: str, user_instructions: str | None = None) -> dict:
    """Start an Avni bundle generation for a specific org.

    Reads all .xlsx files in resources/input/<org>/ and produces
    resources/output/<org>/<Org>.zip. The deterministic parser runs first,
    then Claude (Haiku) enriches each form's spec — fixing option splits,
    inferring missing data types, predicting min/max bounds, etc.

    If any refinement requires user confirmation (long-name shortening,
    duplicate-field disambiguation, min/max bounds, user-driven add/remove),
    the pipeline pauses and this tool returns a `needs_confirmation` payload.
    The agent should present the proposed changes to the user, gather their
    answers as a `{change_id: "yes"|"no"|"edit:<value>"}` dict, then call
    `resume_bundle(thread_id, resolutions)` to finish the run.

    Args:
        org: Org subfolder name, case-insensitive (e.g. 'srijan').
        user_instructions: Optional natural-language instruction passed to
            the LLM enrichment step (e.g. "also add a Sponsor field to
            Pregnancy Enrolment").
    """
    org = org.strip().lower()
    input_dir = os.path.join(INPUT_ROOT, org)
    output_dir = os.path.join(OUTPUT_ROOT, org)
    if not os.path.isdir(input_dir):
        return {
            "status": "error",
            "error": f"Input dir not found: {input_dir}",
        }

    thread_id = f"bundle-{org}-{int(time.time())}"
    config = {"configurable": {"thread_id": thread_id}}
    initial = pipeline_initial_state(org, input_dir, output_dir, user_instructions)
    return _run_with_interrupt_handling(initial, config)


@tool
def resume_bundle(thread_id: str, resolutions: dict[str, str]) -> dict:
    """Resume a paused bundle run after the user confirms pending changes.

    Args:
        thread_id: The id returned in the prior `generate_bundle` /
            `resume_bundle` `needs_confirmation` response.
        resolutions: A dict mapping each `change_id` to one of:
            "yes"               — apply Claude's proposed `after`
            "no"                — skip this change
            "edit:<new_value>"  — apply with a user-provided override
    """
    print(f"[debug:resume_bundle] thread_id={thread_id} resolutions={resolutions}")
    config = {"configurable": {"thread_id": thread_id}}
    return _run_with_interrupt_handling(Command(resume=resolutions), config)


TOOLS = [generate_bundle, resume_bundle]


# ── Agent build ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an Avni bundle generator assistant. You help the user
produce Avni configuration bundle ZIPs from modelling and scoping Excel
documents.

Available tools:
  - generate_bundle(org, user_instructions=None) — start a bundle run.
    May return either a final summary (status="done") or a pause
    (status="needs_confirmation") with a list of proposed changes.
  - resume_bundle(thread_id, resolutions) — continue a paused run with
    the user's confirmation answers. resolutions is a dict mapping each
    change_id to "yes", "no", or "edit:<new_value>".

Behavior:
  - When the user asks to generate, call `generate_bundle`.
  - If it returns status="needs_confirmation", present the proposed
    changes one at a time to the user (form, field, kind, before, after,
    reason). Collect their decisions, then call `resume_bundle` with the
    same thread_id and a resolutions dict.
  - On status="done", report counts: subject types, programs, encounter
    types, forms (main + cancellation), concepts, form mappings, plus any
    warnings or errors.
  - When the user attaches an instruction like "also add a Sponsor field to
    Pregnancy Enrolment", pass it through verbatim as user_instructions.
  - Keep replies concise. Use markdown tables only when comparing counts."""


def build_agent():
    model = ChatAnthropic(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
    )
    checkpointer = MemorySaver()
    return create_react_agent(
        model=model,
        tools=TOOLS,
        prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )


# ── REPL ──────────────────────────────────────────────────────────────────────


def _stream_turn(agent, user_input: str, config: dict) -> None:
    """Send one user message, stream agent text + tool calls, return on completion."""
    started_text = False
    seen_tool_calls: set[str] = set()

    for mode, chunk in agent.stream(
        {"messages": [HumanMessage(content=user_input)]},
        config=config,
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            msg_chunk, _metadata = chunk
            if isinstance(msg_chunk, AIMessageChunk):
                text = _extract_text(msg_chunk.content)
                if text:
                    if not started_text:
                        print("\nagent> ", end="", flush=True)
                        started_text = True
                    print(text, end="", flush=True)

        elif mode == "updates":
            for node, state in chunk.items():
                if node != "agent" or not state:
                    continue
                for msg in state.get("messages", []):
                    if isinstance(msg, AIMessage) and msg.tool_calls:
                        if started_text:
                            print()
                            started_text = False
                        for tc in msg.tool_calls:
                            tc_id = tc.get("id") or ""
                            if tc_id in seen_tool_calls:
                                continue
                            seen_tool_calls.add(tc_id)
                            args = json.dumps(tc.get("args", {}), default=str)
                            if len(args) > 80:
                                args = args[:77] + "..."
                            print(f"  ⚙ {tc.get('name', '?')}({args})")

    if started_text:
        print()


def _extract_text(content) -> str:
    """Anthropic content can be str or list of {type, text}. Return concatenated text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text") or ""
                if t:
                    out.append(t)
        return "".join(out)
    return ""


def _handle_slash(cmd: str, agent, config: dict) -> None:
    cmd = cmd.strip().lower()
    if cmd in ("quit", "exit", "q"):
        print("bye.")
        sys.exit(0)
    if cmd == "clear":
        new_id = f"thread-{int(time.time())}"
        config["configurable"]["thread_id"] = new_id
        print(f"(new thread: {new_id})")
        return
    if cmd == "history":
        try:
            snapshot = agent.get_state(config)
        except Exception as exc:  # noqa: BLE001
            print(f"(could not load state: {exc})")
            return
        msgs = (snapshot.values or {}).get("messages", [])
        if not msgs:
            print("(empty)")
            return
        for i, m in enumerate(msgs):
            role = type(m).__name__
            preview = _extract_text(m.content) or repr(m.content)[:120]
            preview = preview[:120].replace("\n", " ")
            print(f"  {i:2d} {role:15s} {preview}")
        return
    if cmd == "graph":
        _print_graphs(agent)
        return
    if cmd == "help":
        print("Commands:")
        print("  /quit, /q     exit")
        print("  /clear        start a new thread (fresh memory)")
        print("  /history      list message history for the current thread")
        print("  /graph        print the chat agent graph + bundle pipeline graph")
        print("  /help         this help")
        return
    print(f"(unknown command: /{cmd}; try /help)")


_OUTER_GRAPH = """\
   ┌───────────┐        ┌───────┐
   │ __start__ │───────▶│ agent │◀────────────┐
   └───────────┘        └───┬───┘             │
                            │                 │
                  ┌─────────┴─────────┐       │
              end_turn            tool_calls  │
                  │                   │       │
                  ▼                   ▼       │
            ┌─────────┐           ┌───────┐   │
            │ __end__ │           │ tools │───┘
            └─────────┘           └───────┘
"""

_INNER_GRAPH = """\
              ┌───────────┐
              │ __start__ │
              └─────┬─────┘
                    ▼
          ┌────────────────┐
          │ discover_files │─────abort──────┐
          └───────┬────────┘                │
                  │ continue                │
                  ▼                         │
         ┌─────────────────┐                │
         │ parse_documents │─────abort──────┤
         └───────┬─────────┘                │
                 │ continue                 │
                 ▼                          │
        ┌─────────────────┐                 │
        │ enrich_with_llm │┄┄ interrupt ┄┄┄ │ ┄┄┄▶ caller resumes via Command
        └────────┬────────┘                 │
                 ▼                          │
        ┌───────────────────┐               │
        │ generate_entities │               │
        └─────────┬─────────┘               │
                  ▼                         │
         ┌────────────────┐                 │
         │ generate_forms │                 │
         └───────┬────────┘                 │
                 ▼                          │
      ┌────────────────────────┐            │
      │ generate_form_mappings │            │
      └───────────┬────────────┘            │
                  ▼                         │
            ┌─────────────┐                 │
            │ package_zip │                 │
            └──────┬──────┘                 │
                   ▼                        │
               ┌─────────┐                  │
               │ __end__ │◀─────────────────┘
               └─────────┘
"""


def _print_graphs(agent) -> None:  # noqa: ARG001  (agent unused; diagrams are static)
    """Print the outer chat ReAct graph and the inner bundle pipeline graph."""
    print("\nOuter — chat ReAct agent (chat.py)")
    print(_OUTER_GRAPH)
    print("Inner — bundle pipeline (pipeline.py)")
    print(_INNER_GRAPH)


def run_chat() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (check .env)")
        sys.exit(1)

    agent = build_agent()
    config: dict = {"configurable": {"thread_id": f"thread-{int(time.time())}"}}

    print("─" * 60)
    print("Avni Bundle Chat (LangGraph ReAct agent)")
    print(f"Model: {MODEL}    /help for commands, /quit to exit")
    print(f"Thread: {config['configurable']['thread_id']}")
    print("─" * 60)

    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            return

        if not user_input:
            continue

        if user_input.startswith("/"):
            _handle_slash(user_input[1:], agent, config)
            continue

        try:
            _stream_turn(agent, user_input, config)
        except KeyboardInterrupt:
            print("\n(interrupted)")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(error: {exc})")


if __name__ == "__main__":
    run_chat()

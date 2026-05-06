"""
Interactive chat using LangGraph's prebuilt ReAct agent + ChatAnthropic.

Run from project root:
    python src/chat.py

The agent has one tool backed by `pipeline.run`:
  - generate_bundle(org)   — run the LangGraph pipeline; returns a summary

Per-conversation state is held by a MemorySaver checkpointer keyed by thread_id,
so the agent automatically remembers earlier turns.

REPL slash commands (no token cost):
  /quit, /q     exit
  /clear        start a new thread (fresh memory)
  /history      print the message history for the current thread
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

from pipeline import run as run_pipeline  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")

INPUT_ROOT = os.path.join(_ROOT, "resources", "input")
OUTPUT_ROOT = os.path.join(_ROOT, "resources", "output")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192


# ── Tools ─────────────────────────────────────────────────────────────────────


@tool
def generate_bundle(org: str) -> dict:
    """Generate the Avni bundle ZIP for a specific org.

    Reads all .xlsx files in resources/input/<org>/ and produces
    resources/output/<org>/<Org>.zip. Returns counts of subject types,
    programs, encounter types, forms (main + cancellation), concepts,
    form mappings, plus any parse warnings or errors.

    Args:
        org: Org subfolder name, case-insensitive (e.g. 'srijan').
    """
    org = org.strip().lower()
    input_dir = os.path.join(INPUT_ROOT, org)
    output_dir = os.path.join(OUTPUT_ROOT, org)
    if not os.path.isdir(input_dir):
        return {"success": False, "error": f"Input dir not found: {input_dir}"}

    result = run_pipeline(org, input_dir, output_dir)

    cancel_count = sum(
        1 for f in result.get("mapping_specs", [])
        if "Cancellation" in f["name"]
    )
    main_forms = len(result.get("forms_json", [])) - cancel_count

    return {
        "success": not result.get("errors"),
        "org": org,
        "subject_types": len(result.get("subject_types_json", [])),
        "programs": len(result.get("programs_json", [])),
        "encounter_types": len(result.get("encounter_types_json", [])),
        "main_forms": main_forms,
        "cancellation_forms": cancel_count,
        "concepts": len(result.get("concepts_json", [])),
        "form_mappings": len(result.get("form_mappings_json", [])),
        "zip_path": result.get("zip_path", ""),
        "warnings": result.get("parse_warnings", []),
        "errors": result.get("errors", []),
    }


TOOLS = [generate_bundle]


# ── Agent build ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an Avni bundle generator assistant. You help the user
produce Avni configuration bundle ZIPs from modelling and scoping Excel
documents.

Available tools:
  - generate_bundle(org): run the deterministic pipeline; returns a summary

Behavior:
  - When the user asks to generate, run the pipeline and report the summary
    clearly: counts of subject types, programs, encounter types, forms (main +
    cancellation), concepts, form mappings, plus any warnings or errors.
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
    if cmd == "help":
        print("Commands:")
        print("  /quit, /q     exit")
        print("  /clear        start a new thread (fresh memory)")
        print("  /history      list message history for the current thread")
        print("  /help         this help")
        return
    print(f"(unknown command: /{cmd}; try /help)")


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

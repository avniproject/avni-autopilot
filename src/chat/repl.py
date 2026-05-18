"""
Interactive chat REPL using LangGraph's prebuilt ReAct agent + ChatAnthropic.

Run from project root:
    python -m chat.repl

Tools backed by `pipeline/` and `domain/bundle_editor.py`:
  - generate_bundle / resume_bundle      — bundle generation + HITL resume
  - list_bundle_fields / edit_bundle_fields  — bundle field editing

Per-conversation state is held by a `MemorySaver` checkpointer keyed by
thread_id (chat agent's own state). The bundle pipeline keeps its own
checkpointer keyed by bundle thread_id so interrupted runs can be resumed.

REPL slash commands (no token cost):
  /quit, /q     exit
  /clear        start a new thread (fresh memory)
  /history      print the message history for the current thread
  /help         show command list
"""

from __future__ import annotations

import json
import os
import sys
import time

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

# Importing `config` triggers .env loading; importing `logging_setup` exposes
# the setup function. Both must run before `build_agent` so ChatAnthropic
# can see ANTHROPIC_API_KEY.
from config import settings  # noqa: F401  (side-effect: .env loaded)
from logging_setup import setup_logging

setup_logging()

from chat.agent import build_agent  # noqa: E402


# ── REPL ─────────────────────────────────────────────────────────────────────


def _stream_turn(agent, user_input: str, config: dict) -> None:
    """Send one user message, stream agent text + tool calls, return on completion."""
    started_text = False
    seen_tool_calls: set[str] = set()
    shown_thinking_hint = False

    # Indicator so the user sees activity while the model is in adaptive
    # thinking mode (no visible text yet). Cleared when actual content arrives.
    print("  (thinking…)", end="", flush=True)

    def _clear_thinking() -> None:
        nonlocal shown_thinking_hint
        if not shown_thinking_hint:
            print("\r" + " " * 14 + "\r", end="", flush=True)
            shown_thinking_hint = True

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
                    _clear_thinking()
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
                        _clear_thinking()
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

    _clear_thinking()
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
    print(f"Model: {settings.model}    /help for commands, /quit to exit")
    print(f"Thread: {config['configurable']['thread_id']}")
    if settings.langsmith_tracing and settings.langsmith_api_key:
        print(f"LangSmith: tracing → project '{settings.langsmith_project}'")
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
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"\n[error] {exc}")


if __name__ == "__main__":
    run_chat()

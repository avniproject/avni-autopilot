"""Unit tests for `src/web/chat_service.py`.

Tests drive the translation logic with a stub agent that emits canned
LangGraph `updates` chunks. The real agent (LLM-backed) is not exercised
here — the wiring is covered manually in Phase 5's end-to-end smoke test.

Usage:
    pytest tests/test_web_chat_service.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from web.chat_service import ChatService
from web.sessions import ChatSession


# ── Stub agent ────────────────────────────────────────────────────────────────


class _StubAgent:
    """astream-compatible stub that yields a fixed list of update chunks."""

    def __init__(self, updates: Iterable[dict[str, Any]]) -> None:
        self._updates = list(updates)

    async def astream(self, _input, *, config, stream_mode):  # noqa: ARG002
        for u in self._updates:
            yield u


def _make_session(tmp_path: Path) -> ChatSession:
    return ChatSession(
        session_id="sid-1",
        org_name="srijan",
        username="alice",
        workdir=tmp_path,
    )


def _collect_published(session: ChatSession) -> list[tuple[str, dict]]:
    """Return (type, data) pairs from the bus, dropping `ts`."""
    out: list[tuple[str, dict]] = []
    for event in session.bus.replay_since(0):
        data = {k: v for k, v in event.data.items() if k != "ts"}
        out.append((event.type, data))
    return out


# ── Agent message + tool call translation ────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_message_with_text_publishes_agent_message(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    updates = [{"agent": {"messages": [AIMessage(content="hello world")]}}]
    svc = ChatService(session, agent_factory=lambda: _StubAgent(updates))
    await svc.send_message("hi")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    assert events == [
        ("agent.message", {"role": "assistant", "content": "hello world"}),
    ]


@pytest.mark.asyncio
async def test_ai_message_with_list_content_concatenates_text_blocks(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    content = [
        {"type": "text", "text": "part 1 "},
        {"type": "thinking", "text": "discard me"},
        {"type": "text", "text": "part 2"},
    ]
    updates = [{"agent": {"messages": [AIMessage(content=content)]}}]
    svc = ChatService(session, agent_factory=lambda: _StubAgent(updates))
    await svc.send_message("hi")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    assert events == [
        ("agent.message", {"role": "assistant", "content": "part 1 part 2"}),
    ]


@pytest.mark.asyncio
async def test_ai_message_with_tool_calls_publishes_one_event_per_call(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "generate_bundle", "args": {"org": "srijan"}, "id": "c1"},
            {"name": "list_bundle_fields", "args": {"bundle_path": "x"}, "id": "c2"},
        ],
    )
    updates = [{"agent": {"messages": [ai_msg]}}]
    svc = ChatService(session, agent_factory=lambda: _StubAgent(updates))
    await svc.send_message("hi")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    types = [t for t, _ in events]
    assert types == ["tool.call", "tool.call"]
    assert events[0][1]["tool"] == "generate_bundle"
    assert events[0][1]["call_id"] == "c1"
    assert events[1][1]["tool"] == "list_bundle_fields"


# ── Tool result translation: bundle.ready and hitl.pending ───────────────────


@pytest.mark.asyncio
async def test_tool_result_done_with_zip_path_publishes_bundle_ready(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    result_payload = {
        "status": "done",
        "org": "srijan",
        "zip_path": "/tmp/srijan/Srijan.zip",
        "subject_types": 1,
        "programs": 2,
        "encounter_types": 9,
        "concepts": 84,
    }
    tool_msg = ToolMessage(content=json.dumps(result_payload), tool_call_id="c1")
    updates = [{"tools": {"messages": [tool_msg]}}]
    svc = ChatService(session, agent_factory=lambda: _StubAgent(updates))
    await svc.send_message("generate srijan")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    types = [t for t, _ in events]
    assert "bundle.ready" in types
    assert session.bundle_path == Path("/tmp/srijan/Srijan.zip")
    # Verify the bundle.ready summary subset
    bundle_event = next(e for t, e in events if t == "bundle.ready")
    assert bundle_event["path"] == "/tmp/srijan/Srijan.zip"
    assert bundle_event["summary"]["subject_types"] == 1
    assert bundle_event["summary"]["concepts"] == 84


@pytest.mark.asyncio
async def test_tool_result_needs_confirmation_publishes_hitl_pending(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    changes = [
        {"change_id": "c-1", "form": "F1", "kind": "long_name",
         "before": {"name": "long..."}, "after": {"name": "short"}},
        {"change_id": "c-2", "form": "F2", "kind": "duplicate_field",
         "before": {"name": "x"}, "after": {"name": "x2"}},
    ]
    result_payload = {
        "status": "needs_confirmation",
        "thread_id": "bundle-srijan-12345",
        "payload": {"kind": "confirm_changes", "org": "srijan", "changes": changes},
    }
    tool_msg = ToolMessage(content=json.dumps(result_payload), tool_call_id="c1")
    updates = [{"tools": {"messages": [tool_msg]}}]
    svc = ChatService(session, agent_factory=lambda: _StubAgent(updates))
    await svc.send_message("generate srijan")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    types = [t for t, _ in events]
    assert types[-1] == "hitl.pending"

    hitl_event = events[-1][1]
    assert hitl_event["changes"] == changes
    assert hitl_event["interrupt_id"]  # truthy, generated
    assert svc.has_pending_interrupt


# ── resolve ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_returns_false_for_unknown_interrupt(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    svc = ChatService(session, agent_factory=lambda: _StubAgent([]))
    ok = await svc.resolve("not-a-real-id", [{"change_id": "x", "decision": "yes"}])
    assert ok is False


@pytest.mark.asyncio
async def test_resolve_formats_message_and_runs_turn(tmp_path: Path) -> None:
    session = _make_session(tmp_path)

    # First turn: stub yields a needs_confirmation tool result.
    first_payload = {
        "status": "needs_confirmation",
        "thread_id": "bundle-srijan-1",
        "payload": {"changes": [{"change_id": "c-1"}]},
    }
    first_updates = [{
        "tools": {"messages": [
            ToolMessage(content=json.dumps(first_payload), tool_call_id="t1"),
        ]},
    }]

    # Second turn: stub captures the input message text via a closure.
    captured: dict[str, Any] = {}

    class _CapturingAgent:
        async def astream(self, input, *, config, stream_mode):  # noqa: ARG002
            captured["text"] = input["messages"][0].content
            # Yield nothing — just want to observe the input.
            return
            yield  # pragma: no cover

    # Construct with the stub agent for the first call; swap in capturing
    # agent for the resolve call.
    svc = ChatService(session, agent_factory=lambda: _StubAgent(first_updates))
    await svc.send_message("generate srijan")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    # Find the interrupt_id the service generated.
    interrupt_id = next(iter(svc._pending_interrupts.keys()))

    svc._agent = _CapturingAgent()
    ok = await svc.resolve(
        interrupt_id,
        [
            {"change_id": "c-1", "decision": "yes"},
            {"change_id": "c-2", "decision": "edit", "value": "Better name"},
            {"change_id": "c-3", "decision": "no"},
        ],
    )
    assert ok is True
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    text = captured["text"]
    assert "My decisions on the pending changes" in text
    # thread_id from the prior needs_confirmation is round-tripped explicitly
    # so the agent doesn't have to remember it from the prior tool result.
    assert "thread_id='bundle-srijan-1'" in text
    assert "resume_bundle" in text
    assert "- c-1: yes" in text
    assert "- c-2: edit:Better name" in text
    assert "- c-3: no" in text


# ── Error handling ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_exception_emits_error_event_and_closes_bus(tmp_path: Path) -> None:
    session = _make_session(tmp_path)

    class _ExplodingAgent:
        async def astream(self, _input, *, config, stream_mode):  # noqa: ARG002
            yield {"agent": {"messages": [AIMessage(content="starting")]}}
            raise RuntimeError("model overloaded")

    svc = ChatService(session, agent_factory=lambda: _ExplodingAgent())
    await svc.send_message("hi")
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    types = [t for t, _ in events]
    assert "error" in types
    assert "session.closed" in types
    error_event = next(e for t, e in events if t == "error")
    assert error_event["code"] == "E_AGENT"
    assert error_event["recoverable"] is False


@pytest.mark.asyncio
async def test_send_message_while_running_publishes_busy_error(tmp_path: Path) -> None:
    session = _make_session(tmp_path)

    pause_event = asyncio.Event()

    class _StallingAgent:
        async def astream(self, _input, *, config, stream_mode):  # noqa: ARG002
            await pause_event.wait()
            return
            yield  # pragma: no cover

    svc = ChatService(session, agent_factory=lambda: _StallingAgent())
    await svc.send_message("first")
    # Second message should hit the busy guard.
    await svc.send_message("second")
    pause_event.set()
    await asyncio.wait_for(svc._running_task, timeout=1.0)  # type: ignore[arg-type]

    events = _collect_published(session)
    busy = [e for t, e in events if t == "error" and e.get("code") == "E_BUSY"]
    assert len(busy) == 1
    assert busy[0]["recoverable"] is True

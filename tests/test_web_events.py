"""Unit tests for `src/web/events.py` — SSE primitives.

Usage:
    pytest tests/test_web_events.py
"""

from __future__ import annotations

import asyncio
import json

import pytest

from web.events import (
    REPLAY_BUFFER_SIZE,
    Event,
    EventBus,
    agent_message,
    bundle_ready,
    error_payload,
    hitl_pending,
    tool_call,
    tool_result,
    upload_done,
)


# ── Event.to_sse ──────────────────────────────────────────────────────────────


def test_event_to_sse_renders_event_id_data_keys() -> None:
    event = Event(id=7, type="agent.message", data={"role": "assistant", "content": "hi"})
    rendered = event.to_sse()
    assert rendered["event"] == "agent.message"
    assert rendered["id"] == "7"
    data = json.loads(rendered["data"])
    assert data["role"] == "assistant"
    assert data["content"] == "hi"
    assert "ts" in data  # injected by Event.__init__


# ── Payload helpers (field names track SDD §4.2) ──────────────────────────────


def test_payload_helpers_pin_sdd_field_names() -> None:
    assert agent_message("assistant", "ok") == {"role": "assistant", "content": "ok"}
    assert tool_call("generate_bundle", {"org": "srijan"}, "c1") == {
        "tool": "generate_bundle", "args": {"org": "srijan"}, "call_id": "c1",
    }
    assert tool_result("c1", True, "done") == {
        "call_id": "c1", "ok": True, "summary": "done",
    }
    assert hitl_pending("i1", [{"change_id": "x"}]) == {
        "interrupt_id": "i1", "changes": [{"change_id": "x"}],
    }
    assert bundle_ready("/tmp/x.zip", {"forms": 2}) == {
        "path": "/tmp/x.zip", "summary": {"forms": 2},
    }
    assert upload_done("job-1", "ok") == {"job_id": "job-1", "status": "ok"}
    assert upload_done("job-1", "failed", "401") == {
        "job_id": "job-1", "status": "failed", "details": "401",
    }
    assert error_payload("E_X", "boom", recoverable=True) == {
        "code": "E_X", "message": "boom", "recoverable": True,
    }


# ── EventBus.publish ──────────────────────────────────────────────────────────


def test_publish_assigns_monotonic_ids_and_buffers() -> None:
    bus = EventBus(buffer_size=10)
    a = bus.publish("agent.message", agent_message("assistant", "1"))
    b = bus.publish("agent.message", agent_message("assistant", "2"))
    assert a.id == 1 and b.id == 2
    assert bus.next_id == 3
    assert bus.replay_since(0) == [a, b]
    assert bus.replay_since(1) == [b]


def test_publish_after_close_is_dropped() -> None:
    bus = EventBus()
    bus.close("ended")
    dropped = bus.publish("agent.message", agent_message("assistant", "late"))
    assert dropped.id == 0
    # Buffer should still only hold the session.closed event published by close().
    buffered_types = [e.type for e in bus.replay_since(0)]
    assert buffered_types == ["session.closed"]


def test_buffer_caps_at_replay_buffer_size() -> None:
    bus = EventBus(buffer_size=5)
    for i in range(12):
        bus.publish("agent.message", agent_message("assistant", str(i)))
    buffered = bus.replay_since(0)
    assert len(buffered) == 5
    # Oldest five (ids 1-7) have scrolled off; surviving are ids 8-12.
    assert [e.id for e in buffered] == [8, 9, 10, 11, 12]


def test_default_replay_buffer_size_matches_sdd() -> None:
    # SDD §8.2 mandates 200.
    assert REPLAY_BUFFER_SIZE == 200


# ── EventBus.stream — async delivery ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_delivers_backlog_then_live_events_then_closes() -> None:
    bus = EventBus()
    bus.publish("agent.message", agent_message("assistant", "backlog-1"))
    bus.publish("agent.message", agent_message("assistant", "backlog-2"))

    collected: list[Event] = []

    async def consume() -> None:
        async for event in bus.stream(last_event_id=0):
            collected.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let consumer drain the backlog

    bus.publish("tool.call", tool_call("generate_bundle", {}, "c1"))
    await asyncio.sleep(0)

    bus.close("ended")
    await asyncio.wait_for(consumer, timeout=1.0)

    types = [e.type for e in collected]
    assert types == ["agent.message", "agent.message", "tool.call", "session.closed"]


@pytest.mark.asyncio
async def test_stream_resumes_from_last_event_id() -> None:
    bus = EventBus()
    bus.publish("agent.message", agent_message("assistant", "lost"))   # id 1
    bus.publish("agent.message", agent_message("assistant", "kept-1"))  # id 2
    bus.publish("agent.message", agent_message("assistant", "kept-2"))  # id 3

    collected: list[Event] = []

    async def consume() -> None:
        async for event in bus.stream(last_event_id=1):
            collected.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    bus.close("ended")
    await asyncio.wait_for(consumer, timeout=1.0)

    # The id-1 event should have been skipped by replay_since.
    assert [e.id for e in collected if e.type != "session.closed"] == [2, 3]


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    bus = EventBus()
    bus.close("ended")
    bus.close("ended-again")  # no-op
    # Only one session.closed event should be in the buffer.
    closed = [e for e in bus.replay_since(0) if e.type == "session.closed"]
    assert len(closed) == 1

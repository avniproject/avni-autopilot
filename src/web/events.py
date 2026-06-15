"""SSE event types, payload helpers, and per-session event bus.

Pure helpers — no HTTP, no I/O. `EventBus` is the queue + replay buffer the
chat service writes to and the SSE route reads from. At most one consumer
streams from a bus at a time; SDD §9 enforces that with a 409 at the route
layer.

Event payloads follow SDD §4.2. The wire format (`event:`, `id:`, `data:`,
double newline) is owned by `sse-starlette`'s `EventSourceResponse`, which
consumes the dicts `Event.to_sse()` returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal

log = logging.getLogger(__name__)


# ── Event types (SDD §4.2) ────────────────────────────────────────────────────

EventType = Literal[
    "agent.message",
    "tool.call",
    "tool.result",
    "hitl.pending",
    "pipeline.progress",
    "bundle.ready",
    "upload.done",
    "error",
    "session.closed",
]

# Per-session replay window. Older events scroll off when the buffer fills,
# so a reconnecting EventSource that has been gone for too long sees a gap.
# Matches SDD §8.2.
REPLAY_BUFFER_SIZE = 200


# ── Event dataclass ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """One SSE event ready for the wire.

    `id` is a per-session monotonic integer. Browsers send it back via
    `Last-Event-ID` on reconnect; `EventBus.stream` uses that to resume
    without dropping or duplicating events still in the replay buffer.
    """

    id: int
    type: str
    data: dict[str, Any]
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_sse(self) -> dict[str, str]:
        """Render as the dict shape `EventSourceResponse` consumes."""
        return {
            "event": self.type,
            "id": str(self.id),
            "data": json.dumps({**self.data, "ts": self.ts}),
        }


# ── Payload helpers (SDD §4.2 — field names pinned here) ──────────────────────


def agent_message(role: str, content: str) -> dict[str, Any]:
    """Payload for `agent.message` — one chat message from the agent."""
    return {"role": role, "content": content}


def tool_call(tool: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Payload for `tool.call` — agent invokes a tool."""
    return {"tool": tool, "args": args, "call_id": call_id}


def tool_result(call_id: str, ok: bool, summary: str) -> dict[str, Any]:
    """Payload for `tool.result` — tool returned."""
    return {"call_id": call_id, "ok": ok, "summary": summary}


def hitl_pending(interrupt_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Payload for `hitl.pending` — pipeline awaits user confirmations.

    `changes` mirrors `models.Change` dumps (see SDD §4.2 and chat/tools.py
    `_run_with_interrupt_handling`).
    """
    return {"interrupt_id": interrupt_id, "changes": changes}


def pipeline_progress(node: str, status: str) -> dict[str, Any]:
    """Payload for `pipeline.progress` — best-effort node lifecycle ticks."""
    return {"node": node, "status": status}


def bundle_ready(path: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Payload for `bundle.ready` — ZIP on disk, ready to download or upload."""
    return {"path": path, "summary": summary}


def upload_done(job_id: str, status: str, details: str = "") -> dict[str, Any]:
    """Payload for `upload.done` — avni-server's /import/new returned."""
    payload: dict[str, Any] = {"job_id": job_id, "status": status}
    if details:
        payload["details"] = details
    return payload


def error_payload(code: str, message: str, recoverable: bool) -> dict[str, Any]:
    """Payload for `error` — recoverable means the session stays alive."""
    return {"code": code, "message": message, "recoverable": recoverable}


def session_closed(reason: str) -> dict[str, Any]:
    """Payload for `session.closed` — terminal; consumer should disconnect."""
    return {"reason": reason}


# ── Per-session event bus ────────────────────────────────────────────────────


class EventBus:
    """Per-session event fan-out with bounded replay buffer.

    The chat service calls `publish` synchronously. A single SSE consumer at
    a time calls `stream(last_event_id)` and awaits new events; subsequent
    `EventSource` connections are rejected at the route layer with 409 per
    SDD §9.

    Implementation: the replay buffer (`deque` capped at
    `REPLAY_BUFFER_SIZE`) is the source of truth. Consumers track their
    delivery cursor; `_waiter` (asyncio.Event) wakes them when new events
    land. The clear-then-check ordering inside `stream` avoids races between
    a publish and a wait.
    """

    def __init__(self, buffer_size: int = REPLAY_BUFFER_SIZE) -> None:
        self._buffer: deque[Event] = deque(maxlen=buffer_size)
        self._waiter: asyncio.Event = asyncio.Event()
        self._next_id: int = 1
        self._closed: bool = False

    def publish(self, event_type: str, data: dict[str, Any]) -> Event:
        """Append one event and wake any waiting consumer.

        After `close()`, further publishes are dropped with a debug log —
        the bus is terminal.
        """
        if self._closed:
            log.debug(f"EventBus.publish on closed bus dropped: {event_type}")
            return Event(id=0, type=event_type, data=data)
        event = Event(id=self._next_id, type=event_type, data=data)
        self._next_id += 1
        self._buffer.append(event)
        self._waiter.set()
        return event

    def close(self, reason: str = "ended") -> None:
        """Emit a final `session.closed` and unblock any waiting consumer.

        Idempotent — calling twice only emits once.
        """
        if self._closed:
            return
        self.publish("session.closed", session_closed(reason))
        self._closed = True
        self._waiter.set()

    def replay_since(self, last_event_id: int) -> list[Event]:
        """Return buffered events whose id is > `last_event_id`.

        Pass 0 to replay everything in the buffer. Events older than the
        oldest buffered entry have already scrolled off and are gone.
        """
        return [e for e in self._buffer if e.id > last_event_id]

    async def stream(self, last_event_id: int = 0) -> AsyncIterator[Event]:
        """Yield events with id > `last_event_id`, then live events.

        Terminates after delivering a `session.closed` event (or when the
        bus is closed with no `session.closed` left in the buffer, which
        only happens if it scrolled off — defence in depth).
        """
        cursor = last_event_id
        while True:
            self._waiter.clear()
            backlog = [e for e in self._buffer if e.id > cursor]
            for event in backlog:
                cursor = event.id
                yield event
                if event.type == "session.closed":
                    return
            if self._closed:
                # `session.closed` was in the buffer at some point and was
                # delivered above, OR the buffer overflowed it before any
                # consumer connected. In the second case, exit cleanly so
                # the consumer doesn't block forever.
                return
            await self._waiter.wait()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def next_id(self) -> int:
        """For tests / introspection — the id the next publish will assign."""
        return self._next_id

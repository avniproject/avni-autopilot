"""`POST /message`, `POST /resolve`, `GET /events` (SSE).

The streaming endpoint is the only SSE in the service; the other two
accept JSON, hand off to the per-session ChatService, and return a small
ack. Concurrent-stream prevention (SDD §9 — 409 on duplicate `EventSource`)
is enforced via `ChatSession.stream_in_use`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from web.sessions import ChatSession, SessionStore

log = logging.getLogger(__name__)
router = APIRouter()


def _get_store(request: Request) -> SessionStore:
    return request.app.state.store


def _require_session(session_id: str, store: SessionStore) -> ChatSession:
    session = store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "session not found or expired", "code": "E_NO_SESSION"},
        )
    if session.chat_service is None:
        # Defence in depth: routes/sessions.create_session always wires the
        # service; only a partial-construct race could leave it None.
        raise HTTPException(
            status_code=503,
            detail={"error": "session not ready", "code": "E_NOT_READY"},
        )
    session.touch()
    return session


# ── Request bodies ────────────────────────────────────────────────────────────


class _MessageBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)


class _Resolution(BaseModel):
    change_id: str
    decision: str  # "yes" | "no" | "edit"
    value: str | None = None


class _ResolveBody(BaseModel):
    interrupt_id: str
    resolutions: list[_Resolution] = Field(default_factory=list)


# ── POST /message ─────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/message")
async def post_message(
    session_id: str,
    body: _MessageBody,
    store: SessionStore = Depends(_get_store),
) -> dict[str, bool]:
    """Forward one user turn to the agent. Returns immediately; events stream."""
    session = _require_session(session_id, store)
    assert session.chat_service is not None  # _require_session enforced
    await session.chat_service.send_message(body.text)
    return {"accepted": True}


# ── POST /resolve ─────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/resolve")
async def post_resolve(
    session_id: str,
    body: _ResolveBody,
    store: SessionStore = Depends(_get_store),
) -> dict[str, bool]:
    """Apply HITL decisions to a pending interrupt.

    Returns 400 when the interrupt id is unknown (race with reaper, or
    duplicate submission). SDD §9 maps this exact failure.
    """
    session = _require_session(session_id, store)
    assert session.chat_service is not None
    ok = await session.chat_service.resolve(
        body.interrupt_id,
        [r.model_dump() for r in body.resolutions],
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown or already-resolved interrupt_id",
                "code": "E_BAD_INTERRUPT",
            },
        )
    return {"accepted": True}


# ── GET /events (SSE) ─────────────────────────────────────────────────────────


@router.get("/sessions/{session_id}/events")
async def get_events(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(_get_store),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """Stream SSE events for the session.

    Rejects a second concurrent consumer with 409 per SDD §9. Replay is
    keyed by the standard `Last-Event-ID` header so a tab refresh resumes
    without dropping events that fired during the network gap.
    """
    session = _require_session(session_id, store)
    if session.stream_in_use:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "another EventSource is already streaming this session",
                "code": "E_DUP_STREAM",
            },
        )
    session.stream_in_use = True

    cursor = 0
    if last_event_id:
        try:
            cursor = int(last_event_id)
        except ValueError:
            cursor = 0

    async def stream() -> Any:
        try:
            async for event in session.bus.stream(last_event_id=cursor):
                # If the client disconnected, EventSourceResponse will raise
                # when we yield; honour `request.is_disconnected()` to bail
                # early so the bus stops accumulating idle work.
                if await request.is_disconnected():
                    return
                yield event.to_sse()
        finally:
            session.stream_in_use = False

    return EventSourceResponse(stream())

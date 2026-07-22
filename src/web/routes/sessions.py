"""`POST /sessions` and `DELETE /sessions/{session_id}`.

POST verifies the user's token against avni-server, creates a session,
wires its ChatService, and returns the session id (+ org + expiry).
DELETE tears the session down — closes the bus, deletes the workdir.
Failure modes: SDD §9.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from config import settings
from web.auth import AuthError, verify_token
from web.chat_service import ChatService
from web.sessions import SessionStore, download_org_bundle

log = logging.getLogger(__name__)
router = APIRouter()


def _get_store(request: Request) -> SessionStore:
    """Dependency — read the SessionStore from app.state."""
    return request.app.state.store


def _expiry_iso(absolute_hours: int) -> str:
    """ISO-8601 timestamp at which the session is force-reaped."""
    return (
        datetime.now(timezone.utc) + timedelta(hours=absolute_hours)
    ).isoformat()


@router.post("/sessions", status_code=201)
async def create_session(
    response: Response,
    store: SessionStore = Depends(_get_store),
    auth_token: str | None = Header(default=None, alias="AUTH-TOKEN"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Verify the user's token, allocate a session, wire its ChatService.

    Returns `{session_id, org_name, expires_at}` and sets the `AI_SID`
    cookie used by the ALB for sticky routing (DEPLOYMENT_SDD §7).

    Accepts the token on the `AUTH-TOKEN` header (avni-webapp's convention)
    or `Authorization` (any other caller). Forwarded as `AUTH-TOKEN` to
    avni-server.
    """
    token = auth_token or authorization or ""
    try:
        info = await verify_token(settings.avni_server_base_url, token)
    except AuthError as exc:
        log.info(f"create_session rejected: {exc.status_code} {exc.message}")
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.message, "code": "E_AUTH"},
        ) from exc

    session = store.create(
        org_name=info.organisation_name,
        username=info.username,
        auth_token=token,
        shared_input_root=Path(settings.input_root),
        shared_output_root=Path(settings.output_root),
    )
    session.chat_service = ChatService(session)
    asyncio.create_task(download_org_bundle(session))

    # AI_SID cookie — keyed for ALB sticky sessions per DEPLOYMENT_SDD §7.
    # max_age matches the absolute session bound so the cookie expires in
    # lockstep with the reaper.
    response.set_cookie(
        key="AI_SID",
        value=session.session_id,
        max_age=settings.ai_session_max_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=True,
    )

    return {
        "session_id": session.session_id,
        "org_name": session.org_name,
        "expires_at": _expiry_iso(settings.ai_session_max_hours),
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    store: SessionStore = Depends(_get_store),
) -> dict[str, Any]:
    """Liveness check used by the browser to validate a cached session id
    before reconnecting SSE; 404s with `E_NO_SESSION` if reaped."""
    session = store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "E_NO_SESSION", "message": "session not found"},
        )
    return {"session_id": session_id, "org_name": session.org_name}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    response: Response,
    store: SessionStore = Depends(_get_store),
) -> None:
    """Tear down a session. Idempotent — 204 even if the id is unknown."""
    store.remove(session_id, reason="user_close")
    response.delete_cookie(key="AI_SID")

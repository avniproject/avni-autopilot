"""`GET /sessions/{id}/bundle` — stream the generated ZIP to the browser.

Returns 404 when no bundle has been generated yet. The path is captured
on the session by `ChatService._publish_bundle_ready` after the chat
agent's `generate_bundle` / `edit_bundle_from_spec` tool completes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

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
    session.touch()
    return session


@router.get("/sessions/{session_id}/bundle")
async def get_bundle(
    session_id: str,
    store: SessionStore = Depends(_get_store),
) -> FileResponse:
    """Stream the bundle ZIP as `application/zip`."""
    session = _require_session(session_id, store)
    bundle_path = session.bundle_path
    if bundle_path is None or not Path(bundle_path).exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no bundle available — generate one first",
                "code": "E_NO_BUNDLE",
            },
        )
    return FileResponse(
        path=str(bundle_path),
        media_type="application/zip",
        filename=Path(bundle_path).name,
    )

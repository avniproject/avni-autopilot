"""`POST /sessions/{id}/upload` and `POST /sessions/{id}/upload-to-avni`.

The first writes browser-uploaded .xlsx files into the session's input
workdir; the chat agent's `generate_bundle` tool reads them from there.
The second relays the generated bundle ZIP to avni-server's existing
Metadata-Zip import endpoint (the same one avni-webapp's "Upload" page
hits — see `avni-webapp/src/upload/api.js`). Failure modes: SDD §9.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
)

from config import settings
from web.sessions import ChatSession, SessionStore

log = logging.getLogger(__name__)
router = APIRouter()

# Endpoint avni-webapp's bulkUpload posts to (src/upload/api.js).
IMPORT_PATH = "/import/new"
# Type identifier avni-server uses for a Metadata Zip bundle. Wire value is
# the camelCase code (UploadTypes.getCode), not the display name "Metadata
# Zip" — avni-server's ImportController.java compares `type.equals("metadataZip")`.
IMPORT_TYPE = "metadataZip"
# How long the bundle relay may take. Avni-server's import processes
# asynchronously — the response is just job-acceptance, so 30 s is enough.
UPLOAD_TIMEOUT_SEC = 30.0

ALLOWED_UPLOAD_SUFFIXES = {".xlsx", ".xls"}
# 1 MiB per read — small enough to stay responsive on a slow disk, big
# enough that 10 MB workbooks copy in ~10 reads.
COPY_CHUNK_SIZE = 1024 * 1024


def _get_store(request: Request) -> SessionStore:
    return request.app.state.store


def _require_session(session_id: str, store: SessionStore) -> ChatSession:
    """Look up the session or raise 404. Touches activity timestamp on success."""
    session = store.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "session not found or expired", "code": "E_NO_SESSION"},
        )
    session.touch()
    return session


# ── POST /sessions/{id}/upload ────────────────────────────────────────────────


@router.post("/sessions/{session_id}/upload")
async def upload_scoping_files(
    session_id: str,
    files: list[UploadFile] = File(...),
    store: SessionStore = Depends(_get_store),
) -> dict[str, Any]:
    """Stream uploaded .xlsx files into the org's input directory.

    Writes to `settings.input_root/<org_slug>/` — the same folder convention
    the existing `generate_bundle(org)` tool reads from. The slug is derived
    from the user's authenticated org (NOT user-supplied), so the chat agent
    can call `generate_bundle(org=<slug>)` without asking the user for it.

    Files with non-xlsx suffixes are rejected — the pipeline only reads
    xlsx — so the user catches mistakes early.
    """
    session = _require_session(session_id, store)
    input_dir = Path(settings.input_root) / session.org_slug
    input_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"unsupported file type: {upload.filename!r}",
                    "code": "E_BAD_FILE_TYPE",
                },
            )
        target = input_dir / Path(upload.filename).name
        try:
            with target.open("wb") as out:
                while chunk := await upload.read(COPY_CHUNK_SIZE):
                    out.write(chunk)
        except OSError as exc:
            log.warning(f"upload write failed sid={session_id} file={target}: {exc}")
            raise HTTPException(
                status_code=500,
                detail={"error": f"could not write {target.name}", "code": "E_IO"},
            ) from exc
        written.append(str(target))

    log.info(f"upload sid={session_id} files={len(written)}")
    return {"paths": written}


# ── POST /sessions/{id}/upload-to-avni ────────────────────────────────────────


@router.post("/sessions/{session_id}/upload-to-avni")
async def upload_to_avni(
    session_id: str,
    store: SessionStore = Depends(_get_store),
) -> dict[str, Any]:
    """Relay the generated bundle ZIP to avni-server's import endpoint.

    The session must have a `bundle_path` set (the chat agent assigns it on
    a successful `generate_bundle` / `edit_bundle_from_spec` — SDD §7).
    The admin's auth token captured at session creation is reused here.

    avni-server returns the import job id (response body — opaque text).
    The browser deep-links to the existing UploadStatus page filtered to
    that job id.
    """
    session = _require_session(session_id, store)

    if session.bundle_path is None or not Path(session.bundle_path).exists():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no bundle available — generate one first",
                "code": "E_NO_BUNDLE",
            },
        )

    url = settings.avni_server_base_url.rstrip("/") + IMPORT_PATH
    # `/import/new` requires six params (avni-server ImportController.java:124).
    # For Metadata Zip the location / encounter / enrolment modes are unused
    # but Spring still demands them — match what avni-webapp's UploadDashboard
    # sends for the metadataZip upload type (empty strings + locationHierarchy=0).
    params = {
        "type": IMPORT_TYPE,
        "autoApprove": "false",
        "locationUploadMode": "",
        "locationHierarchy": "0",
        "encounterUploadMode": "",
        "programEnrolmentUploadMode": "",
    }
    # avni-server expects the bearer token on `AUTH-TOKEN`, not Authorization.
    # `user-name` is also set by avni-webapp's httpClient on every request and
    # is consumed by avni-server's auth filter.
    headers = {
        "AUTH-TOKEN": session.auth_token,
        "user-name": session.username,
    }
    file_path = Path(session.bundle_path)

    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SEC) as client:
            with file_path.open("rb") as fh:
                response = await client.post(
                    url,
                    params=params,
                    headers=headers,
                    files={"file": (file_path.name, fh, "application/zip")},
                )
    except httpx.HTTPError as exc:
        log.warning(f"upload-to-avni network error sid={session_id}: {exc}")
        session.bus.publish(
            "upload.done",
            {"job_id": "", "status": "failed", "details": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail={"error": f"avni-server unreachable: {exc}", "code": "E_RELAY"},
        ) from exc

    if response.status_code == 401:
        # SDD §9: admin token expired mid-session.
        raise HTTPException(
            status_code=401,
            detail={"error": "avni-server rejected the token", "code": "E_AUTH"},
        )
    if response.status_code >= 400:
        details = response.text[:500]
        log.warning(
            f"upload-to-avni avni-server {response.status_code} sid={session_id}: {details}"
        )
        session.bus.publish(
            "upload.done",
            {"job_id": "", "status": "failed", "details": details},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"avni-server returned {response.status_code}",
                "code": "E_RELAY",
                "details": details,
            },
        )

    job_id = response.text.strip()
    session.bus.publish(
        "upload.done", {"job_id": job_id, "status": "ok"},
    )
    return {"job_id": job_id, "status": "ok"}

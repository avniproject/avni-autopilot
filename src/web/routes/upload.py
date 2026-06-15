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

# Per-session filename for the avni-server error response captured on a
# failed relay. Overwritten on every failed upload — only the most recent
# attempt is retrievable.
UPLOAD_ERROR_LOG_FILENAME = "upload_error.log"


def _write_upload_error_log(
    workdir: Path,
    *,
    status_code: int,
    body: str,
    context: str,
) -> Path | None:
    """Persist a failed-relay error payload to the session workdir.

    Returns the path on success, None if the write failed (best-effort —
    a failed log write must not mask the original upload failure). The
    file is exposed via `GET /sessions/{id}/upload-error-log` so users
    can inspect the full avni-server response, not just the SSE preview.
    """
    target = workdir / UPLOAD_ERROR_LOG_FILENAME
    try:
        header = (
            f"# avni-ai-web :: upload-to-avni error log\n"
            f"# context: {context}\n"
            f"# http_status: {status_code}\n"
            f"# ---\n"
        )
        target.write_text(header + body, encoding="utf-8")
    except OSError as exc:
        log.warning(f"could not write upload error log to {target}: {exc}")
        return None
    return target


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
        error_log = _write_upload_error_log(
            session.workdir,
            status_code=0,
            body=str(exc),
            context=f"network error to {url}",
        )
        session.bus.publish(
            "upload.done",
            {
                "job_id": "",
                "status": "failed",
                "details": str(exc),
                "error_log_available": error_log is not None,
            },
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
        # `details` is the truncated preview shown inline; the full response
        # body is persisted to disk and exposed via GET /upload-error-log so
        # operators can grab the complete avni-server payload (stack traces,
        # per-record reasons) without it bloating the SSE event.
        details = response.text[:500]
        full_body = response.text
        log.warning(
            f"upload-to-avni avni-server {response.status_code} sid={session_id}: {details}"
        )
        error_log = _write_upload_error_log(
            session.workdir,
            status_code=response.status_code,
            body=full_body,
            context=f"avni-server returned HTTP {response.status_code}",
        )
        session.bus.publish(
            "upload.done",
            {
                "job_id": "",
                "status": "failed",
                "details": details,
                "error_log_available": error_log is not None,
            },
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"avni-server returned {response.status_code}",
                "code": "E_RELAY",
                "details": details,
            },
        )

    # avni-server's /import/new returns the literal string "true" on
    # acceptance — NOT the job UUID. The UUID must be discovered by polling
    # /import/status, where the newly-submitted job appears at the head of
    # the list. Match by fileName so a concurrent upload from the same
    # user doesn't get attributed here.
    job_uuid = await _fetch_latest_job_uuid(
        base_url=settings.avni_server_base_url,
        headers=headers,
        file_name=file_path.name,
    )
    session.bus.publish(
        "upload.done", {"job_id": job_uuid or "", "status": "ok"},
    )
    return {"job_id": job_uuid or "", "status": "ok"}


async def _fetch_latest_job_uuid(
    *,
    base_url: str,
    headers: dict[str, str],
    file_name: str,
    retries: int = 3,
    retry_delay_sec: float = 0.5,
) -> str | None:
    """Look up the most recent import job whose fileName matches the upload.

    Returns the job's UUID, or None on any failure (the upload itself has
    already succeeded by this point — a missing UUID just means the user
    can't download the per-row error CSV via the inline button, which
    degrades to the "View jobs" page rather than blocking the flow).

    A small retry loop covers the case where /import/status hasn't yet
    reflected the just-submitted job.
    """
    import asyncio

    url = f"{base_url.rstrip('/')}/import/status"
    params = {"size": 10}
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params, headers=headers)
            if resp.status_code >= 400:
                log.warning(
                    f"import/status returned {resp.status_code}: {resp.text[:200]}"
                )
                return None
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(f"import/status fetch failed: {exc}")
            return None
        for entry in payload.get("content") or []:
            if entry.get("fileName") == file_name and entry.get("uuid"):
                return entry["uuid"]
        if attempt + 1 < retries:
            await asyncio.sleep(retry_delay_sec)
    log.warning(
        f"could not locate import job for fileName={file_name!r} after {retries} attempts"
    )
    return None


# ── GET /sessions/{id}/upload-error-log ───────────────────────────────────────


@router.get("/sessions/{session_id}/upload-error-log")
async def get_upload_error_log(
    session_id: str,
    store: SessionStore = Depends(_get_store),
):
    """Return the avni-server response body captured on the last failed relay.

    Available only after `POST /upload-to-avni` returned a non-2xx; 404
    otherwise. Use this for failures of the relay itself — for the
    per-row error CSV produced by an accepted import job, hit avni-server's
    `/import/errorfile?jobUuid=<id>` directly.
    """
    from fastapi.responses import FileResponse

    session = _require_session(session_id, store)
    log_path = session.workdir / UPLOAD_ERROR_LOG_FILENAME
    if not log_path.exists():
        raise HTTPException(
            status_code=404,
            detail={"code": "E_NO_ERROR_LOG", "message": "no upload error log on this session"},
        )
    return FileResponse(
        path=log_path,
        media_type="text/plain",
        filename="upload_error.log",
    )

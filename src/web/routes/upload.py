"""`POST /sessions/{id}/upload` and `POST /sessions/{id}/upload-to-avni`.

The first writes browser-uploaded .xlsx files into the session's input
workdir; the chat agent's `generate_bundle` tool reads them from there.
The second relays the generated bundle ZIP to avni-server's existing
Metadata-Zip import endpoint (the same one avni-webapp's "Upload" page
hits — see `avni-webapp/src/upload/api.js`). Failure modes: SDD §9.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)

from config import settings
from domain.spreadsheets import is_spreadsheet_name
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

IMPORT_STATUS_PATH = "/import/status"
IMPORT_ERROR_FILE_PATH = "/import/errorfile"
# Spring Batch statuses after which an import job no longer changes.
FINISHED_JOB_STATUSES = frozenset({"COMPLETED", "FAILED", "STOPPED", "ABANDONED"})
# The upload request, counted from its start, waits at most this long for the
# import to finish, under a load balancer's usual 60 s idle timeout; a longer
# import is reported when it ends.
IMPORT_WAIT_IN_REQUEST_SEC = 45.0
IMPORT_WATCH_LIMIT_SEC = 600.0
IMPORT_POLL_INTERVAL_SEC = 2.0
IMPORT_NOT_CONFIRMED = (
    "Avni accepted the file, but its import result could not be read. "
    "Open Admin › Upload to see whether it was imported."
)
# Error lines quoted in the chat; the full error file is in the upload error log.
MAX_IMPORT_ERRORS_SHOWN = 3

# Import watches still running after their upload request returned; held so
# the event loop does not drop them.
_running_import_watches: set[asyncio.Task] = set()

# 1 MiB per read — small enough to stay responsive on a slow disk, big
# enough that 10 MB workbooks copy in ~10 reads.
COPY_CHUNK_SIZE = 1024 * 1024

UPLOAD_ERROR_LOG_FILENAME = "upload_error.log"


def _record_relay_failure(
    session: ChatSession,
    *,
    session_id: str,
    status_code: int,
    body: str,
    details: str,
    context: str,
) -> None:
    """Persist + publish a failed `upload-to-avni` relay.

    Writes the full avni-server body to the session workdir, logs a
    truncated preview, and emits the `upload.done` event with the
    `error_log_available` flag the UI uses to enable the log download.
    """
    log.warning(
        f"upload-to-avni failure sid={session_id} status={status_code}: {details}"
    )
    error_log = _write_upload_error_log(
        session.workdir,
        status_code=status_code,
        body=body,
        context=context,
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


def _write_upload_error_log(
    workdir: Path,
    *,
    status_code: int,
    body: str,
    context: str,
) -> Path | None:
    """Persist a failed-relay error payload. Returns the path on success,
    None if the write failed — must not mask the original upload error."""
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

    Writes to the session's own `input_dir`, which the build and update
    tools read in a webapp session. It is per chat, so a second chat for the
    same org neither sees nor clears these files.

    Files with non-xlsx suffixes (including old-format .xls, which the
    pipeline cannot read) and Office lock files are rejected before any file
    is written, so a rejected upload leaves nothing behind.
    """
    session = _require_session(session_id, store)
    file_names = [Path(upload.filename or "").name for upload in files]
    rejected = [name for name in file_names if not is_spreadsheet_name(name)]
    if rejected:
        listed = ", ".join(f"'{name}'" for name in rejected)
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    f"Nothing was uploaded: {listed} "
                    f"{'is not an .xlsx workbook' if len(rejected) == 1 else 'are not .xlsx workbooks'}. "
                    "Only .xlsx files can be read; save an .xls file as .xlsx and upload it again."
                ),
                "code": "E_BAD_FILE_TYPE",
            },
        )
    input_dir = session.input_dir
    input_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for upload, file_name in zip(files, file_names):
        target = input_dir / file_name
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
    auth_token: str | None = Header(default=None, alias="AUTH-TOKEN"),
) -> dict[str, Any]:
    """Relay the generated bundle ZIP to avni-server's import endpoint.

    The session must have a `bundle_path` set (the chat agent assigns it on
    a successful `generate_bundle` / `edit_bundle_from_spec` — SDD §7).
    The admin's auth token captured at session creation is reused here.

    avni-server only accepts the file; the import then runs as a job. The
    `upload.done` event says ok only when that job imported every file, and
    otherwise carries avni-server's reasons. An import still running after
    IMPORT_WAIT_IN_REQUEST_SEC returns status "importing" and publishes
    `upload.done` when it ends.
    """
    session = _require_session(session_id, store)

    # The token comes from this request, not the session — the browser
    # refreshes it automatically, so it is always current. Only the watch of
    # this upload's import holds it, until the import ends.
    if not auth_token:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing AUTH-TOKEN header", "code": "E_AUTH"},
        )

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
        "AUTH-TOKEN": auth_token,
        "user-name": session.username,
    }
    file_path = Path(session.bundle_path)
    # avni-server records the uploaded file's name on the import job; a name of
    # its own lets the watch find this upload's job among the user's others.
    upload_name = f"{file_path.stem}-{uuid.uuid4().hex[:12]}{file_path.suffix}"
    loop = asyncio.get_running_loop()
    request_started = loop.time()

    try:
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_SEC) as client:
            with file_path.open("rb") as fh:
                response = await client.post(
                    url,
                    params=params,
                    headers=headers,
                    files={"file": (upload_name, fh, "application/zip")},
                )
    except httpx.HTTPError as exc:
        _record_relay_failure(
            session,
            session_id=session_id,
            status_code=0,
            body=str(exc),
            details=str(exc),
            context=f"network error to {url}",
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
        _record_relay_failure(
            session,
            session_id=session_id,
            status_code=response.status_code,
            body=response.text,
            details=details,
            context=f"avni-server returned HTTP {response.status_code}",
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": f"avni-server returned {response.status_code}",
                "code": "E_RELAY",
                "details": details,
            },
        )

    # avni-server's /import/new only accepts the file; the import runs as a
    # job. Its outcome is what the user is told.
    watch = asyncio.create_task(_watch_import(session, headers=headers, upload_name=upload_name))
    _running_import_watches.add(watch)
    watch.add_done_callback(_running_import_watches.discard)
    time_left = IMPORT_WAIT_IN_REQUEST_SEC - (loop.time() - request_started)
    finished, _ = await asyncio.wait({watch}, timeout=max(time_left, 0.0))
    if finished:
        return watch.result()
    return {"job_id": "", "status": "importing"}


async def _watch_import(session: ChatSession, *, headers: dict[str, str], upload_name: str) -> dict[str, Any]:
    """Wait for the import job of this upload to finish and publish `upload.done`."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            job = await _finished_job(client, headers, upload_name)
            outcome = await _import_outcome(client, session, headers=headers, job=job)
    except Exception:  # noqa: BLE001 — the user must hear back whatever went wrong
        log.exception(f"import watch failed for sid={session.session_id}")
        outcome = {"job_id": "", "status": "failed", "details": IMPORT_NOT_CONFIRMED, "error_log_available": False}
    session.bus.publish("upload.done", outcome)
    return outcome


async def _finished_job(
    client: httpx.AsyncClient, headers: dict[str, str], upload_name: str,
) -> dict[str, Any] | None:
    """This upload's job once finished, or as last seen when the watch limit passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + IMPORT_WATCH_LIMIT_SEC
    job: dict[str, Any] | None = None
    while True:
        jobs = await _recent_import_jobs(client, headers)
        if jobs is not None:
            job = next((j for j in jobs if j.get("fileName") == upload_name), None)
            if job is not None and job.get("status") in FINISHED_JOB_STATUSES:
                return job
        if loop.time() >= deadline:
            return job
        await asyncio.sleep(IMPORT_POLL_INTERVAL_SEC)


async def _import_outcome(
    client: httpx.AsyncClient, session: ChatSession, *, headers: dict[str, str], job: dict[str, Any] | None,
) -> dict[str, Any]:
    """`upload.done` payload: ok only when every file of the bundle was imported."""
    job_uuid = (job or {}).get("uuid") or ""
    if job is None or job.get("status") not in FINISHED_JOB_STATUSES:
        log.warning(f"import job for sid={session.session_id} not confirmed: {job}")
        return {"job_id": job_uuid, "status": "failed", "details": IMPORT_NOT_CONFIRMED,
                "error_log_available": False}
    if job.get("status") == "COMPLETED" and not job.get("skipped"):
        return {"job_id": job_uuid, "status": "ok"}

    error_file = await _import_error_file(client, headers, job_uuid)
    details = _import_failure_details(job, error_file)
    log.warning(f"import job {job_uuid} sid={session.session_id} did not import everything: {details}")
    error_log = _write_upload_error_log(
        session.workdir,
        status_code=200,
        body=error_file or details,
        context=f"import job {job_uuid} finished {job.get('status')} with {job.get('skipped')} file(s) skipped",
    )
    return {"job_id": job_uuid, "status": "failed", "details": details,
            "error_log_available": error_log is not None}


def _import_failure_details(job: dict[str, Any], error_file: str | None) -> str:
    """One line for the chat: how many files were not imported and avni-server's first reasons."""
    total, skipped = job.get("total") or 0, job.get("skipped") or 0
    if skipped:
        summary = f"Avni did not import {skipped} of the {total} files in the app"
    else:
        summary = f"Avni's import job ended with status {job.get('status')}"
    reasons = _import_error_reasons(error_file or "")
    if not reasons:
        return f"{summary}."
    shown = "; ".join(reasons[:MAX_IMPORT_ERRORS_SHOWN])
    more = len(reasons) - MAX_IMPORT_ERRORS_SHOWN
    return f"{summary}: {shown}" + (f" (and {more} more)" if more > 0 else "")


def _import_error_reasons(error_file: str) -> list[str]:
    """`<file>: <first line of avni-server's message>` per record of the CSV
    error file; each message is followed by its stack trace."""
    reasons = []
    for row in csv.reader(io.StringIO(error_file)):
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            reasons.append(f"{row[0].strip()}: {row[1].strip().splitlines()[0]}")
    return reasons


async def _recent_import_jobs(client: httpx.AsyncClient, headers: dict[str, str]) -> list[dict[str, Any]] | None:
    """The user's latest import jobs, newest first; None when avni-server cannot be read."""
    url = settings.avni_server_base_url.rstrip("/") + IMPORT_STATUS_PATH
    try:
        resp = await client.get(url, params={"size": 10}, headers=headers)
        if resp.status_code >= 400:
            log.warning(f"import/status returned {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json().get("content") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning(f"import/status fetch failed: {exc}")
        return None


async def _import_error_file(client: httpx.AsyncClient, headers: dict[str, str], job_uuid: str) -> str | None:
    url = settings.avni_server_base_url.rstrip("/") + IMPORT_ERROR_FILE_PATH
    try:
        resp = await client.get(
            url,
            params={"jobUuid": job_uuid},
            headers={**headers, "Content-Type": "application/octet-stream"},
        )
        if resp.status_code >= 400:
            log.warning(f"import/errorfile returned {resp.status_code} for job {job_uuid}")
            return None
        return resp.text.lstrip("\ufeff")
    except httpx.HTTPError as exc:
        log.warning(f"import/errorfile fetch failed for job {job_uuid}: {exc}")
        return None


# ── GET /sessions/{id}/upload-error-log ───────────────────────────────────────


@router.get("/sessions/{session_id}/upload-error-log")
async def get_upload_error_log(
    session_id: str,
    store: SessionStore = Depends(_get_store),
):
    """Return the avni-server response body captured on the last failed
    relay; 404 otherwise. For per-row errors from an accepted import job,
    use avni-server's `/import/errorfile?jobUuid=<id>` instead."""
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

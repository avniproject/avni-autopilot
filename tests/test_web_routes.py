"""End-to-end smoke tests for the FastAPI app — wires every route.

Uses FastAPI's `TestClient` (sync) plus an `httpx.MockTransport` for the
outbound calls to avni-server (token verify + bundle upload). The chat
service is stubbed by monkey-patching `web.routes.sessions.ChatService`
to one that drives a canned event stream.

Usage:
    pytest tests/test_web_routes.py
"""

from __future__ import annotations

import dataclasses
import io
import re
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

import web.auth as auth_mod
import web.routes.sessions as sessions_routes
import web.routes.upload as upload_routes
from config import Settings, settings
from web.app import create_app
from web.sessions import ChatSession, SessionStore


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Settings:
    """Build a Settings replacement and patch every import site.

    `Settings` is a frozen dataclass — direct `setattr` fails. `dataclasses.
    replace` returns a fresh instance honouring the overrides; the patch is
    applied at each module that imported `settings` by name so route handlers
    see the test values.
    """
    new_settings = dataclasses.replace(settings, **overrides)
    monkeypatch.setattr("config.settings", new_settings)
    monkeypatch.setattr("web.app.settings", new_settings)
    monkeypatch.setattr("web.routes.sessions.settings", new_settings)
    monkeypatch.setattr("web.routes.upload.settings", new_settings)
    return new_settings


# ── Stubs ─────────────────────────────────────────────────────────────────────


class _StubChatService:
    """Drop-in for ChatService — records calls; never invokes a real agent."""

    def __init__(self, session: ChatSession, agent_factory: Any = None) -> None:
        self.session = session
        self.sent: list[str] = []
        self.resolved: list[tuple[str, list[dict]]] = []
        self._pending: dict[str, Any] = {}

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def resolve(self, interrupt_id: str, resolutions: list[dict]) -> bool:
        self.resolved.append((interrupt_id, resolutions))
        return interrupt_id in self._pending


def _install_stub_chat_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ChatService at the import sites so create_session uses the stub."""
    monkeypatch.setattr(sessions_routes, "ChatService", _StubChatService)


def _install_fake_avni_server(
    monkeypatch: pytest.MonkeyPatch,
    *,
    userinfo_status: int = 200,
    userinfo_body: dict | None = None,
    import_status: int = 200,
    import_body: str = "job-42",
    import_job: dict | list[dict] | None = None,
    import_error_file: str = "",
) -> None:
    """Replace verify_token and the upload-to-avni httpx client with mocks.

    Each accepted upload starts a job named after the uploaded file, as
    avni-server does; `import_job` (one override, or one per upload in order)
    sets how it ends — by default completed with nothing skipped. A job is
    listed from the second /import/status call after its upload, as
    avni-server can list a new job a moment late. An earlier job of the same
    bundle is always listed.
    """
    userinfo_body = userinfo_body or {"username": "alice", "organisationName": "Srijan"}

    async def fake_verify(base_url: str, header: str, *, client: httpx.AsyncClient | None = None):
        if not header:
            raise auth_mod.AuthError(401, "missing header")
        if userinfo_status != 200:
            raise auth_mod.AuthError(userinfo_status, "stubbed failure")
        return auth_mod.UserInfo(
            username=userinfo_body["username"],
            organisation_name=userinfo_body["organisationName"],
        )

    monkeypatch.setattr(sessions_routes, "verify_token", fake_verify)

    # For /upload-to-avni: patch httpx.AsyncClient with a MockTransport.
    real_async_client = httpx.AsyncClient
    earlier_job = {"fileName": "Srijan-0123456789ab.zip", "uuid": "job-earlier", "status": "COMPLETED",
                   "total": 3, "completed": 1, "skipped": 2}
    overrides = import_job if isinstance(import_job, list) else [import_job or {}]
    # Newest first, each with the number of /import/status calls since its upload.
    jobs: list[dict] = []

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/import/new":
                # All six params avni-server's ImportController requires must be present.
                assert request.url.params.get("type") == "metadataZip"
                assert request.url.params.get("autoApprove") == "false"
                assert "locationUploadMode" in request.url.params
                assert "locationHierarchy" in request.url.params
                assert "encounterUploadMode" in request.url.params
                # avni-server auth filter wants both AUTH-TOKEN and user-name.
                assert request.headers.get("AUTH-TOKEN")
                assert request.headers.get("user-name")
                # On success avni-server returns the literal "true"; on failure
                # callers can override import_body to inspect the error path.
                body = "true" if import_status == 200 else import_body
                if import_status == 200:
                    file_name = re.search(rb'filename="([^"]+)"', request.content).group(1).decode()
                    n = len(jobs)
                    jobs.insert(0, {"job": {
                        "fileName": file_name, "uuid": import_body if n == 0 else f"{import_body}-{n + 1}",
                        "status": "COMPLETED", "total": 19, "completed": 19, "skipped": 0,
                        **overrides[min(n, len(overrides) - 1)],
                    }, "status_calls": 0})
                return httpx.Response(import_status, content=body)
            if request.url.path == "/import/status":
                for entry in jobs:
                    entry["status_calls"] += 1
                listed = [entry["job"] for entry in jobs if entry["status_calls"] >= 2]
                return httpx.Response(200, json={"content": [*listed, earlier_job]})
            if request.url.path == "/import/errorfile":
                assert request.url.params.get("jobUuid") in {entry["job"]["uuid"] for entry in jobs}
                return httpx.Response(200, content=import_error_file)
            raise AssertionError(f"unexpected request to {request.url.path}")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(upload_routes.httpx, "AsyncClient", fake_async_client)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _patch_settings(
        monkeypatch,
        avni_server_base_url="https://x.test",
        ai_session_dir=str(tmp_path / "sessions"),
        input_root=str(tmp_path / "input"),
    )
    _install_stub_chat_service(monkeypatch)
    _install_fake_avni_server(monkeypatch)
    app = create_app()
    # Must use as a context manager so the FastAPI lifespan runs and
    # `app.state.store` gets populated.
    with TestClient(app) as c:
        yield c


# ── Session create / delete ──────────────────────────────────────────────────


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_create_session_returns_id_org_and_cookie(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    assert r.status_code == 201
    body = r.json()
    assert body["org_name"] == "Srijan"
    assert body["session_id"]
    assert body["expires_at"]
    assert "AI_SID" in r.cookies


def test_create_session_401_without_auth_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_settings(
        monkeypatch,
        avni_server_base_url="https://x.test",
        ai_session_dir=str(tmp_path / "s"),
    )
    _install_stub_chat_service(monkeypatch)
    _install_fake_avni_server(monkeypatch)
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/sessions")
        assert r.status_code == 401


def test_delete_session_204_and_idempotent(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert client.delete(f"/sessions/{sid}").status_code == 204


# ── Upload xlsx ───────────────────────────────────────────────────────────────


def test_upload_writes_files_to_session_workdir(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]

    files = [
        ("files", ("modelling.xlsx", io.BytesIO(b"PK\x03\x04stub-1"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("files", ("scoping.xlsx", io.BytesIO(b"PK\x03\x04stub-2"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    r = client.post(f"/sessions/{sid}/upload", files=files)
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert len(paths) == 2
    for p in paths:
        assert Path(p).read_bytes().startswith(b"PK\x03\x04")


def test_upload_rejects_non_xlsx(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    files = [("files", ("evil.exe", io.BytesIO(b"x"), "application/octet-stream"))]
    r = client.post(f"/sessions/{sid}/upload", files=files)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "E_BAD_FILE_TYPE"


@pytest.mark.parametrize("file_name", ["old.xls", "~$Modelling.xlsx", "notes.csv"])
def test_upload_rejects_unreadable_workbooks(client: TestClient, file_name: str) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    files = [("files", (file_name, io.BytesIO(b"x"), "application/octet-stream"))]
    r = client.post(f"/sessions/{sid}/upload", files=files)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "E_BAD_FILE_TYPE"
    assert "save an .xls file as .xlsx" in r.json()["detail"]["error"]


def test_a_rejected_upload_writes_none_of_its_files(client: TestClient) -> None:
    from config import settings

    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    files = [
        ("files", ("good.xlsx", io.BytesIO(b"PK\x03\x04"), "application/x")),
        ("files", ("old.xls", io.BytesIO(b"x"), "application/x")),
    ]
    r = client.post(f"/sessions/{sid}/upload", files=files)
    assert r.status_code == 400
    assert "'old.xls'" in r.json()["detail"]["error"]
    assert "good.xlsx" not in r.json()["detail"]["error"]
    assert not list(Path(settings.ai_session_dir).rglob("good.xlsx"))
    assert not list(Path(settings.input_root).rglob("good.xlsx"))


def test_a_second_chat_for_the_org_leaves_the_first_chats_upload(client: TestClient) -> None:
    first = client.post("/sessions", headers={"Authorization": "Bearer abc"}).json()["session_id"]
    files = [("files", ("Modelling.xlsx", io.BytesIO(b"PK\x03\x04"), "application/x"))]
    uploaded = Path(client.post(f"/sessions/{first}/upload", files=files).json()["paths"][0])

    second = client.post("/sessions", headers={"Authorization": "Bearer abc"}).json()["session_id"]

    assert second != first
    assert uploaded.read_bytes() == b"PK\x03\x04"
    assert first in uploaded.parts and second not in uploaded.parts


def test_upload_accepts_upper_case_xlsx(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    files = [("files", ("MODELLING.XLSX", io.BytesIO(b"PK\x03\x04"), "application/x"))]
    r = client.post(f"/sessions/{sid}/upload", files=files)
    assert r.status_code == 200
    assert Path(r.json()["paths"][0]).name == "MODELLING.XLSX"


def test_upload_404_when_session_missing(client: TestClient) -> None:
    files = [("files", ("a.xlsx", io.BytesIO(b"x"), "application/x"))]
    r = client.post("/sessions/does-not-exist/upload", files=files)
    assert r.status_code == 404


# ── Message + resolve ────────────────────────────────────────────────────────


def test_post_message_forwards_to_chat_service(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/message", json={"text": "generate srijan"})
    assert r.status_code == 200 and r.json() == {"accepted": True}

    store: SessionStore = client.app.state.store
    svc = store.get(sid).chat_service  # type: ignore[union-attr]
    assert svc.sent == ["generate srijan"]  # type: ignore[attr-defined]


def test_post_resolve_400_for_unknown_interrupt(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    r = client.post(
        f"/sessions/{sid}/resolve",
        json={"interrupt_id": "ghost", "resolutions": [{"change_id": "c1", "decision": "yes"}]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "E_BAD_INTERRUPT"


def test_post_resolve_200_when_known(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    store: SessionStore = client.app.state.store
    svc = store.get(sid).chat_service  # type: ignore[union-attr]
    svc._pending["i-1"] = {}  # type: ignore[attr-defined]
    r = client.post(
        f"/sessions/{sid}/resolve",
        json={"interrupt_id": "i-1", "resolutions": [{"change_id": "c1", "decision": "yes"}]},
    )
    assert r.status_code == 200 and r.json() == {"accepted": True}


# ── Bundle download + upload-to-avni ─────────────────────────────────────────


def test_get_bundle_404_when_not_ready(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    r = client.get(f"/sessions/{sid}/bundle")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "E_NO_BUNDLE"


def test_get_bundle_streams_zip_when_ready(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    store: SessionStore = client.app.state.store
    session = store.get(sid)
    assert session is not None

    # Simulate ChatService publishing bundle.ready by setting bundle_path.
    zip_path = session.workdir / "output" / "Srijan.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"PK\x03\x04stub-zip-bytes")
    session.bundle_path = zip_path

    r = client.get(f"/sessions/{sid}/bundle")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.content == b"PK\x03\x04stub-zip-bytes"


def test_upload_to_avni_404_when_no_bundle(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/upload-to-avni", headers={"AUTH-TOKEN": "tok"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "E_NO_BUNDLE"


def test_upload_to_avni_401_when_token_missing(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/upload-to-avni")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "E_AUTH"


def test_upload_to_avni_relays_zip_and_returns_job_id(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    store: SessionStore = client.app.state.store
    session = store.get(sid)
    assert session is not None
    zip_path = session.workdir / "output" / "Srijan.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"PK\x03\x04zipbytes")
    session.bundle_path = zip_path

    r = client.post(f"/sessions/{sid}/upload-to-avni", headers={"AUTH-TOKEN": "tok"})
    assert r.status_code == 200
    assert r.json()["job_id"] == "job-42"
    assert r.json()["status"] == "ok"


def test_upload_to_avni_401_when_avni_server_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _patch_settings(
        monkeypatch,
        avni_server_base_url="https://x.test",
        ai_session_dir=str(tmp_path / "s"),
    )
    _install_stub_chat_service(monkeypatch)
    _install_fake_avni_server(monkeypatch, import_status=401, import_body="nope")
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/sessions", headers={"Authorization": "Bearer abc"})
        sid = r.json()["session_id"]
        store: SessionStore = c.app.state.store
        session = store.get(sid)
        assert session is not None
        zip_path = session.workdir / "output" / "Srijan.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"PK")
        session.bundle_path = zip_path

        r = c.post(f"/sessions/{sid}/upload-to-avni", headers={"AUTH-TOKEN": "tok"})
        assert r.status_code == 401


# ── SSE: 409 on duplicate stream ─────────────────────────────────────────────


def test_get_events_returns_409_when_stream_in_use(client: TestClient) -> None:
    r = client.post("/sessions", headers={"Authorization": "Bearer abc"})
    sid = r.json()["session_id"]
    store: SessionStore = client.app.state.store
    session = store.get(sid)
    assert session is not None
    session.stream_in_use = True

    r = client.get(f"/sessions/{sid}/events")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "E_DUP_STREAM"


# ── upload-to-avni: the import job's outcome ─────────────────────────────────

# avni-server's error file for the chat-added field that could not be imported
# (Concern India UAT, prerelease, 17 Nov 2026), stack traces trimmed.
_CONCEPT_CLASH_ERROR_FILE = (
    "\ufeffconcepts.json,\"Concept with name 'Yes' already exists with different UUID: 8509a321\n"
    "org.avni.server.util.BadRequestError: Concept with name 'Yes' already exists with different UUID: 8509a321\n"
    "\tat org.avni.server.service.ConceptService.assertNotDuplicate(ConceptService.java:300)\n"
    "\"\n"
    "forms/Women Registration_b4d84445.json,\"Concept with uuid 'c5f027af' not found\n"
    "org.avni.server.builder.FormBuilderException: Concept with uuid 'c5f027af' not found\n"
    "\"\n"
)


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **fake_server: Any):
    """An app whose upload-to-avni talks to a fake avni-server."""
    _patch_settings(monkeypatch, avni_server_base_url="https://x.test", ai_session_dir=str(tmp_path / "s"))
    monkeypatch.setattr(upload_routes, "IMPORT_POLL_INTERVAL_SEC", 0.01)
    _install_stub_chat_service(monkeypatch)
    _install_fake_avni_server(monkeypatch, **fake_server)
    return create_app()


def _upload(c: TestClient) -> tuple[dict, ChatSession]:
    """Upload a bundle; returns the response body and the session."""
    sid = c.post("/sessions", headers={"Authorization": "Bearer abc"}).json()["session_id"]
    session = c.app.state.store.get(sid)
    zip_path = session.workdir / "output" / "Srijan.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"PK")
    session.bundle_path = zip_path
    r = c.post(f"/sessions/{sid}/upload-to-avni", headers={"AUTH-TOKEN": "tok"})
    assert r.status_code == 200
    return r.json(), session


def _upload_done_events(session: ChatSession) -> list[dict]:
    return [e.data for e in session.bus._buffer if e.type == "upload.done"]


def test_an_upload_is_ok_only_when_the_import_imported_every_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    with TestClient(_app(monkeypatch, tmp_path)) as c:
        body, session = _upload(c)
    assert body == {"job_id": "job-42", "status": "ok"}
    assert _upload_done_events(session) == [body]


def test_an_import_that_skipped_files_is_reported_as_failed_with_avnis_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = _app(monkeypatch, tmp_path, import_job={"completed": 17, "skipped": 2},
               import_error_file=_CONCEPT_CLASH_ERROR_FILE)
    with TestClient(app) as c:
        body, session = _upload(c)
        error_log = c.get(f"/sessions/{session.session_id}/upload-error-log")
    assert body["status"] == "failed"
    assert body["details"] == (
        "Avni did not import 2 of the 19 files in the app: "
        "concepts.json: Concept with name 'Yes' already exists with different UUID: 8509a321; "
        "forms/Women Registration_b4d84445.json: Concept with uuid 'c5f027af' not found"
    )
    assert body["error_log_available"] is True
    assert _upload_done_events(session) == [body]
    assert error_log.status_code == 200
    assert "assertNotDuplicate" in error_log.text


def test_a_failed_import_job_is_reported_as_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with TestClient(_app(monkeypatch, tmp_path, import_job={"status": "FAILED", "skipped": 0})) as c:
        body, _ = _upload(c)
    assert body["status"] == "failed"
    assert body["details"] == "Avni's import job ended with status FAILED."


def test_an_import_that_never_shows_its_outcome_is_not_reported_as_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(upload_routes, "IMPORT_WATCH_LIMIT_SEC", 0.05)
    with TestClient(_app(monkeypatch, tmp_path, import_job={"status": "STARTED"})) as c:
        body, session = _upload(c)
    assert body["status"] == "failed"
    assert body["details"] == upload_routes.IMPORT_NOT_CONFIRMED
    assert _upload_done_events(session) == [body]


def test_a_long_import_returns_importing_and_reports_its_outcome_when_it_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(upload_routes, "IMPORT_WAIT_IN_REQUEST_SEC", 0.05)
    monkeypatch.setattr(upload_routes, "IMPORT_WATCH_LIMIT_SEC", 0.3)
    with TestClient(_app(monkeypatch, tmp_path, import_job={"status": "STARTED"})) as c:
        body, session = _upload(c)
        assert body == {"job_id": "", "status": "importing"}
        assert _upload_done_events(session) == []
        c.portal.call(anyio.sleep, 0.6)
    later = _upload_done_events(session)
    assert [e["status"] for e in later] == ["failed"]
    assert later[0]["details"] == upload_routes.IMPORT_NOT_CONFIRMED


def test_each_upload_reports_its_own_import_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every upload of a bundle has the bundle's name; avni-server lists the
    user's jobs newest first, so a job is told apart by the upload's own name."""
    app = _app(monkeypatch, tmp_path, import_job=[{"skipped": 2}, {}])
    with TestClient(app) as c:
        first, session = _upload(c)
        second = c.post(f"/sessions/{session.session_id}/upload-to-avni", headers={"AUTH-TOKEN": "tok"}).json()
    assert (first["job_id"], first["status"]) == ("job-42", "failed")
    assert (second["job_id"], second["status"]) == ("job-42-2", "ok")
    assert [e["job_id"] for e in _upload_done_events(session)] == ["job-42", "job-42-2"]


def test_the_upload_carries_a_name_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    names: list[str] = []
    real_post = httpx.AsyncClient.post

    async def recording_post(self, url, **kwargs):
        names.append(kwargs["files"]["file"][0])
        return await real_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", recording_post)
    with TestClient(_app(monkeypatch, tmp_path)) as c:
        _upload(c)
    assert len(names) == 1
    assert re.fullmatch(r"Srijan-[0-9a-f]{12}\.zip", names[0])


def test_an_error_while_reading_the_outcome_still_tells_the_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app = _app(monkeypatch, tmp_path)

    async def broken_outcome(*args, **kwargs):
        raise KeyError("status")

    monkeypatch.setattr(upload_routes, "_import_outcome", broken_outcome)
    with TestClient(app) as c:
        body, session = _upload(c)
    assert body["status"] == "failed"
    assert body["details"] == upload_routes.IMPORT_NOT_CONFIRMED
    assert _upload_done_events(session) == [body]


def test_error_file_records_are_read_as_csv() -> None:
    error_file = (
        '"forms/Registration, Women_ab12.json","Invalid value ""x"" for Age\n\tat org.avni.X(X.java:1)\n"\n'
        'concepts.json,"Concept with name \'Yes\' already exists\nstack\n"\n'
    )
    assert upload_routes._import_error_reasons(error_file) == [
        'forms/Registration, Women_ab12.json: Invalid value "x" for Age',
        "concepts.json: Concept with name 'Yes' already exists",
    ]


def test_the_wait_for_the_import_counts_from_the_start_of_the_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A slow /import/new uses up the time the request may wait."""
    monkeypatch.setattr(upload_routes, "IMPORT_WAIT_IN_REQUEST_SEC", 0.2)
    real_post = httpx.AsyncClient.post

    async def slow_post(self, url, **kwargs):
        await anyio.sleep(0.3)
        return await real_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", slow_post)
    with TestClient(_app(monkeypatch, tmp_path)) as c:
        body, session = _upload(c)
        assert body == {"job_id": "", "status": "importing"}
        c.portal.call(anyio.sleep, 0.3)
    assert [e["status"] for e in _upload_done_events(session)] == ["ok"]

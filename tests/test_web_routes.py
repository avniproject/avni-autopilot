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
from pathlib import Path
from typing import Any

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
) -> None:
    """Replace verify_token and the upload-to-avni httpx client with mocks."""
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
                return httpx.Response(import_status, content=body)
            if request.url.path == "/import/status":
                # Production code calls this after a successful /import/new to
                # resolve the job UUID by fileName. import_body doubles as the
                # job UUID in this path.
                return httpx.Response(
                    200,
                    json={"content": [{"fileName": "Srijan.zip", "uuid": import_body}]},
                )
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
    r = client.post(f"/sessions/{sid}/upload-to-avni")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "E_NO_BUNDLE"


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

    r = client.post(f"/sessions/{sid}/upload-to-avni")
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

        r = c.post(f"/sessions/{sid}/upload-to-avni")
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

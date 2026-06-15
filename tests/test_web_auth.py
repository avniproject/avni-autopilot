"""Unit tests for `src/web/auth.py`.

Usage:
    pytest tests/test_web_auth.py
"""

from __future__ import annotations

import json

import httpx
import pytest

from web.auth import USER_INFO_PATH, AuthError, UserInfo, verify_token


# ── Mock transport helpers ────────────────────────────────────────────────────


def _client_returning(status: int, body: dict | str = "") -> httpx.AsyncClient:
    """Build an httpx.AsyncClient whose transport returns a canned response."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == USER_INFO_PATH
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, content=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client_capturing_header(captured: dict) -> httpx.AsyncClient:
    """Client that records the AUTH-TOKEN header it saw and returns 200."""
    def handler(request: httpx.Request) -> httpx.Response:
        captured["AUTH-TOKEN"] = request.headers.get("AUTH-TOKEN")
        return httpx.Response(200, json={"username": "u", "organisationName": "Org"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Happy path ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_token_returns_user_info_on_200() -> None:
    client = _client_returning(200, {
        "username": "alice",
        "organisationName": "Srijan",
        "roles": ["admin"],
    })
    info = await verify_token("https://x.test", "Bearer abc", client=client)
    assert info == UserInfo(username="alice", organisation_name="Srijan")


@pytest.mark.asyncio
async def test_verify_token_forwards_auth_token_header() -> None:
    captured: dict = {}
    client = _client_capturing_header(captured)
    await verify_token("https://x.test", "secret-token", client=client)
    assert captured["AUTH-TOKEN"] == "secret-token"


@pytest.mark.asyncio
async def test_verify_token_strips_trailing_slash_in_base_url() -> None:
    captured_url: dict = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(200, json={"username": "u", "organisationName": "Org"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await verify_token("https://x.test/", "Bearer t", client=client)
    assert captured_url["url"] == f"https://x.test{USER_INFO_PATH}"


# ── Failure modes ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_token_raises_401_on_invalid_token() -> None:
    client = _client_returning(401, {"message": "bad token"})
    with pytest.raises(AuthError) as exc_info:
        await verify_token("https://x.test", "Bearer bad", client=client)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_raises_when_base_url_missing() -> None:
    client = _client_returning(200, {})
    with pytest.raises(AuthError) as exc_info:
        await verify_token("", "Bearer t", client=client)
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_verify_token_raises_401_when_no_token() -> None:
    client = _client_returning(200, {})
    with pytest.raises(AuthError) as exc_info:
        await verify_token("https://x.test", "", client=client)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_raises_401_when_user_has_no_org() -> None:
    client = _client_returning(200, {"username": "alice", "organisationName": ""})
    with pytest.raises(AuthError) as exc_info:
        await verify_token("https://x.test", "Bearer t", client=client)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_propagates_5xx_as_bad_gateway_or_passthrough() -> None:
    client = _client_returning(503, {"message": "down"})
    with pytest.raises(AuthError) as exc_info:
        await verify_token("https://x.test", "Bearer t", client=client)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_verify_token_502_on_non_json_body() -> None:
    client = _client_returning(200, "not json")
    with pytest.raises(AuthError) as exc_info:
        await verify_token("https://x.test", "Bearer t", client=client)
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_verify_token_502_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError) as exc_info:
        await verify_token("https://x.test", "Bearer t", client=client)
    assert exc_info.value.status_code == 502

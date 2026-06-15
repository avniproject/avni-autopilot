"""avni-server auth verification helpers.

Single responsibility: take the `Authorization` header the browser sent,
forward it to avni-server's `/web/userInfo` endpoint, and return the
username + organisation name on 200 — or `None` on 401.

The token is never logged. Per SDD §6 it lives only in the in-memory
`ChatSession` record and is dropped on session expiry. Used by
`web.routes.sessions.create_session` and `web.routes.upload.upload_to_avni`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Endpoint avni-webapp itself uses to fetch the logged-in user (saga.jsx).
# `/me` is a deprecated alias on avni-server — use the canonical path.
USER_INFO_PATH = "/web/userInfo"

# How long to wait on avni-server before giving up. Token verification runs
# once per session creation, so a few seconds is generous.
VERIFY_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class UserInfo:
    """Subset of avni-server's `UserInfoContract` the service needs.

    Other fields (privileges, groups, catchment) flow back to the browser
    via avni-webapp's own session — this service only needs to know who
    the request is for, and which org to scope the work to.
    """

    username: str
    organisation_name: str


class AuthError(Exception):
    """Raised when avni-server returns a non-200 from `/web/userInfo`.

    `status_code == 401` means the token is invalid; the caller should
    propagate that to the browser. Any other status means avni-server is
    unhealthy or the URL is wrong.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def verify_token(
    base_url: str,
    auth_token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> UserInfo:
    """Forward the user's auth token to avni-server and return user info.

    avni-server expects the token on the `AUTH-TOKEN` header — that is the
    convention CognitoWebClient.jsx / KeycloakWebClient.jsx use; the
    `Authorization` header is NOT accepted.

    Args:
        base_url: avni-server base URL (e.g. `https://staging.avniproject.org`).
        auth_token: Opaque token captured from the browser's `AUTH-TOKEN`
            header (or `Authorization` fallback). Forwarded verbatim to
            avni-server.
        client: Optional pre-built `httpx.AsyncClient` — useful for tests with
            a mocked transport. When omitted, a one-shot client is constructed.

    Raises:
        AuthError(401, ...): The token is invalid; propagate to the browser.
        AuthError(5xx, ...): avni-server is down or the URL is misconfigured.
    """
    if not base_url:
        raise AuthError(500, "AVNI_SERVER_BASE_URL is not configured")
    if not auth_token:
        raise AuthError(401, "missing AUTH-TOKEN header")

    url = base_url.rstrip("/") + USER_INFO_PATH
    headers = {"AUTH-TOKEN": auth_token}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SEC)
    try:
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise AuthError(502, f"avni-server unreachable: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code == 401:
        raise AuthError(401, "avni-server rejected the token")
    if response.status_code != 200:
        raise AuthError(
            response.status_code,
            f"avni-server /web/userInfo returned {response.status_code}",
        )

    try:
        body: dict[str, Any] = response.json()
    except ValueError as exc:
        raise AuthError(502, f"avni-server returned non-JSON: {exc}") from exc

    username = (body.get("username") or "").strip()
    organisation_name = (body.get("organisationName") or "").strip()
    if not organisation_name:
        # An admin without an org cannot use this service (the bundle is
        # always scoped to one org). Treat as 401 — the user is real but
        # not authorised to drive bundle generation here.
        raise AuthError(401, "user has no organisation; cannot scope a bundle")

    return UserInfo(username=username, organisation_name=organisation_name)

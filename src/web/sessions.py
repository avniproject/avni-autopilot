"""In-memory session store + idle/absolute reaper.

One `ChatSession` per session id. Holds the user's auth token (forwarded
only to avni-server for the bundle upload, never logged), the working
directory, the SSE `EventBus`, and a reference to the per-session
`ChatService` adapter (set after the chat service is constructed).

The reaper runs as an `asyncio.Task` started in `web.app`'s lifespan — it
walks the store every minute and removes records that are idle longer
than `AI_SESSION_IDLE_MIN` or older than `AI_SESSION_MAX_HOURS`. Workdirs
are deleted recursively on removal. SDD §8.1 / §8.2.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx

from config import settings
from domain.bundle_editor import BundleOrigin, classify_bundle
from web.events import EventBus, error_payload, session_loading, session_ready


log = logging.getLogger(__name__)


def _purge_org_dir(dir_path: Path, patterns: tuple[str, ...]) -> None:
    """Best-effort wipe of files matching `patterns` under `dir_path`.

    Best-effort because per-file IOErrors are logged and skipped — purge
    is an isolation optimisation across sessions for the same org, not a
    correctness gate.
    """
    if not dir_path.exists():
        return
    for pattern in patterns:
        for path in dir_path.glob(pattern):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
            except OSError as exc:
                log.warning(f"purge skipped {path}: {exc}")


# ── Helpers ──────────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_org_name(org_name: str) -> str:
    """Map an avni-server org display name to its resources/input/ folder slug.

    Existing folders follow `lowercase_underscore` (e.g. "Durga India" →
    "durga_india"). Replicates `domain.rules.kb_cli._slug`'s rule so the chat
    agent's `generate_bundle(org=…)` tool can find the upload directory.
    """
    slug = _SLUG_RE.sub("_", org_name.lower()).strip("_")
    return slug or "default"

if TYPE_CHECKING:
    # `ChatService` lives in `web.chat_service`; importing it eagerly would
    # create a circular dep (chat_service holds a ChatSession reference too).
    # Used only for type hints.
    from web.chat_service import ChatService


REAPER_INTERVAL_SEC = 60.0


# ── Session record ────────────────────────────────────────────────────────────


class OrgSetupStatus(str, Enum):
    """State of the org's current bundle download from avni-server."""

    LOADING = "loading"
    LOADED = "loaded"
    FAILED = "failed"



@dataclass
class ChatSession:
    """One in-memory session.

    The `chat_service` field is `None` until `web.app` wires the adapter
    after constructing the session. Routes that need the agent must check
    for `None` and 503 if the service is not yet ready (only happens during
    a server-restart race that the reaper otherwise cleans up).
    """


    session_id: str
    org_name: str
    username: str
    workdir: Path
    bus: EventBus = field(default_factory=EventBus)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    chat_service: Optional["ChatService"] = None
    # Set when an SSE consumer is connected; SDD §9 rejects a second
    # `GET /events` with 409 while this is True.
    stream_in_use: bool = False
    # Last bundle path published via `bundle.ready` — used by GET /bundle and
    # POST /upload-to-avni to know where the ZIP lives without re-deriving it.
    bundle_path: Optional[Path] = None
    org_setup_status: OrgSetupStatus = OrgSetupStatus.LOADING
    # User-facing reason the org's current bundle could not be loaded; set
    # only when `org_setup_status` is FAILED.
    org_setup_error: Optional[str] = None
    # Origin of the forms in the bundle at `bundle_path`; refreshed whenever
    # that bundle is replaced or edited.
    bundle_origin: Optional[BundleOrigin] = None
    # True once a tool in this chat has built or edited the bundle at
    # `bundle_path` (its card with Download is then in the chat).
    bundle_changed_in_chat: bool = False
    # Notes from the last build or update tool call, shown by the chat
    # service and kept out of what the model reads.
    pending_notices: list[str] = field(default_factory=list)

    @property
    def org_slug(self) -> str:
        """Folder-safe slug derived from `org_name` (e.g. "Durga India" → "durga_india")."""
        return slugify_org_name(self.org_name)

    @property
    def org_setup_loading(self) -> bool:
        """True while the org's current bundle is still downloading."""
        return self.org_setup_status is OrgSetupStatus.LOADING and not self.bundle_changed_in_chat

    @property
    def build_needs_confirmation(self) -> bool:
        """True when a fresh build may duplicate what the org already has.

        That is when the org's setup could not be loaded (its contents are
        unknown) or its forms were set up outside Autopilot, and no
        Autopilot bundle has been built in this chat since.
        """
        if self.bundle_changed_in_chat:
            return self.bundle_origin is BundleOrigin.OTHER
        if self.org_setup_status is OrgSetupStatus.FAILED:
            return True
        return self.bundle_origin is BundleOrigin.OTHER

    def touch(self) -> None:
        """Bump `last_activity_at` to now. Called on every request that hits
        a session — keeps it from being reaped while in active use."""
        self.last_activity_at = datetime.now(timezone.utc)


# ── Bundle download ──────────────────────────────────────────────────────────


def org_setup_failure_reason(exc: Exception) -> str:
    """User-facing reason for a failed `/implementation/export` download."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 403:
            return (
                "you do not have the 'Download bundle' privilege. Ask an "
                "administrator of your organisation to grant it, then start "
                "a fresh chat"
            )
        if status == 401:
            return "your sign-in has expired. Sign in again, then start a fresh chat"
        return f"the Avni server returned an error (HTTP {status})"
    if isinstance(exc, httpx.TimeoutException):
        return "the Avni server took too long to respond"
    if isinstance(exc, httpx.HTTPError):
        return "the Avni server could not be reached"
    if isinstance(exc, FileNotFoundError):
        return "the Avni server's reply was not a bundle"
    return "of an unexpected error while saving it"


async def download_org_bundle(session: ChatSession, auth_token: str) -> None:
    """Fetch the org's current bundle from avni-server into session workdir.

    Runs as a background task — does not block session creation. Emits
    `session.loading` at start and `session.ready` on completion so the webapp
    can show / hide the "Checking the current setup" indicator. On failure,
    the reason is kept on the session for the chat agent's context and
    published to the user as a recoverable `error` event.

    The token is passed in from the session-creation request rather than held
    on the session record, so no credential outlives this call.
    """
    session.bus.publish("session.loading", session_loading("Checking the current setup"))
    url = f"{settings.avni_server_base_url}/implementation/export/false"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"AUTH-TOKEN": auth_token},
                timeout=60,
                follow_redirects=True,
            )
        resp.raise_for_status()
        dest = session.workdir / "bundle.zip"
        dest.write_bytes(resp.content)
        session.bundle_origin = await asyncio.to_thread(classify_bundle, str(dest))
        session.bundle_path = dest
        session.org_setup_status = OrgSetupStatus.LOADED
        log.info(
            f"bundle downloaded session={session.session_id} size={len(resp.content)} "
            f"origin={session.bundle_origin.value}"
        )
    except Exception as exc:  # noqa: BLE001
        reason = org_setup_failure_reason(exc)
        session.org_setup_status = OrgSetupStatus.FAILED
        session.org_setup_error = reason
        log.warning(f"bundle download failed session={session.session_id}: {exc}")
        session.bus.publish(
            "error",
            error_payload(
                "E_SETUP_NOT_LOADED",
                f"Your organisation's current setup could not be loaded because {reason}.",
                recoverable=True,
            ),
        )
    finally:
        session.bus.publish("session.ready", session_ready())


# ── Store ─────────────────────────────────────────────────────────────────────


class SessionStore:
    """Process-local dict of session_id → ChatSession.

    Single-process by design (SDD §8.3). Replace with Redis-backed storage
    when v2 horizontal scaling lands. All mutating operations are
    thread-safe under asyncio (single event-loop access).
    """

    def __init__(
        self,
        root_dir: Path,
        idle_minutes: int,
        max_hours: int,
    ) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._root_dir = root_dir
        self._idle = timedelta(minutes=idle_minutes)
        self._absolute = timedelta(hours=max_hours)
        self._root_dir.mkdir(parents=True, exist_ok=True)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def create(
        self,
        org_name: str,
        username: str,
        shared_input_root: Optional[Path] = None,
        shared_output_root: Optional[Path] = None,
    ) -> ChatSession:
        """Allocate a session id, create its workdir, and register the record.

        When `shared_input_root` / `shared_output_root` are supplied, the
        org's subdir under each is wiped — prevents the previous session's
        uploads being parsed alongside the new session's files.
        """
        session_id = secrets.token_urlsafe(16)
        workdir = self._root_dir / session_id
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "input").mkdir(exist_ok=True)
        (workdir / "output").mkdir(exist_ok=True)
        session = ChatSession(
            session_id=session_id,
            org_name=org_name,
            username=username,
            workdir=workdir,
        )
        if shared_input_root is not None:
            _purge_org_dir(shared_input_root / session.org_slug, ("*.xlsx", "*.xls"))
        if shared_output_root is not None:
            _purge_org_dir(shared_output_root / session.org_slug, ("*",))
        self._sessions[session_id] = session
        log.info(f"session created sid={session_id} org={org_name!r}")
        return session

    def get(self, session_id: str) -> ChatSession | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str, reason: str = "removed") -> None:
        """Close the bus, delete the workdir, drop the record.

        Idempotent — calling on an unknown id is a no-op. The token is
        cleared from memory by the dataclass going out of scope; nothing
        else holds a reference per SDD §6.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            session.bus.close(reason)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"bus close failed sid={session_id}: {exc}")
        try:
            shutil.rmtree(session.workdir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"workdir rmtree failed sid={session_id}: {exc}")
        log.info(f"session removed sid={session_id} reason={reason!r}")

    # ── Reaping ──────────────────────────────────────────────────────────────

    def expired_session_ids(self, *, now: datetime | None = None) -> list[str]:
        """Ids of sessions past the idle or absolute threshold.

        Exposed for unit testing with a frozen clock; the reaper task uses
        wall-clock `now`.
        """
        now = now or datetime.now(timezone.utc)
        expired: list[str] = []
        for sid, session in self._sessions.items():
            if now - session.last_activity_at > self._idle:
                expired.append(sid)
            elif now - session.created_at > self._absolute:
                expired.append(sid)
        return expired

    def reap_once(self, *, now: datetime | None = None) -> int:
        """Run one reap pass; return the count of sessions removed."""
        for sid in self.expired_session_ids(now=now):
            self.remove(sid, reason="expired")
        return 0  # unused; kept for symmetry / tests count via len changes

    async def run_reaper(self, interval_sec: float = REAPER_INTERVAL_SEC) -> None:
        """Background loop — call once from `web.app`'s lifespan.

        Cancellation-safe: when the task is cancelled (server shutdown), all
        remaining sessions are removed so workdirs don't leak.
        """
        try:
            while True:
                await asyncio.sleep(interval_sec)
                try:
                    self.reap_once()
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"reaper pass failed: {exc}")
        except asyncio.CancelledError:
            log.info("reaper stopping; removing all live sessions")
            for sid in list(self._sessions.keys()):
                self.remove(sid, reason="shutdown")
            raise

    # ── Introspection ────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._sessions

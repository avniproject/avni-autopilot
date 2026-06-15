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
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from web.events import EventBus


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

log = logging.getLogger(__name__)

REAPER_INTERVAL_SEC = 60.0


# ── Session record ────────────────────────────────────────────────────────────


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
    auth_token: str
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

    @property
    def org_slug(self) -> str:
        """Folder-safe slug derived from `org_name` (e.g. "Durga India" → "durga_india")."""
        return slugify_org_name(self.org_name)

    def touch(self) -> None:
        """Bump `last_activity_at` to now. Called on every request that hits
        a session — keeps it from being reaped while in active use."""
        self.last_activity_at = datetime.now(timezone.utc)


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

    def create(self, org_name: str, username: str, auth_token: str) -> ChatSession:
        """Allocate a session id, create its workdir, and register the record."""
        session_id = secrets.token_urlsafe(16)
        workdir = self._root_dir / session_id
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "input").mkdir(exist_ok=True)
        (workdir / "output").mkdir(exist_ok=True)
        session = ChatSession(
            session_id=session_id,
            org_name=org_name,
            username=username,
            auth_token=auth_token,
            workdir=workdir,
        )
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

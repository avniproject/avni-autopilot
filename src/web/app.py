"""FastAPI app for `avni-ai-web`.

Mounted endpoints (SDD §4.1):

    POST   /sessions
    DELETE /sessions/{session_id}
    POST   /sessions/{session_id}/upload
    POST   /sessions/{session_id}/upload-to-avni
    POST   /sessions/{session_id}/message
    POST   /sessions/{session_id}/resolve
    GET    /sessions/{session_id}/events     (SSE)
    GET    /sessions/{session_id}/bundle
    GET    /health                           (operations)

Lifecycle: on startup, allocates the SessionStore on `app.state.store` and
launches the reaper as a background task; on shutdown, cancels the reaper
(which removes any still-live sessions and their workdirs).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from domain.docs.knowledge_base import DocsKnowledgeBase
from domain.rules.knowledge_base import KnowledgeBase as RulesKnowledgeBase
from logging_setup import setup_logging
from web.routes import bundle as bundle_routes
from web.routes import chat as chat_routes
from web.routes import sessions as sessions_routes
from web.routes import upload as upload_routes
from web.sessions import SessionStore

log = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────


def _rebuild_knowledge_bases() -> None:
    """Rebuild the docs + rules KB embedding caches in a background thread on
    startup. Sequential on purpose — both hit Voyage, and free-tier rate
    limits punish concurrent requests."""
    if not os.environ.get("VOYAGE_API_KEY"):
        log.info("VOYAGE_API_KEY not set — skipping KB rebuild on startup")
        return
    try:
        DocsKnowledgeBase().rebuild()
    except Exception as exc:
        log.warning(f"Docs KB rebuild on startup failed: {exc}")
    try:
        RulesKnowledgeBase().rebuild()
    except Exception as exc:
        log.warning(f"Rules KB rebuild on startup failed: {exc}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Allocate the session store + reaper for the life of the process."""
    store = SessionStore(
        root_dir=Path(settings.ai_session_dir),
        idle_minutes=settings.ai_session_idle_min,
        max_hours=settings.ai_session_max_hours,
    )
    app.state.store = store
    reaper = asyncio.create_task(store.run_reaper())
    # Rebuild KB embeddings in a thread so startup is non-blocking.
    # Content-hash gating makes this a near-instant no-op when nothing changed.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _rebuild_knowledge_bases)
    log.info(
        f"avni-ai-web started — session_dir={settings.ai_session_dir} "
        f"idle_min={settings.ai_session_idle_min} max_hours={settings.ai_session_max_hours}"
    )
    try:
        yield
    finally:
        reaper.cancel()
        try:
            await reaper
        except asyncio.CancelledError:
            pass
        log.info("avni-ai-web stopped")


# ── App factory ──────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """Build a fresh FastAPI app. Exposed for tests + the `run()` entry point."""
    app = FastAPI(
        title="avni-ai-web",
        description="HTTP + SSE shell over the Avni AI bundle-generation agent.",
        version="0.1.0",
        lifespan=_lifespan,
    )

    origins = [o.strip() for o in settings.ai_webapp_origin.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Content-Type", "Last-Event-ID"],
        )

    @app.get("/health")
    async def health() -> dict[str, bool]:
        """Liveness probe for the ALB. Always 200 if the process is up."""
        return {"ok": True}

    app.include_router(sessions_routes.router)
    app.include_router(upload_routes.router)
    app.include_router(chat_routes.router)
    app.include_router(bundle_routes.router)
    return app


# Module-level app instance — `uvicorn web.app:app` uses this directly.
app = create_app()


# ── Entry point for `avni-ai-web` console script ─────────────────────────────


def run() -> None:
    """`avni-ai-web` (declared in pyproject.toml `[project.scripts]`)."""
    import uvicorn

    setup_logging()
    if not settings.avni_server_base_url:
        log.warning(
            "AVNI_SERVER_BASE_URL is not set — token verification and "
            "bundle upload will fail until it is configured."
        )

    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=settings.ai_web_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()

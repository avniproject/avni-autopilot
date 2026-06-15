"""FastAPI service that wraps the avni-chat ReAct agent behind HTTP + SSE.

Entry point: `avni-ai-web` (defined in pyproject.toml `[project.scripts]`),
which calls `web.app:run`. The chat REPL and the rules CLI are unaffected;
this package only exposes the existing agent and pipeline over HTTP.

Layout:

    src/web/
    ├── app.py            FastAPI app, CORS, lifespan, run() entry point
    ├── auth.py           avni-server /me verification + token-relay helpers
    ├── chat_service.py   Adapter — agent.astream events → SSE event payloads
    ├── events.py         SSE event types, encoder, replay buffer
    ├── sessions.py       ChatSession dataclass, in-memory store, reaper
    └── routes/
        ├── sessions.py   POST /sessions, DELETE /sessions/{id}
        ├── upload.py     POST /sessions/{id}/upload, /upload-to-avni
        ├── chat.py       POST /message, /resolve; GET /events (SSE)
        └── bundle.py     GET /sessions/{id}/bundle

See specs/AVNI_WEBAPP_INTEGRATION_SDD.md for the contract.
"""

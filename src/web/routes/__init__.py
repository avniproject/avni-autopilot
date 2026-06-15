"""FastAPI routers, one per concern.

Each module exposes a `router: APIRouter` that `web.app` mounts. Grouping is
by failure mode (per SDD §9): session lifecycle, file I/O + avni-server
relay, chat + SSE, and bundle download.
"""

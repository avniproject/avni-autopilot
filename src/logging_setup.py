"""
Logging configuration. Call `setup_logging()` once at the start of any
entry point (REPL, script, test).

The default level is set to WARNING so noisy 3rd-party packages (langchain,
anthropic, httpx) stay quiet; our own modules under `pipeline.*` and
`domain.*` are raised to INFO so progress logs surface.
"""

from __future__ import annotations

import contextlib
import logging
import time

from config import settings


def setup_logging(level: str | None = None) -> None:
    root_level = (level or settings.log_level).upper()
    logging.basicConfig(level=root_level, format="%(message)s")
    # Our own modules at INFO; 3rd-party noise stays at the root level.
    for name in ("pipeline", "pipeline.nodes", "domain.enricher", "domain.llm"):
        logging.getLogger(name).setLevel(logging.INFO)


@contextlib.contextmanager
def log_node(name: str, **fields):
    """Context manager to log node start/end + elapsed time.

    Usage inside a node:
        with log_node("discover_files", org=state["org_name"]):
            ...
    """
    log = logging.getLogger(f"pipeline.nodes.{name}")
    extras = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info("→ %s %s", name, extras)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = (time.perf_counter() - t0) * 1000
        log.info("← %s done (%.1fms)", name, dt)

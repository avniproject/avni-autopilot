"""
Logging configuration. Call `setup_logging()` once at the start of any
entry point (REPL, script, test).

Layout:
  - Diagnostic logs from our modules (`pipeline.*`, `domain.*`) and library
    warnings go to a rotating file (default `logs/avni.log`). Format includes
    timestamp + level + logger name.
  - The terminal only sees WARNING and above, so the chat REPL stays clean
    while INFO-level progress still gets captured on disk.

Override the log path with the `LOG_FILE` env var. Override the threshold
with `LOG_LEVEL` (applies to the file handler; console is fixed at WARNING).
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import time

from config import settings


def setup_logging(level: str | None = None) -> None:
    file_level = (level or settings.log_level).upper()

    os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    # Loggers whose WARNINGs are noisy library chatter, not actionable signal.
    # They still land in the log file via the file handler; we only hide them
    # from the chat terminal.
    #   py.warnings                          — pandas/openpyxl `warnings.warn`
    #   langgraph.checkpoint.serde.jsonplus  — "Deserializing unregistered type
    #                                          models.EntitySpec..." heads-up
    _CONSOLE_MUTED = {"py.warnings", "langgraph.checkpoint.serde.jsonplus"}
    console_handler.addFilter(lambda r: r.name not in _CONSOLE_MUTED)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Replace any handlers basicConfig or a previous call may have installed.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Our own modules at INFO so progress logs reach the file even when the
    # global level is WARNING. 3rd-party noise stays at the root level.
    for name in ("pipeline", "pipeline.nodes", "domain.enricher", "domain.llm"):
        logging.getLogger(name).setLevel(logging.INFO)

    # Funnel `warnings.warn(...)` (pandas/openpyxl) into the same log file
    # instead of letting them print to stderr alongside the chat stream.
    logging.captureWarnings(True)


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

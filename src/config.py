"""
Centralised settings, read from environment / .env once at startup.

Every other module reads `settings` instead of duplicating constants. The
.env file is loaded on first import; missing values fall back to defaults.

Currently we don't depend on `pydantic-settings` — the project's existing
dependency is plain `pydantic`. A lightweight dataclass + manual env read
keeps the dependency footprint unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Project root = parent of src/
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env_file(os.path.join(_PROJECT_ROOT, ".env"))


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    project_root: str = _PROJECT_ROOT
    input_root: str = _env("BUNDLE_INPUT_ROOT", os.path.join(_PROJECT_ROOT, "resources", "input"))
    output_root: str = _env("BUNDLE_OUTPUT_ROOT", os.path.join(_PROJECT_ROOT, "resources", "output"))

    # Anthropic / LLM
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")
    model: str = _env("CHAT_MODEL", "claude-sonnet-4-6")
    max_tokens: int = _env_int("CHAT_MAX_TOKENS", 8192)

    # LangSmith tracing (optional). Set LANGSMITH_TRACING=true and provide
    # LANGSMITH_API_KEY to send traces. Project defaults to the package name.
    langsmith_tracing: bool = _env("LANGSMITH_TRACING", "").lower() in {"1", "true", "yes"}
    langsmith_api_key: str = _env("LANGSMITH_API_KEY", "")
    langsmith_project: str = _env("LANGSMITH_PROJECT", "avni-ai-tools")
    langsmith_endpoint: str = _env("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")

    # Logging. File-handler threshold; the console handler stays at WARNING+
    # regardless so the chat REPL is clean. INFO surfaces per-form progress.
    log_level: str = _env("LOG_LEVEL", "INFO")
    # Diagnostic logs are written here; the chat REPL stays clean.
    # Relative paths resolve against the project root.
    log_file: str = _env("LOG_FILE", os.path.join(_PROJECT_ROOT, "logs", "avni.log"))


settings = Settings()


# Re-export LangSmith settings into the environment so the `langsmith` SDK
# (which LangChain delegates to) picks them up. Done once at import time.
# `setdefault` lets explicitly-set shell env vars win over .env values.
if settings.langsmith_tracing and settings.langsmith_api_key:
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)

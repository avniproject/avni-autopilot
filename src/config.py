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

    # Logging
    log_level: str = _env("LOG_LEVEL", "WARNING")


settings = Settings()

"""Configuration for the CampusPilot agent layer.

Values come from the environment so the demo can be reconfigured without
code changes. Nothing here ever hardcodes a secret.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Marker returned by the mock backend / used for ID prefix validation.
IST_TIMEZONE = "Asia/Kolkata"
IST = ZoneInfo(IST_TIMEZONE)
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Hard caps used by the agent (kept here so tests and validators agree).
MAX_TOOL_ITERATIONS = 6
LLM_TIMEOUT_S = 30.0
MAX_RETRIES = 2
MAX_HISTORY_TURNS = 12
MAX_CORRECTIONS = 2

MAX_RANGE_DAYS = 14
MIN_RANGE_DAYS = 0
MIN_MINUTES = 5
MAX_MINUTES = 480
MIN_DAYS_AHEAD = 1
MAX_DAYS_AHEAD = 30

_ENV_LOADED = False


def _load_env_once(env_file: str | Path | None = None) -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    # override=False: a real shell/CI environment always wins over .env.
    load_dotenv(dotenv_path=env_file, override=False)
    _ENV_LOADED = True


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class AgentConfig:
    """Immutable snapshot of agent settings."""

    gemini_api_key: str | None = None
    gemini_model: str = DEFAULT_GEMINI_MODEL
    max_tool_iterations: int = MAX_TOOL_ITERATIONS
    llm_timeout_s: float = LLM_TIMEOUT_S
    max_retries: int = MAX_RETRIES
    max_history_turns: int = MAX_HISTORY_TURNS
    max_corrections: int = MAX_CORRECTIONS
    max_range_days: int = MAX_RANGE_DAYS
    timezone: str = IST_TIMEZONE

    def require_api_key(self) -> str:
        """Return the API key or raise a clear, non-leaking error."""
        if not self.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env (see .env.example) "
                "or run with the mock/fallback path."
            )
        return self.gemini_api_key


def load_config(env_file: str | Path | None = None) -> AgentConfig:
    """Read configuration from the environment (and optionally a .env file)."""
    _load_env_once(env_file)
    key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    return AgentConfig(
        gemini_api_key=key or None,
        gemini_model=model,
        max_tool_iterations=_int_env("AGENT_MAX_TOOL_ITERATIONS", MAX_TOOL_ITERATIONS),
        llm_timeout_s=_float_env("AGENT_LLM_TIMEOUT_S", LLM_TIMEOUT_S),
        max_retries=_int_env("AGENT_MAX_RETRIES", MAX_RETRIES),
        max_history_turns=_int_env("AGENT_MAX_HISTORY_TURNS", MAX_HISTORY_TURNS),
        max_corrections=_int_env("AGENT_MAX_CORRECTIONS", MAX_CORRECTIONS),
        max_range_days=_int_env("AGENT_MAX_RANGE_DAYS", MAX_RANGE_DAYS),
    )

"""FIX (deploy-ready, item 3/4): in-process live-AI budget and IP throttles.

The public demo runs on a free host in a single uvicorn worker, so a
process-local counter is enough to protect the owner's Gemini key. Nothing here
is persisted: a restart starts the day over, which is acceptable for a demo and
is documented in the README.

Two separate ideas live here:

* :class:`LiveBudget` -- how many *real* Gemini requests may be made today
  (global) and per visitor per hour. Every attempt counts, retries included.
* :class:`SlidingWindowLimiter` -- cheap abuse limits for HTTP endpoints
  (``/chat``, ``/demo/reset``), so one visitor cannot hammer the app.

Neither module-level instance ever stores a key or a message body.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from datetime import date, datetime
from typing import Deque

from agent.config import IST

DEFAULT_LIVE_CALLS_PER_DAY = 15
DEFAULT_LIVE_CALLS_PER_IP_PER_HOUR = 5
HOUR_SECONDS = 3600.0
MAX_IP_LENGTH = 64


def now_ist() -> datetime:
    """Current time in IST; the daily counter resets at IST midnight."""
    return datetime.now(IST)


def _int_env(name: str, default: int) -> int:
    """Read a non-negative integer from the environment, else the default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def client_ip_from_headers(peer: str | None, forwarded_for: str | None) -> str:
    """Best-effort visitor IP, including behind a host's TLS proxy.

    Render/Railway terminate TLS and add ``X-Forwarded-For``; the value is
    client-controlled in general, so it is only ever used for throttling -- a
    spoofed value can cost the spoofer their own quota, never the key.
    """
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first[:MAX_IP_LENGTH]
    return (peer or "unknown")[:MAX_IP_LENGTH]


class LiveBudget:
    """Global daily + per-IP hourly budget for real Gemini requests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._day: date | None = None
        self._used = 0
        self._ips: dict[str, Deque[float]] = {}

    # -- configuration (read at call time so tests can change env) --------
    def limits(self) -> tuple[int, int]:
        return (
            _int_env("LIVE_CALLS_PER_DAY", DEFAULT_LIVE_CALLS_PER_DAY),
            _int_env("LIVE_CALLS_PER_IP_PER_HOUR", DEFAULT_LIVE_CALLS_PER_IP_PER_HOUR),
        )

    def _roll(self) -> None:
        today = now_ist().date()
        if today != self._day:
            self._day = today
            self._used = 0
            self._ips.clear()

    # -- public API ------------------------------------------------------
    def remaining(self) -> int:
        daily_limit, _ = self.limits()
        with self._lock:
            self._roll()
            return max(0, daily_limit - self._used)

    def acquire(self, client_ip: str) -> str | None:
        """Reserve one live request. Returns None, or ``"daily"``/``"per_ip"``."""
        daily_limit, per_ip_limit = self.limits()
        key = (client_ip or "unknown")[:MAX_IP_LENGTH]
        now = time.monotonic()
        with self._lock:
            self._roll()
            if self._used >= daily_limit:
                return "daily"
            stamps = self._ips.setdefault(key, deque())
            cutoff = now - HOUR_SECONDS
            while stamps and stamps[0] < cutoff:
                stamps.popleft()
            if len(stamps) >= per_ip_limit:
                return "per_ip"
            self._used += 1
            stamps.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._day = None
            self._used = 0
            self._ips.clear()


class SlidingWindowLimiter:
    """At most ``limit`` events per ``window_seconds`` for each key."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = threading.RLock()
        self._events: dict[str, Deque[float]] = {}

    def allow(self, key: str) -> bool:
        name = (key or "unknown")[:MAX_IP_LENGTH]
        now = time.monotonic()
        with self._lock:
            stamps = self._events.setdefault(name, deque())
            cutoff = now - self.window_seconds
            while stamps and stamps[0] < cutoff:
                stamps.popleft()
            if len(stamps) >= self.limit:
                return False
            stamps.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


# Shared instances: one process, one demo, one budget.
budget = LiveBudget()
chat_limiter = SlidingWindowLimiter(limit=10, window_seconds=60.0)
reset_limiter = SlidingWindowLimiter(limit=3, window_seconds=HOUR_SECONDS)
login_limiter = SlidingWindowLimiter(limit=20, window_seconds=60.0)


def reset_all() -> None:
    """Clear every counter (tests, and the /demo/reset endpoint)."""
    budget.reset()
    chat_limiter.reset()
    reset_limiter.reset()
    login_limiter.reset()

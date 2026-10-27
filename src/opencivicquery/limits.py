from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

from opencivicquery.models import UsageLimits

CATALOG_KEY = "catalog"
"""Bucket for searches that span every portal through the shared catalog."""


class UsageLimitExceeded(RuntimeError):
    """A usage cap refused the call before it reached any data source."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class UsageLimiter:
    """In-process caps on calls that reach a data source.

    Each server process counts on its own, so N instances admit up to N times the
    configured numbers. That is acceptable because the platform's instance ceiling is the
    outer bound on cost. A refused call is not counted, so a burst of refusals cannot
    extend its own lockout.
    """

    def __init__(
        self,
        limits: UsageLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = lambda: datetime.now(UTC).date().isoformat(),
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._today = today
        self._day = today()
        self._daily_count = 0
        self._client_counts: dict[str, int] = {}
        self._recent: dict[str, deque[float]] = {}

    def acquire(self, portal_key: str, client_key: str | None = None) -> None:
        day = self._today()
        if day != self._day:
            self._day = day
            self._daily_count = 0
            self._client_counts = {}
        if self._daily_count >= self._limits.daily_requests:
            raise UsageLimitExceeded(
                "This server reached its daily request limit. Try again after midnight UTC.",
                code="DAILY_LIMIT_REACHED",
            )
        per_client = self._limits.per_client_daily
        if (
            per_client is not None
            and client_key is not None
            and self._client_counts.get(client_key, 0) >= per_client
        ):
            raise UsageLimitExceeded(
                "You reached this server's daily limit for one user. Try again after "
                "midnight UTC, or run your own copy.",
                code="CLIENT_DAILY_LIMIT_REACHED",
            )

        now = self._clock()
        window = self._recent.setdefault(portal_key, deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self._limits.per_portal_per_minute:
            raise UsageLimitExceeded(
                f"Too many requests to {portal_key} in the last minute. Wait and retry.",
                code="PORTAL_RATE_LIMITED",
            )

        window.append(now)
        self._daily_count += 1
        # Only counted calls create an entry, so the map holds at most daily_requests keys.
        if per_client is not None and client_key is not None:
            self._client_counts[client_key] = self._client_counts.get(client_key, 0) + 1
        # Drop portals idle for a full window so a caller cycling through many domains
        # cannot grow memory without bound.
        if len(self._recent) > 1024:
            stale = [key for key, times in self._recent.items() if now - times[-1] >= 60.0]
            for key in stale:
                del self._recent[key]

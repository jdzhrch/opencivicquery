from __future__ import annotations

import pytest

from opencivicquery.limits import CATALOG_KEY, UsageLimiter, UsageLimitExceeded
from opencivicquery.models import UsageLimits


class FakeTime:
    """A monotonic clock and a UTC calendar that tests move by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.day = "2026-10-27"

    def clock(self) -> float:
        return self.now

    def today(self) -> str:
        return self.day


def limiter(daily: int = 100, per_minute: int = 100) -> tuple[UsageLimiter, FakeTime]:
    time = FakeTime()
    return (
        UsageLimiter(
            UsageLimits(daily_requests=daily, per_portal_per_minute=per_minute),
            clock=time.clock,
            today=time.today,
        ),
        time,
    )


def test_daily_cap_refuses_the_next_call_across_portals() -> None:
    limits, _ = limiter(daily=3)

    limits.acquire("data.sf.gov")
    limits.acquire("data.cityofnewyork.us")
    limits.acquire(CATALOG_KEY)
    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.example.gov")

    assert caught.value.code == "DAILY_LIMIT_REACHED"
    assert "midnight UTC" in str(caught.value)


def test_per_portal_window_counts_each_portal_separately() -> None:
    limits, _ = limiter(per_minute=2)

    limits.acquire("data.sf.gov")
    limits.acquire("data.sf.gov")
    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.sf.gov")

    assert caught.value.code == "PORTAL_RATE_LIMITED"
    assert "data.sf.gov" in str(caught.value)
    # Another portal, and the shared catalog, still have room.
    limits.acquire("data.cityofnewyork.us")
    limits.acquire(CATALOG_KEY)


def test_per_portal_window_is_a_rolling_sixty_seconds() -> None:
    limits, time = limiter(per_minute=2)

    limits.acquire("data.sf.gov")  # t = 1000
    time.now += 30
    limits.acquire("data.sf.gov")  # t = 1030
    time.now += 29.999  # t = 1059.999, both calls are still inside the window
    with pytest.raises(UsageLimitExceeded):
        limits.acquire("data.sf.gov")

    time.now = 1_060.0  # the first call is now exactly 60 seconds old and drops out
    limits.acquire("data.sf.gov")
    with pytest.raises(UsageLimitExceeded):
        limits.acquire("data.sf.gov")

    time.now = 1_090.0  # the call at t = 1030 drops out
    limits.acquire("data.sf.gov")


def test_refused_calls_are_not_counted_against_the_window() -> None:
    limits, time = limiter(per_minute=1)

    limits.acquire("data.sf.gov")
    for _ in range(50):
        time.now += 1
        with pytest.raises(UsageLimitExceeded):
            limits.acquire("data.sf.gov")

    # A burst of refusals did not extend the lockout: the one admitted call ages out
    # sixty seconds after it was made, not sixty seconds after the last refusal.
    time.now = 1_060.0
    limits.acquire("data.sf.gov")


def test_refused_calls_are_not_counted_against_the_daily_cap() -> None:
    limits, _ = limiter(daily=3, per_minute=1)

    limits.acquire("data.sf.gov")
    for _ in range(10):
        with pytest.raises(UsageLimitExceeded) as caught:
            limits.acquire("data.sf.gov")
        assert caught.value.code == "PORTAL_RATE_LIMITED"

    # Ten rate-limited refusals left two of the three daily calls unused.
    limits.acquire("data.cityofnewyork.us")
    limits.acquire(CATALOG_KEY)
    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.example.gov")
    assert caught.value.code == "DAILY_LIMIT_REACHED"


def test_daily_refusals_do_not_consume_the_portal_window() -> None:
    limits, time = limiter(daily=1, per_minute=1)

    limits.acquire("data.sf.gov")
    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.cityofnewyork.us")
    assert caught.value.code == "DAILY_LIMIT_REACHED"

    time.day = "2026-10-28"
    # The refused call to this portal left no trace in its window.
    limits.acquire("data.cityofnewyork.us")


def test_daily_counter_resets_on_a_new_utc_day() -> None:
    limits, time = limiter(daily=2)

    limits.acquire("data.sf.gov")
    limits.acquire("data.cityofnewyork.us")
    with pytest.raises(UsageLimitExceeded):
        limits.acquire(CATALOG_KEY)

    time.day = "2026-10-28"
    time.now += 120
    limits.acquire(CATALOG_KEY)
    limits.acquire("data.sf.gov")
    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.example.gov")
    assert caught.value.code == "DAILY_LIMIT_REACHED"


def test_day_rollover_does_not_clear_the_per_portal_window() -> None:
    limits, time = limiter(daily=100, per_minute=1)

    limits.acquire("data.sf.gov")
    time.day = "2026-10-28"
    time.now += 1

    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.sf.gov")
    assert caught.value.code == "PORTAL_RATE_LIMITED"


def test_idle_portals_are_pruned_once_the_table_is_large() -> None:
    limits, time = limiter(daily=10_000, per_minute=5)

    for index in range(1_025):
        limits.acquire(f"portal-{index}.example.gov")
    assert len(limits._recent) == 1_025

    time.now += 60
    limits.acquire("data.sf.gov")

    # Every portal idle for a full window is gone. The fresh entry stays.
    assert set(limits._recent) == {"data.sf.gov"}


def test_active_portals_survive_pruning() -> None:
    limits, time = limiter(daily=10_000, per_minute=5)

    for index in range(1_024):
        limits.acquire(f"portal-{index}.example.gov")
    time.now += 59
    limits.acquire("portal-0.example.gov")  # still busy
    time.now += 1
    limits.acquire("data.sf.gov")  # table now exceeds 1,024 keys, pruning runs

    assert "portal-0.example.gov" in limits._recent
    assert "portal-1.example.gov" not in limits._recent
    assert "data.sf.gov" in limits._recent


def test_small_tables_are_not_pruned() -> None:
    limits, time = limiter(daily=10_000, per_minute=5)

    for index in range(10):
        limits.acquire(f"portal-{index}.example.gov")
    time.now += 600
    limits.acquire("data.sf.gov")

    assert len(limits._recent) == 11


def test_default_clock_and_calendar_work_without_injection() -> None:
    limits = UsageLimiter(UsageLimits(daily_requests=1, per_portal_per_minute=1))

    limits.acquire("data.sf.gov")
    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.sf.gov")
    assert caught.value.code == "DAILY_LIMIT_REACHED"


def client_limiter(per_client: int, daily: int = 100) -> tuple[UsageLimiter, FakeTime]:
    time = FakeTime()
    return (
        UsageLimiter(
            UsageLimits(
                daily_requests=daily,
                per_portal_per_minute=100,
                per_client_daily=per_client,
                client_address="peer",
            ),
            clock=time.clock,
            today=time.today,
        ),
        time,
    )


def test_per_client_cap_refuses_one_client_and_admits_another() -> None:
    limits, _ = client_limiter(per_client=2)
    limits.acquire("data.sf.gov", "203.0.113.7")
    limits.acquire("data.cityofnewyork.us", "203.0.113.7")

    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire(CATALOG_KEY, "203.0.113.7")
    assert caught.value.code == "CLIENT_DAILY_LIMIT_REACHED"
    limits.acquire("data.sf.gov", "203.0.113.8")


def test_refused_client_calls_do_not_spend_the_shared_daily_allowance() -> None:
    limits, _ = client_limiter(per_client=1, daily=3)
    limits.acquire("data.sf.gov", "203.0.113.7")
    for _ in range(5):
        with pytest.raises(UsageLimitExceeded):
            limits.acquire("data.sf.gov", "203.0.113.7")

    limits.acquire("data.sf.gov", "203.0.113.8")
    limits.acquire("data.sf.gov", "203.0.113.9")


def test_per_client_counts_reset_on_a_new_utc_day() -> None:
    limits, time = client_limiter(per_client=1)
    limits.acquire("data.sf.gov", "203.0.113.7")
    with pytest.raises(UsageLimitExceeded):
        limits.acquire("data.sf.gov", "203.0.113.7")

    time.day = "2026-10-28"
    limits.acquire("data.sf.gov", "203.0.113.7")


def test_a_call_without_a_client_key_skips_only_the_per_client_cap() -> None:
    limits, _ = client_limiter(per_client=1, daily=2)
    limits.acquire("data.sf.gov", None)
    limits.acquire("data.sf.gov", None)

    with pytest.raises(UsageLimitExceeded) as caught:
        limits.acquire("data.sf.gov", None)
    assert caught.value.code == "DAILY_LIMIT_REACHED"


def test_without_a_per_client_cap_clients_are_not_tracked() -> None:
    limits, _ = limiter(daily=10)
    for _ in range(5):
        limits.acquire("data.sf.gov", "203.0.113.7")
    assert limits._client_counts == {}


def test_per_client_cap_cannot_exceed_the_daily_cap() -> None:
    with pytest.raises(ValueError, match="per_client_daily"):
        UsageLimits(
            daily_requests=10,
            per_portal_per_minute=10,
            per_client_daily=11,
            client_address="peer",
        )

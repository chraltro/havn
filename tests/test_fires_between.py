"""``schedule_time.fires_between`` with a limit: same answer, bounded work."""

from __future__ import annotations

import datetime as dt

import pytest

from havn.engine import schedule_time
from havn.engine.schedule_time import fires_between, load_zone

UTC = dt.timezone.utc


def _brute(expr, start, end, tz):
    out, cursor = [], start
    while True:
        nxt = schedule_time.next_fire(expr, cursor, tz)
        if nxt is None or nxt > end:
            return out
        out.append(nxt)
        cursor = nxt


@pytest.mark.parametrize("expr", ["* * * * *", "*/5 * * * *", "0 6 * * *", "30 2 * * *", "0 0 1 1 *",
                                  "0 9 * * MON"])
@pytest.mark.parametrize("days", [0.01, 1, 9])
@pytest.mark.parametrize("zone", [None, "Europe/Oslo"])
def test_limited_result_matches_a_full_walk(expr, days, zone):
    tz = load_zone(zone)
    start = dt.datetime(2026, 3, 1, 0, 0, 30, tzinfo=UTC)
    end = start + dt.timedelta(days=days)
    full = _brute(expr, start, end, tz)
    for limit in (0, 1, 7, 100):
        fires, total = fires_between(expr, start, end, tz, limit=limit, count_cap=10**9)
        assert fires == (full[-limit:] if limit else [])
        assert total == len(full)
    assert fires_between(expr, start, end, tz) == (full, len(full))


@pytest.mark.parametrize("expr", ["0 0 1 1 *", "0 6 1 * *", "0 9 * * MON"])
def test_sparse_schedules_over_years(expr):
    start = dt.datetime(2024, 2, 1, 12, 0, tzinfo=UTC)
    end = dt.datetime(2027, 1, 1, 12, 0, tzinfo=UTC)
    full = _brute(expr, start, end, None)
    for limit in (1, 5, 100):
        assert fires_between(expr, start, end, None, limit=limit, count_cap=10**9) == (full[-limit:], len(full))


def test_a_year_of_downtime_is_cheap(monkeypatch):
    calls = [0]
    real = schedule_time.next_fire

    def counting(*a, **k):
        calls[0] += 1
        return real(*a, **k)

    monkeypatch.setattr(schedule_time, "next_fire", counting)
    start = dt.datetime(2025, 4, 6, 0, 0, tzinfo=UTC)
    end = dt.datetime(2026, 4, 6, 0, 0, tzinfo=UTC)
    fires, total = fires_between("* * * * *", start, end, None, limit=100)
    assert len(fires) == 100
    assert fires[-1] == end
    # A lower bound past the cap (the true count is 525,600).
    assert 100 + schedule_time.FIRES_COUNT_CAP <= total < 525_600
    # The old walk visited all 525,600 minutes.
    assert calls[0] < 2 * schedule_time.FIRES_COUNT_CAP

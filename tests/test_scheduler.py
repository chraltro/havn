"""Tests for the scheduler."""

from pathlib import Path

from havn.engine.scheduler import SchedulerThread, get_scheduled_streams


def test_get_scheduled_streams_empty(tmp_path):
    (tmp_path / "project.yml").write_text("name: test\nstreams: {}\n")
    result = get_scheduled_streams(tmp_path)
    assert result == []


def test_get_scheduled_streams(tmp_path):
    (tmp_path / "project.yml").write_text("""
name: test
streams:
  daily:
    description: "Daily refresh"
    steps:
      - ingest: [all]
      - transform: [all]
    schedule: "0 6 * * *"
  manual:
    description: "On demand"
    steps:
      - transform: [all]
    schedule: null
""")
    result = get_scheduled_streams(tmp_path)
    assert len(result) == 1
    assert result[0]["name"] == "daily"
    assert result[0]["schedule"] == "0 6 * * *"


def test_cron_matching():
    """Test that the scheduler's cron matching works correctly."""
    import datetime

    scheduler = SchedulerThread(Path("/tmp"))

    # Mock a known time
    # _should_run checks current time, so we test the basic contract
    # that it doesn't crash and returns a bool
    result = scheduler._should_run("test", "0 6 * * *")
    assert isinstance(result, bool)

    # Invalid cron should not match
    result = scheduler._should_run("test", "bad")
    assert result is False


# --- Shared cron parser (engine/cron.py) ---

import datetime as _dt

import pytest

from havn.engine.cron import CronError, is_valid_cron, next_cron_fire, parse_cron


def _days(expr: str) -> list[int]:
    return sorted(parse_cron(expr).days)


def test_cron_step_anchors_at_field_start():
    # */2 in day-of-month is 1,3,5,... (was 2,4,6 when anchored at 0)
    assert _days("0 0 */2 * *")[:4] == [1, 3, 5, 7]
    assert sorted(parse_cron("0 0 1 */3 *").months) == [1, 4, 7, 10]
    assert sorted(parse_cron("*/20 * * * *").minutes) == [0, 20, 40]


def test_cron_start_slash_step():
    assert sorted(parse_cron("5/15 * * * *").minutes) == [5, 20, 35, 50]


def test_cron_names_and_sunday_seven():
    s = parse_cron("0 9 * JAN-MAR MON-FRI")
    assert s.months == {1, 2, 3}
    assert s.weekdays == {1, 2, 3, 4, 5}
    assert parse_cron("0 9 * * 7").weekdays == {0}
    assert parse_cron("0 9 * * sun").weekdays == {0}
    # 2026-04-05 is a Sunday
    assert parse_cron("0 9 * * 7").matches(_dt.datetime(2026, 4, 5, 9, 0))


def test_cron_dom_and_dow_restricted_is_or():
    # "1st of the month OR any Monday" (Vixie cron)
    s = parse_cron("0 0 1 * 1")
    assert s.matches(_dt.datetime(2026, 4, 1, 0, 0))   # Wednesday the 1st
    assert s.matches(_dt.datetime(2026, 4, 6, 0, 0))   # Monday the 6th
    assert not s.matches(_dt.datetime(2026, 4, 7, 0, 0))
    # One side starting with '*' keeps AND semantics
    assert not parse_cron("0 0 * * 1").matches(_dt.datetime(2026, 4, 1, 0, 0))


@pytest.mark.parametrize("expr", [
    "", "bad", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
    "* * 0 * *", "* * 32 * *", "* * * 13 *", "* * * * 8", "*/0 * * * *",
    "*/-1 * * * *", "5-1 * * * *", "a b c d e", "1,,2 * * * *", "* * * FOO *",
])
def test_cron_rejects_bad_fields(expr):
    assert not is_valid_cron(expr)
    with pytest.raises(CronError):
        parse_cron(expr)


def test_cron_aliases():
    assert parse_cron("@daily") == parse_cron("0 0 * * *")
    assert parse_cron("@weekly") == parse_cron("0 0 * * 0")


def test_next_cron_fire_weekly_monthly_yearly():
    base = _dt.datetime(2026, 4, 1, 12, 30)  # Wednesday
    assert next_cron_fire("0 6 * * 1", base) == _dt.datetime(2026, 4, 6, 6, 0)
    assert next_cron_fire("0 0 1 * *", base) == _dt.datetime(2026, 5, 1, 0, 0)
    assert next_cron_fire("0 0 1 1 *", base) == _dt.datetime(2027, 1, 1, 0, 0)
    # Feb 29 is reachable (next leap year)
    assert next_cron_fire("0 0 29 2 *", base) == _dt.datetime(2028, 2, 29, 0, 0)
    # Same day, later minute
    assert next_cron_fire("45 12 * * *", base) == _dt.datetime(2026, 4, 1, 12, 45)
    # Strictly after
    assert next_cron_fire("30 12 * * *", base) == _dt.datetime(2026, 4, 2, 12, 30)
    # Never fires (Feb 30)
    assert next_cron_fire("0 0 30 2 *", base) is None


def test_should_run_uses_shared_parser(tmp_path):
    scheduler = SchedulerThread(tmp_path)
    monday_9 = _dt.datetime(2026, 4, 6, 9, 0)
    assert scheduler._should_run("a", "0 9 * * MON", now=monday_9)
    assert scheduler._should_run("b", "0 9 * * 1-5", now=monday_9)
    assert not scheduler._should_run("c", "0 9 */2 * *", now=monday_9)  # 6th is even
    assert scheduler._should_run("d", "0 9 */5 * *", now=monday_9)      # 1,6,11,...
    assert not scheduler._should_run("e", "99 9 * * *", now=monday_9)


def test_cron_rejects_non_ascii_digits():
    # str.isdigit() accepts "²"; it must be a CronError, not a ValueError
    assert not is_valid_cron("\u00b2 * * * *")
    assert not is_valid_cron("*/\u00b2 * * * *")


def test_next_cron_fire_feb_29_across_2100():
    # 2100 is not a leap year, so the next Feb 29 after 2097 is in 2104
    assert next_cron_fire("0 0 29 2 *", _dt.datetime(2097, 3, 1)) == _dt.datetime(2104, 2, 29)

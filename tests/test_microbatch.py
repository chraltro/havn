"""Microbatch incremental models: `incremental_strategy=microbatch`."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from havn.engine.database import ensure_meta_table
from havn.engine.transform import (
    BatchRange,
    MicrobatchError,
    SQLModel,
    _execute_microbatch,
    compute_batch_windows,
    discover_models,
    parse_event_time,
    run_transform,
    shift_batch,
    substitute_batch_window,
    truncate_to_batch,
    validate_models,
)


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------


def test_truncate_to_each_batch_size():
    ts = datetime(2024, 3, 17, 14, 38, 51, 12345)
    assert truncate_to_batch(ts, "hour") == datetime(2024, 3, 17, 14)
    assert truncate_to_batch(ts, "day") == datetime(2024, 3, 17)
    assert truncate_to_batch(ts, "month") == datetime(2024, 3, 1)
    assert truncate_to_batch(ts, "year") == datetime(2024, 1, 1)


def test_unknown_batch_size_is_refused():
    with pytest.raises(MicrobatchError, match="Unknown batch_size"):
        truncate_to_batch(datetime(2024, 1, 1), "week")


def test_hourly_windows_start_at_the_containing_hour():
    windows = compute_batch_windows(
        datetime(2024, 1, 1, 5, 30), datetime(2024, 1, 1, 8), "hour"
    )
    assert windows == [
        (datetime(2024, 1, 1, 5), datetime(2024, 1, 1, 6)),
        (datetime(2024, 1, 1, 6), datetime(2024, 1, 1, 7)),
        (datetime(2024, 1, 1, 7), datetime(2024, 1, 1, 8)),
    ]


def test_daily_windows_cover_a_leap_day():
    windows = compute_batch_windows(
        datetime(2024, 2, 28), datetime(2024, 3, 2), "day"
    )
    assert [w[0] for w in windows] == [
        datetime(2024, 2, 28),
        datetime(2024, 2, 29),
        datetime(2024, 3, 1),
    ]


def test_monthly_windows_land_on_calendar_months():
    windows = compute_batch_windows(
        datetime(2024, 1, 15), datetime(2024, 4, 2), "month"
    )
    assert windows == [
        (datetime(2024, 1, 1), datetime(2024, 2, 1)),
        (datetime(2024, 2, 1), datetime(2024, 3, 1)),
        (datetime(2024, 3, 1), datetime(2024, 4, 1)),
        (datetime(2024, 4, 1), datetime(2024, 5, 1)),
    ]


def test_monthly_windows_cross_a_year_boundary():
    windows = compute_batch_windows(
        datetime(2024, 11, 5), datetime(2025, 2, 1), "month"
    )
    assert [w[0] for w in windows] == [
        datetime(2024, 11, 1),
        datetime(2024, 12, 1),
        datetime(2025, 1, 1),
    ]
    assert windows[-1][1] == datetime(2025, 2, 1)


def test_yearly_windows():
    windows = compute_batch_windows(
        datetime(2022, 6, 1), datetime(2024, 1, 1), "year"
    )
    assert windows == [
        (datetime(2022, 1, 1), datetime(2023, 1, 1)),
        (datetime(2023, 1, 1), datetime(2024, 1, 1)),
    ]


def test_an_end_before_begin_yields_no_windows():
    assert compute_batch_windows(
        datetime(2024, 5, 1), datetime(2024, 4, 1), "day"
    ) == []


def test_shift_batch_goes_backwards_across_a_year():
    assert shift_batch(datetime(2025, 2, 1), "month", -3) == datetime(2024, 11, 1)
    assert shift_batch(datetime(2024, 1, 1), "year", -1) == datetime(2023, 1, 1)


def test_an_absurd_window_count_is_refused():
    with pytest.raises(MicrobatchError, match="100,000 batch windows"):
        compute_batch_windows(datetime(1900, 1, 1), datetime(2024, 1, 1), "hour")


def test_parse_event_time_accepts_dates_and_timestamps():
    assert parse_event_time("2024-01-01") == datetime(2024, 1, 1)
    assert parse_event_time("2024-01-01 06:30:00") == datetime(2024, 1, 1, 6, 30)
    assert parse_event_time("2024-01-01T06:30:00Z") == datetime(2024, 1, 1, 6, 30)
    with pytest.raises(MicrobatchError, match="Could not read"):
        parse_event_time("last tuesday")


def test_placeholders_become_typed_literals():
    out = substitute_batch_window(
        "SELECT * FROM t WHERE at >= {start} AND at < {end}",
        datetime(2024, 1, 1),
        datetime(2024, 1, 2),
    )
    assert out == (
        "SELECT * FROM t WHERE at >= TIMESTAMP '2024-01-01 00:00:00' "
        "AND at < TIMESTAMP '2024-01-02 00:00:00'"
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


QUERY = (
    "SELECT id, event_at FROM landing.events "
    "WHERE event_at >= {start} AND event_at < {end}"
)


@pytest.fixture()
def events() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    ensure_meta_table(conn)
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE SCHEMA gold")
    conn.execute("CREATE TABLE landing.events (id INTEGER, event_at TIMESTAMP)")
    for i in range(10):
        conn.execute(
            "INSERT INTO landing.events VALUES (?, ?)",
            [i, datetime(2024, 1, 1) + timedelta(days=i)],
        )
    yield conn
    conn.close()


def make_model(query: str = QUERY, **kwargs) -> SQLModel:
    kwargs.setdefault("event_time", "event_at")
    kwargs.setdefault("batch_size", "day")
    kwargs.setdefault("begin", "2024-01-01")
    return SQLModel(
        path=Path("transform/gold/events.sql"),
        name="events",
        schema="gold",
        full_name="gold.events",
        sql="",
        query=query,
        materialized="incremental",
        incremental_strategy="microbatch",
        **kwargs,
    )


def batch_state(conn: duckdb.DuckDBPyConnection) -> list[tuple]:
    return conn.execute(
        'SELECT window_start, status, "rows" FROM _havn.batch_state '
        "ORDER BY window_start"
    ).fetchall()


def test_explicit_range_backfill(events):
    model = make_model()

    _, row_count = _execute_microbatch(
        events, model,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 6)),
    )

    assert row_count == 5
    assert [s for _, s, _ in batch_state(events)] == ["done"] * 5
    assert [r for _, _, r in batch_state(events)] == [1] * 5


def test_replaying_a_range_does_not_duplicate_rows(events):
    model = make_model()
    window = BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 6))
    _execute_microbatch(events, model, batch_range=window)

    _, row_count = _execute_microbatch(events, model, batch_range=window)

    assert row_count == 5


def test_a_window_reflects_rows_added_later(events):
    """Re-running a window replaces it, which is how a late arrival lands."""
    model = make_model()
    window = BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 2))
    _execute_microbatch(events, model, batch_range=window)
    events.execute(
        "INSERT INTO landing.events VALUES (99, TIMESTAMP '2024-01-01 18:00:00')"
    )

    _, row_count = _execute_microbatch(events, model, batch_range=window)

    assert row_count == 2


def test_open_ended_start_falls_back_to_begin(events):
    model = make_model()

    _, row_count = _execute_microbatch(
        events, model, batch_range=BatchRange(end=datetime(2024, 1, 4))
    )

    assert row_count == 3


def test_initial_run_starts_at_begin(events):
    """With no recorded state the first run covers begin through now."""
    begin = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")
    events.execute(
        "INSERT INTO landing.events VALUES (100, ?)",
        [datetime.utcnow() - timedelta(days=1)],
    )
    model = make_model(begin=begin)

    _, row_count = _execute_microbatch(events, model)

    assert row_count == 1
    # begin, begin+1 and today: three windows, all recorded.
    assert len(batch_state(events)) == 3
    assert {s for _, s, _ in batch_state(events)} == {"done"}


def test_incremental_run_resumes_with_lookback(events):
    """The second run redoes `lookback` finished windows, then continues."""
    model = make_model(lookback=1)
    _execute_microbatch(
        events, model,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 4)),
    )
    assert len(batch_state(events)) == 3

    # Resuming with an end of 2024-01-06: the last done window is 01-03, so
    # with lookback=1 the run starts at 01-03 and reaches 01-05.
    _execute_microbatch(
        events, model, batch_range=BatchRange(datetime(2024, 1, 3), datetime(2024, 1, 6))
    )

    assert [w for w, _, _ in batch_state(events)] == [
        datetime(2024, 1, 1),
        datetime(2024, 1, 2),
        datetime(2024, 1, 3),
        datetime(2024, 1, 4),
        datetime(2024, 1, 5),
    ]
    assert events.execute("SELECT count(*) FROM gold.events").fetchone() == (5,)


def test_resume_start_honours_lookback_and_begin(events):
    from havn.engine.transform.execution import _batch_resume_start

    model = make_model(lookback=2)
    _execute_microbatch(
        events, model,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 6)),
    )

    resume = _batch_resume_start(
        events, model, datetime(2024, 1, 1), "day", 2
    )
    # Last done window is 01-05, next is 01-06, minus two windows is 01-04.
    assert resume == datetime(2024, 1, 4)

    # A lookback that would reach before begin is clamped to begin.
    assert _batch_resume_start(
        events, model, datetime(2024, 1, 1), "day", 30
    ) == datetime(2024, 1, 1)


def test_resume_returns_to_the_first_window_that_is_not_done(events):
    from havn.engine.transform.execution import _batch_resume_start

    model = make_model()
    _execute_microbatch(
        events, model,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 6)),
    )
    events.execute(
        "UPDATE _havn.batch_state SET status = 'failed' WHERE window_start = ?",
        [datetime(2024, 1, 3)],
    )

    resume = _batch_resume_start(events, model, datetime(2024, 1, 1), "day", 0)

    assert resume == datetime(2024, 1, 3)


def test_a_failing_window_leaves_earlier_windows_committed(events):
    """Window 3 of 5 fails; the first two stay, and state names the failure."""
    model = make_model(
        "SELECT id, event_at, "
        "CASE WHEN {start} = TIMESTAMP '2024-01-03 00:00:00' "
        "THEN error('window 3 is broken') ELSE 1 END AS guard "
        "FROM landing.events WHERE event_at >= {start} AND event_at < {end}"
    )

    with pytest.raises(MicrobatchError, match="window 3 of 5"):
        _execute_microbatch(
            events, model,
            batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 6)),
        )

    assert events.execute("SELECT count(*) FROM gold.events").fetchone() == (2,)
    state = batch_state(events)
    assert [(w, s) for w, s, _ in state] == [
        (datetime(2024, 1, 1), "done"),
        (datetime(2024, 1, 2), "done"),
        (datetime(2024, 1, 3), "failed"),
    ]


def test_rerunning_after_a_failure_finishes_the_backfill(events):
    """The classic 'fix the data, run it again' loop."""
    bad = make_model(
        "SELECT id, event_at, "
        "CASE WHEN {start} = TIMESTAMP '2024-01-03 00:00:00' "
        "THEN error('window 3 is broken') ELSE 1 END AS guard "
        "FROM landing.events WHERE event_at >= {start} AND event_at < {end}"
    )
    with pytest.raises(MicrobatchError):
        _execute_microbatch(
            events, bad,
            batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 6)),
        )

    fixed = make_model(
        "SELECT id, event_at, 1 AS guard FROM landing.events "
        "WHERE event_at >= {start} AND event_at < {end}"
    )
    _execute_microbatch(events, fixed)

    done = [w for w, s, _ in batch_state(events) if s == "done"]
    assert datetime(2024, 1, 3) in done
    assert not [s for _, s, _ in batch_state(events) if s == "failed"]


def test_new_column_is_added_mid_backfill(events):
    model = make_model()
    _execute_microbatch(
        events, model,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 3)),
    )

    wider = make_model(
        "SELECT id, event_at, 'eu' AS region FROM landing.events "
        "WHERE event_at >= {start} AND event_at < {end}"
    )
    _execute_microbatch(
        events, wider,
        batch_range=BatchRange(datetime(2024, 1, 3), datetime(2024, 1, 5)),
    )

    assert events.execute(
        "SELECT region FROM gold.events ORDER BY event_at"
    ).fetchall() == [(None,), (None,), ("eu",), ("eu",)]


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"event_time": None}, "event_time"),
        ({"batch_size": None}, "batch_size"),
        ({"batch_size": "week"}, "Unknown batch_size"),
        ({"begin": None}, "begin"),
        ({"begin": "whenever"}, "Could not read"),
        ({"lookback": -1}, "lookback must be zero or more"),
    ],
)
def test_bad_microbatch_config_is_refused_before_any_window(events, kwargs, match):
    model = make_model(**kwargs)

    with pytest.raises(MicrobatchError, match=match):
        _execute_microbatch(events, model)

    assert events.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = 'gold' AND table_name = 'events'"
    ).fetchone() == (0,)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def write_project(tmp_path: Path, files: dict[str, str]) -> Path:
    transform = tmp_path / "transform"
    for rel, body in files.items():
        target = transform / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return transform


def messages_for(tmp_path: Path, body: str, severity: str = "error") -> list[str]:
    transform = write_project(tmp_path, {"gold/events.sql": body})
    models = discover_models(transform)
    return [
        e.message for e in validate_models(None, models) if e.severity == severity
    ]


MB_HEADER = (
    "@config materialized=incremental, incremental_strategy=microbatch, "
)


def test_validate_requires_event_time_batch_size_and_begin(tmp_path):
    messages = messages_for(
        tmp_path,
        "@config materialized=incremental, incremental_strategy=microbatch\n"
        "SELECT 1 AS id\n",
    )
    assert any("event_time=" in m for m in messages)
    assert any("batch_size=" in m for m in messages)
    assert any("begin=" in m for m in messages)


def test_validate_rejects_an_unknown_batch_size(tmp_path):
    messages = messages_for(
        tmp_path,
        MB_HEADER + "event_time=at, batch_size=week, begin=2024-01-01\n"
        "SELECT 1 AS id, {start} AS s, {end} AS e\n",
    )
    assert any("Unknown batch_size" in m for m in messages)


def test_validate_rejects_an_unreadable_begin(tmp_path):
    messages = messages_for(
        tmp_path,
        MB_HEADER + "event_time=at, batch_size=day, begin=soon\n"
        "SELECT 1 AS id, {start} AS s, {end} AS e\n",
    )
    assert any("Could not read" in m for m in messages)


def test_validate_rejects_incremental_filter_and_watermark(tmp_path):
    messages = messages_for(
        tmp_path,
        MB_HEADER + "event_time=at, batch_size=day, begin=2024-01-01, "
        "incremental_filter=WHERE 1=1, watermark=at\n"
        "SELECT 1 AS id, {start} AS s, {end} AS e\n",
    )
    assert any("incremental_filter= cannot be combined" in m for m in messages)
    assert any("watermark= cannot be combined" in m for m in messages)


def test_validate_rejects_a_non_numeric_lookback(tmp_path):
    messages = messages_for(
        tmp_path,
        MB_HEADER + "event_time=at, batch_size=day, begin=2024-01-01, lookback=two\n"
        "SELECT 1 AS id, {start} AS s, {end} AS e\n",
    )
    assert any("lookback must be a whole number" in m for m in messages)


def test_validate_warns_when_the_model_never_uses_the_window(tmp_path):
    warnings = messages_for(
        tmp_path,
        MB_HEADER + "event_time=at, batch_size=day, begin=2024-01-01\n"
        "SELECT 1 AS id\n",
        severity="warning",
    )
    assert any("{start}" in m for m in warnings)


def test_validate_warns_about_microbatch_keys_on_other_models(tmp_path):
    warnings = messages_for(
        tmp_path,
        "@config materialized=table, batch_size=day\nSELECT 1 AS id\n",
        severity="warning",
    )
    assert any("batch_size= only applies" in m for m in warnings)


def test_microbatch_config_is_folded_into_the_content_hash():
    base = make_model()
    assert make_model().content_hash == base.content_hash
    assert make_model(batch_size="hour").content_hash != base.content_hash
    assert make_model(begin="2024-02-01").content_hash != base.content_hash
    assert make_model(lookback=3).content_hash != base.content_hash


# ---------------------------------------------------------------------------
# End to end: run_transform, --force and the bind pass
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, begin: str) -> tuple[Path, duckdb.DuckDBPyConnection]:
    transform = write_project(
        tmp_path,
        {
            "gold/events.sql": (
                "@config materialized=incremental, "
                "incremental_strategy=microbatch, event_time=event_at, "
                f"batch_size=day, begin={begin}\n\n"
                "SELECT id, event_at FROM landing.events\n"
                "WHERE event_at >= {start} AND event_at < {end}\n"
            )
        },
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.events (id INTEGER, event_at TIMESTAMP)")
    return transform, conn


def test_run_transform_backfills_an_explicit_range(tmp_path):
    transform, conn = _project(tmp_path, "2024-01-01")
    for i in range(4):
        conn.execute(
            "INSERT INTO landing.events VALUES (?, ?)",
            [i, datetime(2024, 1, 1) + timedelta(days=i)],
        )

    results = run_transform(
        conn, transform, project_dir=tmp_path,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 3)),
    )

    assert results["gold.events"] == "built"
    assert conn.execute("SELECT count(*) FROM gold.events").fetchone() == (2,)
    conn.close()


def test_force_reprocesses_from_begin(tmp_path):
    begin = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    transform, conn = _project(tmp_path, begin)
    conn.execute(
        "INSERT INTO landing.events VALUES (1, ?)", [datetime.utcnow()]
    )
    run_transform(conn, transform, project_dir=tmp_path)
    assert conn.execute("SELECT count(*) FROM gold.events").fetchone() == (1,)

    # A row lands in a window the first run already finished. An ordinary
    # rerun with lookback=1 would catch it too; --force is the blunt version
    # and must redo every window from begin.
    conn.execute(
        "INSERT INTO landing.events VALUES (2, ?)",
        [datetime.utcnow() - timedelta(days=1)],
    )
    run_transform(conn, transform, force=True, project_dir=tmp_path)

    assert conn.execute("SELECT count(*) FROM gold.events").fetchone() == (2,)
    windows = conn.execute(
        "SELECT count(*) FROM _havn.batch_state WHERE model_path = 'gold.events'"
    ).fetchone()[0]
    assert windows >= 2
    conn.close()


def test_the_bind_pass_binds_a_microbatch_model(tmp_path):
    from havn.engine.transform.bind import bind_models

    transform, conn = _project(tmp_path, "2024-01-01")

    result = bind_models(conn, discover_models(transform), project_dir=tmp_path)

    assert result.ok, result.errors
    assert dict(result.schemas["gold.events"])["event_at"] == "TIMESTAMP"
    conn.close()


def test_the_bind_pass_survives_an_unreadable_begin(tmp_path):
    from havn.engine.transform.bind import bind_models

    transform = write_project(
        tmp_path,
        {
            "gold/events.sql": (
                "@config materialized=incremental, "
                "incremental_strategy=microbatch, event_time=event_at, "
                "batch_size=day, begin=nonsense\n"
                "SELECT id FROM landing.events WHERE event_at >= {start}\n"
            )
        },
    )
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.events (id INTEGER, event_at TIMESTAMP)")

    result = bind_models(conn, discover_models(transform), project_dir=tmp_path)

    assert result.ok, result.errors
    conn.close()


# ---------------------------------------------------------------------------
# Ephemeral upstreams: the window placeholders must survive inlining
# ---------------------------------------------------------------------------


def test_ephemeral_upstream_keeps_the_window_placeholders(tmp_path):
    """Inlining round-trips the SQL through sqlglot, which eats ``{start}``.

    Without masking, ``{start}`` comes back out as ``{'start': start}`` and
    every window fails to bind, so a microbatch model with an ephemeral
    upstream could never build.
    """
    transform = write_project(
        tmp_path,
        {
            "bronze/clean_events.sql": (
                "@config materialized=ephemeral, schema=bronze\n\n"
                "SELECT id, event_at FROM landing.events WHERE id >= 0\n"
            ),
            "gold/events.sql": (
                "@config materialized=incremental, "
                "incremental_strategy=microbatch, event_time=event_at, "
                "batch_size=day, begin=2024-01-01\n\n"
                "SELECT id, event_at FROM bronze.clean_events\n"
                "WHERE event_at >= {start} AND event_at < {end}\n"
            ),
        },
    )
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute("CREATE TABLE landing.events (id INTEGER, event_at TIMESTAMP)")
    for i in range(4):
        conn.execute(
            "INSERT INTO landing.events VALUES (?, ?)",
            [i, datetime(2024, 1, 1) + timedelta(days=i)],
        )

    results = run_transform(
        conn, transform, project_dir=tmp_path,
        batch_range=BatchRange(datetime(2024, 1, 1), datetime(2024, 1, 3)),
    )

    assert results["gold.events"] == "built"
    assert conn.execute("SELECT count(*) FROM gold.events").fetchone() == (2,)
    conn.close()


def test_inline_ephemeral_leaves_placeholders_alone():
    """The placeholders come back out of inlining spelled as they went in."""
    from havn.engine.transform.inline import inline_ephemeral

    eph = SQLModel(
        path=Path("transform/bronze/clean.sql"),
        name="clean",
        schema="bronze",
        full_name="bronze.clean",
        sql="",
        query="SELECT id, event_at FROM landing.events",
        materialized="ephemeral",
    )
    consumer = make_model(
        "SELECT id, event_at FROM bronze.clean "
        "WHERE event_at >= {start} AND event_at < {end}",
        depends_on=["bronze.clean"],
    )

    resolved = inline_ephemeral(
        consumer, {"bronze.clean": eph, "gold.events": consumer}
    )

    assert "{start}" in resolved
    assert "{end}" in resolved
    assert "'start'" not in resolved

"""Tests for the Python script runner."""

import json
import sys
from pathlib import Path

import duckdb

from havn.engine.database import ensure_meta_table
from havn.engine.runner import _extract_row_count, run_script, run_scripts_in_dir


def test_run_script_success(tmp_path):
    """A valid script with run(db) should execute successfully (backward compat)."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "test_ingest.py"
    script.write_text(
        'import duckdb\n\n'
        'def run(db):\n'
        '    db.execute("CREATE SCHEMA IF NOT EXISTS landing")\n'
        '    db.execute("CREATE TABLE landing.test AS SELECT 42 AS val")\n'
        '    print("done")\n'
    )

    result = run_script(conn, script, "ingest")
    assert result["status"] == "success"
    assert result["duration_ms"] >= 0
    assert "done" in result["log_output"]

    # Verify the table was created
    row = conn.execute("SELECT val FROM landing.test").fetchone()
    assert row[0] == 42
    conn.close()


def test_run_script_error(tmp_path):
    """A script that raises an exception should be captured."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "bad_script.py"
    script.write_text(
        'def run(db):\n'
        '    raise ValueError("something went wrong")\n'
    )

    result = run_script(conn, script, "ingest")
    assert result["status"] == "error"
    assert "something went wrong" in result["error"]
    conn.close()


def test_run_script_no_run_function(tmp_path):
    """A script without run() should succeed as top-level code."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "no_run.py"
    script.write_text('x = 1\n')

    result = run_script(conn, script, "ingest")
    assert result["status"] == "success"
    conn.close()


def test_run_script_top_level(tmp_path):
    """A top-level script using db should execute and create tables."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "top_level.py"
    script.write_text(
        'db.execute("CREATE SCHEMA IF NOT EXISTS landing")\n'
        'db.execute("CREATE TABLE landing.top AS SELECT 99 AS val")\n'
        'print("top-level done")\n'
    )

    result = run_script(conn, script, "ingest")
    assert result["status"] == "success"
    assert "top-level done" in result["log_output"]

    row = conn.execute("SELECT val FROM landing.top").fetchone()
    assert row[0] == 99
    conn.close()


def test_run_notebook_as_script(tmp_path):
    """A .dpnb notebook should run as a pipeline step."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    notebook = {
        "title": "Test Notebook",
        "cells": [
            {
                "id": "cell_1",
                "type": "code",
                "source": 'db.execute("CREATE SCHEMA IF NOT EXISTS landing")',
                "outputs": [],
            },
            {
                "id": "cell_2",
                "type": "code",
                "source": 'db.execute("CREATE TABLE landing.nb_test AS SELECT 77 AS val")',
                "outputs": [],
            },
        ],
    }

    nb_path = tmp_path / "ingest_nb.dpnb"
    nb_path.write_text(json.dumps(notebook))

    result = run_script(conn, nb_path, "ingest")
    assert result["status"] == "success"

    row = conn.execute("SELECT val FROM landing.nb_test").fetchone()
    assert row[0] == 77
    conn.close()


def test_run_scripts_in_dir_discovers_notebooks(tmp_path):
    """run_scripts_in_dir should discover both .py and .dpnb files."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    ingest_dir = tmp_path / "ingest"
    ingest_dir.mkdir()

    # A .py script
    (ingest_dir / "a_script.py").write_text(
        'db.execute("CREATE SCHEMA IF NOT EXISTS landing")\n'
    )

    # A .dpnb notebook
    notebook = {
        "title": "NB",
        "cells": [
            {
                "id": "c1",
                "type": "code",
                "source": 'db.execute("CREATE SCHEMA IF NOT EXISTS landing")',
                "outputs": [],
            },
        ],
    }
    (ingest_dir / "b_notebook.dpnb").write_text(json.dumps(notebook))

    # A skipped file
    (ingest_dir / "_skip.py").write_text('x = 1\n')

    results = run_scripts_in_dir(conn, ingest_dir, "ingest")
    assert len(results) == 2
    assert all(r["status"] == "success" for r in results)
    conn.close()


# ---------------------------------------------------------------------------
# _extract_row_count tests
# ---------------------------------------------------------------------------


def test_extract_row_count_loaded():
    assert _extract_row_count("Loaded 42 rows into landing.data") == 42


def test_extract_row_count_got_records():
    assert _extract_row_count("Got 15 records") == 15


def test_extract_row_count_does_not_match_bytes():
    """'Downloaded X bytes' should NOT be counted as rows."""
    output = "Downloaded 1048576 bytes\nLoaded 842 rows into landing.data"
    assert _extract_row_count(output) == 842


def test_extract_row_count_bytes_only():
    """If output only mentions bytes, row count should be 0."""
    assert _extract_row_count("Downloaded 999999 bytes") == 0


def test_extract_row_count_empty():
    assert _extract_row_count("") == 0


def test_extract_row_count_with_thousand_separators():
    """Row counts printed with comma thousand-separators must be preserved.

    Regression: a script that printed 'loaded 2,616,838 rows' was being
    summarised as '845 rows' because the regex stopped at the first comma.
    """
    assert _extract_row_count("loading landing.transactions ... 2,616,838 rows") == 2616838
    assert _extract_row_count("Loaded 1_234_567 rows") == 1234567
    # Mixed output: the largest match still wins
    output = (
        "  loading landing.customers ...\n    10,003 rows\n"
        "  loading landing.transactions ...\n    2,616,838 rows\n"
    )
    assert _extract_row_count(output) == 2616838


# ---------------------------------------------------------------------------
# Pragma parsing and schedule=once skip behavior
# ---------------------------------------------------------------------------


def test_parse_pragma_basic():
    from havn.engine.runner import _parse_pragma
    src = "# @havn: schedule=once\nimport os\n"
    assert _parse_pragma(src) == {"schedule": "once"}


def test_parse_pragma_multiple_keys():
    from havn.engine.runner import _parse_pragma
    src = "# @havn: schedule=once owner=data-team\nx = 1\n"
    out = _parse_pragma(src)
    assert out["schedule"] == "once"
    assert out["owner"] == "data-team"


def test_parse_pragma_skipped_after_first_code_line():
    from havn.engine.runner import _parse_pragma
    src = "import os\n# @havn: schedule=once\n"
    # Pragma must be at the top; comments after code don't count.
    assert _parse_pragma(src) == {}


def test_parse_pragma_after_module_docstring():
    from havn.engine.runner import _parse_pragma
    src = '"""Module docstring."""\n# @havn: schedule=once\nimport os\n'
    assert _parse_pragma(src) == {"schedule": "once"}


def test_parse_pragma_inside_multiline_docstring_ignored():
    from havn.engine.runner import _parse_pragma
    src = '"""Hello\n# @havn: schedule=once\n"""\nimport os\n'
    # The pragma sits inside the docstring, so it must not be parsed.
    assert _parse_pragma(src) == {}


def test_parse_pragma_no_directive():
    from havn.engine.runner import _parse_pragma
    assert _parse_pragma("import os\n") == {}


def test_run_script_schedule_once_skips_after_first_success(tmp_path):
    """Second run of a `schedule=once` script must be skipped, not re-executed."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "once_ingest.py"
    # Each run appends a row to a sentinel table so we can count executions.
    script.write_text(
        "# @havn: schedule=once\n"
        "db.execute('CREATE TABLE IF NOT EXISTS sentinel (n INT)')\n"
        "db.execute('INSERT INTO sentinel VALUES (1)')\n"
        "print('ran')\n"
    )

    # First run: executes normally.
    r1 = run_script(conn, script, "ingest")
    assert r1["status"] == "success"
    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 1

    # Second run: skipped, no new rows.
    r2 = run_script(conn, script, "ingest")
    assert r2["status"] == "skipped"
    assert "schedule=once" in r2["log_output"]
    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 1
    conn.close()


def test_run_script_schedule_once_force_overrides(tmp_path):
    """`force=True` must re-run a `schedule=once` script even with prior success."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "once_ingest.py"
    script.write_text(
        "# @havn: schedule=once\n"
        "db.execute('CREATE TABLE IF NOT EXISTS sentinel (n INT)')\n"
        "db.execute('INSERT INTO sentinel VALUES (1)')\n"
    )

    run_script(conn, script, "ingest")
    run_script(conn, script, "ingest", force=True)
    # Two executions = two rows in sentinel.
    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 2
    conn.close()


def test_run_script_no_pragma_runs_every_time(tmp_path):
    """Scripts without a schedule pragma keep the old behavior: run every time."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))

    script = tmp_path / "always_ingest.py"
    script.write_text(
        "db.execute('CREATE TABLE IF NOT EXISTS sentinel (n INT)')\n"
        "db.execute('INSERT INTO sentinel VALUES (1)')\n"
    )

    run_script(conn, script, "ingest")
    run_script(conn, script, "ingest")
    assert conn.execute("SELECT COUNT(*) FROM sentinel").fetchone()[0] == 2
    conn.close()


def test_run_script_long_running_does_not_corrupt_cursor(tmp_path, monkeypatch):
    """Regression: long-running scripts must not have their result cursor
    clobbered by the runner's idle-detection poll.

    Originally the runner polled `duckdb_queries()` on the *same* connection
    used by the script thread. DuckDB connections are not thread-safe; the
    concurrent probe corrupted the script's cursor state, so chained
    `db.execute(...).fetchone()` returned None and unpacking blew up with
    "cannot unpack non-iterable NoneType object". Real failure surfaced via
    the Nordvik case ingest on a ~20s Postgres CTAS.

    To make the test deterministic and fast, we shorten the poll interval so
    several polls fire during a ~1s script. With the bug present, at least one
    of the chained fetches returns None.
    """
    from havn.engine import runner as runner_mod

    monkeypatch.setattr(runner_mod, "SCRIPT_POLL_INTERVAL_SECONDS", 0.05)

    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA landing")

    # 30 iterations of (CTAS, COUNT, sleep 50ms) ≈ 1.5s wall time, with the
    # poll firing every 50ms. Many overlap windows; the bug reproduces ~always.
    script = tmp_path / "long_ingest.py"
    script.write_text(
        "import time\n"
        "for i in range(30):\n"
        "    db.execute(f'CREATE OR REPLACE TABLE landing.t AS "
        "SELECT range AS x FROM range(50000)')\n"
        "    row = db.execute('SELECT COUNT(*) FROM landing.t').fetchone()\n"
        "    assert row is not None, "
        "f'iter {i}: fetchone returned None — runner clobbered the cursor'\n"
        "    (n,) = row\n"
        "    assert n == 50000, f'iter {i}: got {n} rows, expected 50000'\n"
        "    time.sleep(0.05)\n"
        "print('all iterations OK', flush=True)\n"
    )

    result = run_script(conn, script, "ingest", timeout=60)
    assert result["status"] == "success", (
        f"script failed: {result.get('error')!r}\nlog:\n{result.get('log_output')}"
    )
    assert "all iterations OK" in result["log_output"]
    conn.close()


# --- Timeouts (idle, hard, notebooks, orphans) ---

import threading
import time

import pytest

from havn.engine import runner as runner_mod


@pytest.fixture
def fast_timeouts(monkeypatch):
    """Shrink the poll / idle / stop-grace windows so timeout tests run in ~1s."""
    monkeypatch.setattr(runner_mod, "SCRIPT_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(runner_mod, "SCRIPT_IDLE_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(runner_mod, "SCRIPT_STOP_GRACE_SECONDS", 2)


def _conn(tmp_path):
    conn = duckdb.connect(str(tmp_path / "t.duckdb"))
    ensure_meta_table(conn)
    return conn


def test_idle_timeout_fires_for_silent_script(tmp_path, fast_timeouts):
    """No query and no output means stuck. The old duckdb_queries() probe
    failed on DuckDB 1.5, counted as activity, and the idle timeout never
    fired."""
    conn = _conn(tmp_path)
    script = tmp_path / "silent.py"
    script.write_text("import time\nfor _ in range(1000):\n    time.sleep(0.01)\n")
    t0 = time.perf_counter()
    result = run_script(conn, script, "ingest", timeout=60, use_circuit_breaker=False)
    assert time.perf_counter() - t0 < 5
    assert result["status"] == "error"
    assert "appears stuck" in result["error"]
    assert result["orphaned"] is False  # the loop was stopped, not left running
    conn.close()


def test_output_counts_as_activity(tmp_path, fast_timeouts):
    conn = _conn(tmp_path)
    script = tmp_path / "chatty.py"
    script.write_text(
        "import time\nfor i in range(20):\n    print(i, flush=True)\n    time.sleep(0.05)\n"
    )
    result = run_script(conn, script, "ingest", timeout=60, use_circuit_breaker=False)
    assert result["status"] == "success", result["error"]
    conn.close()


def test_busy_probe_verdict():
    """Busy means at least 5% of a core over a full window (fake clocks)."""
    clock = {"cpu": 0.0, "wall": 0.0}
    probe = runner_mod._BusyProbe(lambda: clock["cpu"], lambda: clock["wall"])
    assert probe.active() is True          # no full window measured yet
    clock["wall"] = 1.0
    clock["cpu"] = 0.01                    # 1% of a core
    assert probe.active() is False
    clock["wall"] = 1.1                    # shorter than a window: last verdict
    clock["cpu"] = 1.0
    assert probe.active() is False
    clock["wall"] = 2.0                    # 99% of a core over the window
    assert probe.active() is True


def test_busy_probe_sees_running_query(tmp_path):
    """A DuckDB query in flight keeps the process busy; this is what keeps
    long CTAS scripts from idling out."""
    conn = _conn(tmp_path)
    probe = runner_mod._BusyProbe()

    def _long():
        try:
            conn.execute("SELECT count(*) FROM range(100000000000) t(a) WHERE a % 7 = 3").fetchall()
        except Exception:
            pass

    t = threading.Thread(target=_long)
    t.start()
    try:
        # Several full windows: a running query must read busy in one.
        deadline = time.perf_counter() + 5
        seen = False
        while time.perf_counter() < deadline and not seen:
            time.sleep(0.3)
            seen = probe.active()
        assert seen
    finally:
        conn.interrupt()
        t.join(5)
    conn.close()


def test_idle_timeout_fires_after_partial_fetch(tmp_path, fast_timeouts):
    """A partly fetched result keeps conn.query_progress() at 0 for as long
    as the script holds it; a parked result must not count as a query in
    flight."""
    conn = _conn(tmp_path)
    conn.execute("SET enable_progress_bar = true")  # makes query_progress() report 0
    script = tmp_path / "parked.py"
    script.write_text(
        "import time\n"
        "db.execute('SELECT * FROM range(1000000)').fetchone()\n"
        "for _ in range(1000):\n    time.sleep(0.01)\n"
    )
    t0 = time.perf_counter()
    result = run_script(conn, script, "ingest", timeout=60, use_circuit_breaker=False)
    assert time.perf_counter() - t0 < 5
    assert "appears stuck" in result["error"]
    conn.close()


def test_hard_timeout_message(tmp_path, fast_timeouts, monkeypatch):
    monkeypatch.setattr(runner_mod, "SCRIPT_IDLE_TIMEOUT_SECONDS", 60)
    conn = _conn(tmp_path)
    script = tmp_path / "slow.py"
    script.write_text("import time\nfor _ in range(1000):\n    time.sleep(0.01)\n")
    result = run_script(conn, script, "ingest", timeout=0.3, use_circuit_breaker=False)
    assert result["status"] == "error"
    assert "timed out after 0.3s" in result["error"]
    assert result["timeout_reason"] == "timeout"
    conn.close()


def test_orphaned_script_blocks_later_scripts(tmp_path, fast_timeouts, monkeypatch):
    """A script stuck in a blocking call cannot be killed; later scripts on
    the same connection must not start and race it."""
    monkeypatch.setattr(runner_mod, "SCRIPT_STOP_GRACE_SECONDS", 0.2)
    conn = _conn(tmp_path)
    (tmp_path / "export").mkdir()
    (tmp_path / "export" / "a_blocking.py").write_text("import time\ntime.sleep(1.5)\n")
    (tmp_path / "export" / "b_next.py").write_text("db.execute('CREATE TABLE ran AS SELECT 1')\n")

    results = run_scripts_in_dir(conn, tmp_path / "export", "export")
    assert len(results) == 1  # stopped after the orphan, even for export
    assert results[0]["orphaned"] is True
    assert "still running" in results[0]["error"]

    blocked = run_script(conn, tmp_path / "export" / "b_next.py", "export", use_circuit_breaker=False)
    assert blocked["status"] == "error"
    assert "a_blocking.py" in blocked["error"]

    # Once the orphan exits the connection is usable again.
    deadline = time.perf_counter() + 5
    while runner_mod.orphaned_script(conn) and time.perf_counter() < deadline:
        time.sleep(0.05)
    ok = run_script(conn, tmp_path / "export" / "b_next.py", "export", use_circuit_breaker=False)
    assert ok["status"] == "success", ok["error"]
    conn.close()


def test_notebook_step_has_a_timeout(tmp_path, fast_timeouts, monkeypatch):
    """.dpnb steps used to run synchronously with no timeout at all. A hung
    SQL cell is interrupted and the step fails at the hard timeout."""
    monkeypatch.setattr(runner_mod, "SCRIPT_IDLE_TIMEOUT_SECONDS", 60)
    conn = _conn(tmp_path)
    nb = {"title": "hang", "cells": [
        {"id": "c1", "type": "sql",
         "source": "SELECT count(*) FROM range(100000000000) t(a) WHERE a % 7 = 3"},
        {"id": "c2", "type": "sql", "source": "CREATE TABLE after_hang AS SELECT 1"},
    ]}
    path = tmp_path / "hang.dpnb"
    path.write_text(json.dumps(nb))
    t0 = time.perf_counter()
    result = run_script(conn, path, "ingest", timeout=0.5, use_circuit_breaker=False)
    assert time.perf_counter() - t0 < 5
    assert result["status"] == "error"
    assert "timed out after 0.5s" in result["error"]
    assert result["orphaned"] is False
    tables = {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "after_hang" not in tables
    conn.close()


def test_notebook_step_runs_no_cell_after_timeout(tmp_path, fast_timeouts, monkeypatch):
    """A code cell runs in the sandbox's own thread and cannot be killed;
    once the step has timed out, no further cell may start."""
    monkeypatch.setattr(runner_mod, "SCRIPT_IDLE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(runner_mod, "SCRIPT_STOP_GRACE_SECONDS", 0.1)
    conn = _conn(tmp_path)
    nb = {"title": "slow", "cells": [
        {"id": "c1", "type": "code", "source": "import time\nfor _ in range(80):\n    time.sleep(0.01)"},
        {"id": "c2", "type": "sql", "source": "CREATE TABLE after_timeout AS SELECT 1"},
    ]}
    path = tmp_path / "slow.dpnb"
    path.write_text(json.dumps(nb))
    result = run_script(conn, path, "ingest", timeout=60, use_circuit_breaker=False)
    assert result["status"] == "error"
    assert result["orphaned"] is True
    deadline = time.perf_counter() + 5
    while runner_mod.orphaned_script(conn) and time.perf_counter() < deadline:
        time.sleep(0.05)
    assert runner_mod.orphaned_script(conn) is None
    tables = {r[0] for r in conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "after_timeout" not in tables
    conn.close()


def test_notebook_step_stops_at_first_failing_cell(tmp_path):
    conn = _conn(tmp_path)
    nb = {"title": "fail", "cells": [
        {"id": "c1", "type": "sql", "source": "CREATE TABLE t (step INTEGER)"},
        {"id": "c2", "type": "code", "source": "raise RuntimeError('boom')"},
        {"id": "c3", "type": "sql", "source": "INSERT INTO t VALUES (3)"},
    ]}
    path = tmp_path / "fail.dpnb"
    path.write_text(json.dumps(nb))
    result = run_script(conn, path, "ingest", use_circuit_breaker=False)
    assert result["status"] == "error"
    assert "boom" in result["error"]
    assert result["skipped_cells"] == ["c3"]
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    conn.close()


def test_notebook_step_ignores_error_outputs_saved_in_file(tmp_path):
    """An error output saved from an earlier interactive run is not this
    run's failure."""
    conn = _conn(tmp_path)
    nb = {"title": "ok", "cells": [
        {"id": "c1", "type": "sql", "source": "SELECT 1",
         "outputs": [{"type": "error", "text": "old failure"}]},
        {"id": "c2", "type": "markdown", "source": "notes",
         "outputs": [{"type": "error", "text": "stale"}]},
    ]}
    path = tmp_path / "ok.dpnb"
    path.write_text(json.dumps(nb))
    result = run_script(conn, path, "ingest", use_circuit_breaker=False)
    assert result["status"] == "success", result["error"]
    conn.close()


def test_timeout_stop_is_not_swallowed_by_except_exception(tmp_path, fast_timeouts):
    """The stop is a BaseException: a retry loop's `except Exception:` must
    not keep a timed-out script alive."""
    conn = _conn(tmp_path)
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import time\n"
        "for _ in range(1000):\n"
        "    try:\n"
        "        time.sleep(0.01)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    result = run_script(conn, script, "ingest", timeout=60, use_circuit_breaker=False)
    assert "appears stuck" in result["error"]
    assert result["orphaned"] is False
    conn.close()


def test_idle_timeout_pragma_disables_idle_kill(tmp_path, fast_timeouts):
    """A script waiting on a slow remote source can opt out of the idle
    timeout; the hard timeout still applies."""
    conn = _conn(tmp_path)
    script = tmp_path / "remote.py"
    script.write_text(
        "# @havn: idle_timeout=0\n"
        "import time\n"
        "for _ in range(60):\n    time.sleep(0.01)\n"
    )
    result = run_script(conn, script, "ingest", timeout=60, use_circuit_breaker=False)
    assert result["status"] == "success", result["error"]
    conn.close()


def test_orphan_check_comes_before_any_use_of_the_connection(tmp_path, fast_timeouts, monkeypatch):
    """While an orphan holds the connection, a blocked run_script must not
    touch it at all (ensure_meta_table, schedule=once lookup, resource SETs)."""
    monkeypatch.setattr(runner_mod, "SCRIPT_STOP_GRACE_SECONDS", 0.2)
    conn = _conn(tmp_path)
    (tmp_path / "a.py").write_text("import time\ntime.sleep(1.5)\n")
    (tmp_path / "b.py").write_text("# @havn: schedule=once\nprint('b')\n")
    assert run_script(conn, tmp_path / "a.py", "export", use_circuit_breaker=False)["orphaned"]

    touched = []
    monkeypatch.setattr(runner_mod, "ensure_meta_table", lambda c: touched.append("meta"))
    monkeypatch.setattr(runner_mod, "_has_prior_success", lambda c, t: touched.append("once"))
    blocked = run_script(conn, tmp_path / "b.py", "export", use_circuit_breaker=False)
    assert blocked["status"] == "error" and blocked["orphaned"]
    assert touched == []
    while runner_mod.orphaned_script(conn):
        time.sleep(0.05)
    conn.close()


def test_orphan_does_not_hijack_process_output(tmp_path, fast_timeouts, monkeypatch, capsys):
    """Output is captured per script thread: while an orphan runs, other
    threads' prints still reach the real stdout, and the orphan's later
    output does not leak there."""
    monkeypatch.setattr(runner_mod, "SCRIPT_STOP_GRACE_SECONDS", 0.2)
    conn = _conn(tmp_path)
    (tmp_path / "a.py").write_text(
        "import time\nprint('from-script-early', flush=True)\ntime.sleep(1.2)\n"
        "print('from-script-late', flush=True)\n"
    )
    monkeypatch.setattr(runner_mod, "SCRIPT_IDLE_TIMEOUT_SECONDS", 0.3)
    result = run_script(conn, tmp_path / "a.py", "export", use_circuit_breaker=False)
    assert result["orphaned"]
    assert "from-script-early" in result["log_output"]
    print("from-main-thread")
    while runner_mod.orphaned_script(conn):
        time.sleep(0.05)
    out = capsys.readouterr().out
    assert "from-main-thread" in out
    assert "from-script-late" not in out
    assert not isinstance(sys.stdout, runner_mod._StreamRouter)  # removed when done
    conn.close()


def test_script_output_still_captured_per_thread(tmp_path):
    conn = _conn(tmp_path)
    (tmp_path / "p.py").write_text(
        "import sys\nprint('out-line')\nprint('err-line', file=sys.stderr)\nprint('Loaded 7 rows')\n"
    )
    result = run_script(conn, tmp_path / "p.py", "ingest", use_circuit_breaker=False)
    assert result["status"] == "success"
    assert "out-line" in result["log_output"] and "err-line" in result["log_output"]
    assert result["rows_affected"] == 7
    conn.close()


def test_notebook_idle_timeout_pragma_in_first_code_cell(tmp_path, fast_timeouts):
    conn = _conn(tmp_path)
    nb = {"title": "slow", "cells": [
        {"id": "m", "type": "markdown", "source": "notes"},
        {"id": "c", "type": "code",
         "source": "# @havn: idle_timeout=0\nimport time\nfor _ in range(60):\n    time.sleep(0.01)"},
    ]}
    path = tmp_path / "slow.dpnb"
    path.write_text(json.dumps(nb))
    result = run_script(conn, path, "ingest", timeout=60, use_circuit_breaker=False)
    assert result["status"] == "success", result["error"]
    conn.close()


def test_notebook_idle_message_gives_notebook_advice(tmp_path, fast_timeouts):
    conn = _conn(tmp_path)
    nb = {"title": "slow", "cells": [
        {"id": "c", "type": "sql",
         "source": "SELECT count(*) FROM range(10)"},
        {"id": "d", "type": "code", "source": "import time\nfor _ in range(80):\n    time.sleep(0.01)"},
    ]}
    path = tmp_path / "slow2.dpnb"
    path.write_text(json.dumps(nb))
    result = run_script(conn, path, "ingest", timeout=60, use_circuit_breaker=False)
    assert "first code cell" in result["error"]
    while runner_mod.orphaned_script(conn):
        time.sleep(0.05)
    conn.close()

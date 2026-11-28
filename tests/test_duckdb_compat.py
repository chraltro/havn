"""Helpers that keep havn working on DuckDB 1.5 and 2.x alike."""

from __future__ import annotations

import json
import warnings

import duckdb
import pytest

from havn.engine.database import finish_pending_result, result_to_arrow
from havn.engine.profile_json import normalize_profile


def test_finish_pending_result_frees_the_connection_after_fetchone():
    # DuckDB 2.0 keeps a half-read result open, and create_function refuses
    # to run until it is finished. On 1.x the helper is a no-op.
    conn = duckdb.connect()
    conn.execute("SELECT * FROM range(10)").fetchone()
    finish_pending_result(conn)
    conn.create_function("twice", lambda x: x * 2, [int], int)
    assert conn.execute("SELECT twice(21)").fetchall() == [(42,)]


def test_finish_pending_result_on_idle_connection_and_other_objects():
    conn = duckdb.connect()
    finish_pending_result(conn)
    finish_pending_result(conn.cursor())
    finish_pending_result(object())


def test_finish_pending_result_swallows_a_late_error():
    # An error the abandoned result would raise on a later fetch is not the
    # next caller's problem.
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t AS SELECT range::VARCHAR AS v FROM range(200000)")
    conn.execute("INSERT INTO t VALUES ('x')")
    try:
        conn.execute("SELECT v::INTEGER FROM t").fetchone()
    except duckdb.Error:
        pass  # DuckDB 1.x materialises the result and raises here
    finish_pending_result(conn)
    assert conn.execute("SELECT 1").fetchall() == [(1,)]


def test_result_to_arrow_reads_an_executed_result_without_warning():
    conn = duckdb.connect()
    conn.execute("SELECT * FROM range(3) r(i)")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        table = result_to_arrow(conn)
    assert table.column("i").to_pylist() == [0, 1, 2]


_SECTIONED = {
    "io": {"total_bytes_read": 10, "total_bytes_written": 20},
    "query": {"sql": "SELECT 1", "total_time": 0.5, "cpu_time": 0.25, "total_rows_scanned": 7},
    "system": {"peak_buffer_memory": 4096, "peak_temp_dir_size": 8, "total_memory_allocated": 1024},
    "optimizer": {"join_order": 0.001},
    "operator": [{
        "name": "RESULT_COLLECTOR", "type": "RESULT_COLLECTOR", "timing": 0.0,
        "intermediate_rows": 0, "extra_info": {},
        "children": [{
            "name": "SEQ_SCAN", "type": "TABLE_SCAN", "timing": 0.002,
            "intermediate_rows": 3, "rows_scanned": 7,
            "extra_info": {"Table": "memory.main.t", "Estimated Cardinality": "3"},
            "children": [],
        }],
    }],
}


def test_normalize_profile_maps_the_sectioned_layout():
    flat = normalize_profile(json.loads(json.dumps(_SECTIONED)))
    assert flat["query_name"] == "SELECT 1"
    assert flat["latency"] == 0.5
    assert flat["cpu_time"] == 0.25
    assert flat["cumulative_rows_scanned"] == 7
    assert flat["system_peak_buffer_memory"] == 4096
    assert flat["system_peak_temp_dir_size"] == 8
    assert flat["total_memory_allocated"] == 1024
    assert flat["total_bytes_read"] == 10 and flat["total_bytes_written"] == 20
    # RESULT_COLLECTOR, which 1.x never showed, is dropped.
    (scan,) = flat["children"]
    assert scan["operator_name"] == "SEQ_SCAN"
    assert scan["operator_type"] == "TABLE_SCAN"
    assert scan["operator_cardinality"] == 3
    assert scan["operator_rows_scanned"] == 7
    assert scan["operator_timing"] == 0.002
    assert scan["extra_info"]["Table"] == "memory.main.t"
    assert scan["children"] == []


def test_normalize_profile_leaves_the_flat_layout_alone():
    flat = {"latency": 1.0, "children": [{"operator_name": "SEQ_SCAN", "children": []}]}
    assert normalize_profile(flat) is flat


@pytest.mark.parametrize("sql", ["SELECT count(*) FROM range(1000)"])
def test_real_profile_normalizes_to_the_flat_layout(sql):
    conn = duckdb.connect()
    conn.execute("PRAGMA enable_profiling='no_output'")
    conn.execute(sql).fetchall()
    profile = normalize_profile(json.loads(conn.get_profiling_information(format="json")))
    assert profile["children"], profile
    assert "latency" in profile
    nodes, stack = [], list(profile["children"])
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(node.get("children") or [])
    assert all("operator_name" in n and "operator_timing" in n for n in nodes)


def _sectioned(peak: int, spilled: int, temp_dir: int, allocated: int = 0) -> dict:
    p = json.loads(json.dumps(_SECTIONED))
    p["system"] = {
        "peak_buffer_memory": peak, "peak_temp_dir_size": temp_dir,
        "total_memory_allocated": allocated, "total_bytes_spilled": spilled,
    }
    return normalize_profile(p)


def test_sectioned_profile_is_marked_per_query():
    from havn.engine.profile_json import PER_QUERY_PEAKS

    flat = normalize_profile(json.loads(json.dumps(_SECTIONED)))
    assert flat[PER_QUERY_PEAKS] is True


def test_capture_uses_per_query_peak_and_spill_on_duckdb2_profiles():
    # DuckDB 2.0's peaks are per query: a build after a bigger one used to be
    # read as "did not raise the connection's high-water mark" and record 0.
    from havn.engine.perf.capture import BuildCapture

    big = BuildCapture(model="a")
    big.absorb(_sectioned(peak=200_000_000, spilled=75_000_000, temp_dir=77_000_000, allocated=1_200_000_000))
    small = BuildCapture(model="b")
    small.absorb(
        _sectioned(peak=85_000_000, spilled=0, temp_dir=69_000_000),
        previous_marks=(200_000_000, 77_000_000),
    )
    assert big.peak_memory_bytes == 200_000_000  # not the 1.2 GB of allocations
    assert big.spill_bytes == 75_000_000
    assert small.peak_memory_bytes == 85_000_000
    assert small.spill_bytes == 0  # leftover temp files are not this query's spill


def test_capture_keeps_high_water_logic_for_duckdb1_profiles():
    from havn.engine.perf.capture import BuildCapture

    flat = {"latency": 0.1, "system_peak_buffer_memory": 100, "system_peak_temp_dir_size": 10,
            "total_memory_allocated": 40, "children": []}
    cap = BuildCapture(model="m")
    cap.absorb(flat, previous_marks=(100, 10))  # did not raise the marks
    assert cap.peak_memory_bytes == 40 and cap.spill_bytes == 0
    cap2 = BuildCapture(model="m")
    cap2.absorb(flat, previous_marks=(50, 4))
    assert cap2.peak_memory_bytes == 100 and cap2.spill_bytes == 6


def test_build_after_a_bigger_build_records_its_own_peak():
    from havn.engine.perf.capture import BuildCapture, activate, run_build_statement

    conn = duckdb.connect()
    conn.execute("CREATE TABLE t AS SELECT i, i % 50000 AS g FROM range(1000000) r(i)")
    caps = []
    for sql in (
        "CREATE TABLE a AS SELECT g, list(i) AS l FROM t GROUP BY g",
        "CREATE TABLE b AS SELECT g % 7 AS g2, sum(i) AS s FROM t GROUP BY 1",
    ):
        cap = BuildCapture(model=sql[13:14])
        with activate(cap):
            run_build_statement(conn, sql)
        caps.append(cap)
    assert caps[0].statements == caps[1].statements == 1
    if int(duckdb.__version__.split(".")[0]) >= 2:
        assert caps[1].peak_memory_bytes > 0

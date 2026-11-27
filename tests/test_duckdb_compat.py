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

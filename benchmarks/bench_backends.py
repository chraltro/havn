"""Benchmark harness: DuckDB vs DuckLake backends.

Generates TPC-H-lite data with the DuckDB ``tpch`` extension, materialises it
into each backend, and times a fixed workload (load, scan, filter, aggregate,
join, concurrent write, storage). Reports a markdown table with wall-clock
milliseconds per benchmark.

Run from the repo root:

    python benchmarks/bench_backends.py

Flags:

    --scales 0.1 1.0          scales to run (default: 0.1 1.0)
    --backends duckdb ducklake backends to run (default: both)
    --out benchmarks/results   write markdown report to file
    --iterations 3             warm-scan iteration count
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gc
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import duckdb


# --- Standard TPC-H queries --------------------------------------------------


# Simplified TPC-H Q1: 7 aggregates over a filtered lineitem scan.
Q_AGGREGATION = """
SELECT
    l_returnflag,
    l_linestatus,
    sum(l_quantity) AS sum_qty,
    sum(l_extendedprice) AS sum_base_price,
    sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
    avg(l_quantity) AS avg_qty,
    avg(l_extendedprice) AS avg_price,
    count(*) AS count_order
FROM lineitem
WHERE l_shipdate <= DATE '1998-09-02'
GROUP BY l_returnflag, l_linestatus
ORDER BY l_returnflag, l_linestatus
"""

# Simplified TPC-H Q3: 3-way join.
Q_JOIN = """
SELECT
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    o_orderdate
FROM customer, orders, lineitem
WHERE c_mktsegment = 'BUILDING'
  AND c_custkey = o_custkey
  AND l_orderkey = o_orderkey
  AND o_orderdate < DATE '1995-03-15'
  AND l_shipdate > DATE '1995-03-15'
GROUP BY l_orderkey, o_orderdate
ORDER BY revenue DESC, o_orderdate
LIMIT 10
"""

# Filtered scan with 10-20% selectivity.
Q_FILTER = """
SELECT count(*), sum(l_extendedprice) AS total
FROM lineitem
WHERE l_shipdate > DATE '1997-01-01' AND l_quantity > 30
"""

Q_COUNT = "SELECT count(*) FROM lineitem"

TPCH_TABLES = (
    "customer", "orders", "lineitem", "partsupp",
    "part", "supplier", "nation", "region",
)


# --- Timing helpers ----------------------------------------------------------


def _time_ms(fn, *args, **kwargs) -> tuple[float, object]:
    """Run ``fn`` once, return (wall-ms, return value)."""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return (time.perf_counter() - t0) * 1000, result


def _time_query(conn: duckdb.DuckDBPyConnection, sql: str) -> float:
    """Execute ``sql`` and fully materialise the result, return wall-ms.

    DuckDB's ``execute`` returns a relation lazily; real work happens only
    when rows are pulled. ``fetchall`` forces full materialisation so the
    measurement reflects end-to-end query latency.
    """
    t0 = time.perf_counter()
    conn.execute(sql).fetchall()
    return (time.perf_counter() - t0) * 1000


@dataclass
class Sample:
    label: str
    backend: str
    scale: float
    metric: str              # "ms" or "MB"
    value: float


# --- TPC-H data generation (cached per scale) --------------------------------


def generate_tpch(scale: float, cache_dir: Path) -> Path:
    """Generate TPC-H at ``scale`` and export each table to Parquet.

    Caches under ``cache_dir/sf{scale}/``. Returns the cache directory.
    """
    out = cache_dir / f"sf{scale}"
    marker = out / ".done"
    if marker.exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    print(f"[gen] generating TPC-H sf={scale} into {out}", flush=True)
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL tpch")
        conn.execute("LOAD tpch")
        conn.execute(f"CALL dbgen(sf={scale})")
        for tbl in TPCH_TABLES:
            conn.execute(
                f"COPY {tbl} TO '{out / (tbl + '.parquet')}' (FORMAT PARQUET)"
            )
    finally:
        conn.close()
    marker.touch()
    return out


# --- Backend factories -------------------------------------------------------


def _apply_common_settings(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("SET threads = 4")
    conn.execute("SET memory_limit = '4GB'")


def open_duckdb(db_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(db_path))
    _apply_common_settings(conn)
    return conn


def open_ducklake(workdir: Path) -> duckdb.DuckDBPyConnection:
    """Open a DuckLake-backed connection with local catalog + local Parquet."""
    catalog = workdir / "catalog.ducklake"
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL ducklake")
    conn.execute("LOAD ducklake")
    conn.execute(
        f"ATTACH 'ducklake:{catalog.as_posix()}' AS warehouse "
        f"(DATA_PATH '{data_dir.as_posix()}')"
    )
    conn.execute("USE warehouse")
    _apply_common_settings(conn)
    return conn


def open_backend(kind: str, workdir: Path) -> duckdb.DuckDBPyConnection:
    if kind == "duckdb":
        return open_duckdb(workdir / "warehouse.duckdb")
    if kind == "ducklake":
        return open_ducklake(workdir)
    raise ValueError(f"unknown backend: {kind}")


def storage_mb(kind: str, workdir: Path) -> float:
    if kind == "duckdb":
        p = workdir / "warehouse.duckdb"
        return p.stat().st_size / (1024 * 1024) if p.exists() else 0.0
    # DuckLake: catalog + data dir
    total = 0
    for p in workdir.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total / (1024 * 1024)


# --- Load -------------------------------------------------------------------


def load_from_parquet(
    conn: duckdb.DuckDBPyConnection, parquet_dir: Path
) -> None:
    for tbl in TPCH_TABLES:
        parquet = parquet_dir / f"{tbl}.parquet"
        conn.execute(
            f"CREATE TABLE {tbl} AS SELECT * FROM read_parquet('{parquet.as_posix()}')"
        )


# --- Concurrent-write benchmark ---------------------------------------------


def _bench_concurrent_write(kind: str, workdir: Path, n_workers: int = 4,
                            rows_per_worker: int = 100_000) -> float:
    """Run N parallel writers against the backend, return wall-ms.

    DuckDB can't be attached to the same DuckLake catalog twice from the same
    process, and DuckDB file-backed connections take an exclusive lock. The
    realistic in-process pattern for both backends is therefore: one shared
    connection, N thread cursors. DuckDB will still serialise writes via its
    internal write lock; DuckLake uses optimistic concurrency over its Postgres
    (or in our case, local) catalog. This measures wall-clock throughput under
    the same concurrency pattern both backends actually support in-process.
    """
    conn = open_backend(kind, workdir)
    try:
        def _worker(writer_id: int) -> int:
            cur = conn.cursor()
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS writer_{writer_id} "
                "(id INTEGER, v DOUBLE, s VARCHAR)"
            )
            cur.execute(
                f"INSERT INTO writer_{writer_id} "
                f"SELECT i, random(), 'x' || i::VARCHAR "
                f"FROM range({rows_per_worker}) AS t(i)"
            )
            return rows_per_worker

        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as ex:
            list(ex.map(_worker, range(n_workers)))
        return (time.perf_counter() - t0) * 1000
    finally:
        conn.close()


# --- Per-backend, per-scale run ---------------------------------------------


def run_one(kind: str, scale: float, parquet_dir: Path, iterations: int
            ) -> list[Sample]:
    samples: list[Sample] = []
    with tempfile.TemporaryDirectory(prefix=f"bench_{kind}_{scale}_") as td:
        workdir = Path(td)

        # 1. Load.
        conn = open_backend(kind, workdir)
        try:
            load_ms, _ = _time_ms(load_from_parquet, conn, parquet_dir)
            samples.append(Sample("load", kind, scale, "ms", load_ms))
        finally:
            conn.close()

        # 2. Storage — measured after load, before any query.
        samples.append(Sample("storage", kind, scale, "MB",
                              storage_mb(kind, workdir)))

        # 3. Attach/connect latency — 3 fresh connects, report median.
        connect_samples = []
        for _ in range(3):
            gc.collect()
            t0 = time.perf_counter()
            c = open_backend(kind, workdir)
            connect_samples.append((time.perf_counter() - t0) * 1000)
            c.close()
        samples.append(Sample("attach", kind, scale, "ms",
                              median(connect_samples)))

        # 4. Cold scan — fresh connection, first query.
        conn = open_backend(kind, workdir)
        try:
            cold_ms = _time_query(conn, Q_COUNT)
            samples.append(Sample("cold_scan", kind, scale, "ms", cold_ms))

            # 5. Warm scan — subsequent iterations on same connection.
            warm = [_time_query(conn, Q_COUNT) for _ in range(iterations)]
            samples.append(Sample("warm_scan", kind, scale, "ms", median(warm)))

            # 6. Filtered scan.
            samples.append(Sample("filter", kind, scale, "ms",
                                  _time_query(conn, Q_FILTER)))

            # 7. Aggregation (TPC-H Q1).
            samples.append(Sample("aggregation", kind, scale, "ms",
                                  _time_query(conn, Q_AGGREGATION)))

            # 8. Join (TPC-H Q3-like).
            samples.append(Sample("join", kind, scale, "ms",
                                  _time_query(conn, Q_JOIN)))
        finally:
            conn.close()

        # 9. Concurrent writes — 4 workers, separate tables.
        cw_ms = _bench_concurrent_write(kind, workdir)
        samples.append(Sample("concurrent_write", kind, scale, "ms", cw_ms))

    return samples


# --- Reporting --------------------------------------------------------------


def format_markdown(samples: list[Sample]) -> str:
    scales = sorted({s.scale for s in samples})
    backends = sorted({s.backend for s in samples})
    benchmarks = [
        "load", "storage", "attach", "cold_scan", "warm_scan",
        "filter", "aggregation", "join", "concurrent_write",
    ]

    lines: list[str] = []
    lines.append("# havn backend benchmark results")
    lines.append("")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append(
        "Lower is better for all rows except `storage` (which is informational). "
        "Unit column: `ms` = wall-clock milliseconds, `MB` = on-disk megabytes."
    )
    lines.append("")

    lookup: dict[tuple[str, str, float], Sample] = {}
    for s in samples:
        lookup[(s.metric, s.backend, s.scale)] = s

    for scale in scales:
        lines.append(f"## Scale factor {scale}")
        lines.append("")
        header = "| benchmark | unit | " + " | ".join(backends) + " | winner |"
        sep = "|" + "---|" * (len(backends) + 3)
        lines.append(header)
        lines.append(sep)
        for bench in benchmarks:
            row_samples = {
                b: next((s for s in samples
                         if s.label == bench and s.backend == b and s.scale == scale),
                        None)
                for b in backends
            }
            if not any(row_samples.values()):
                continue
            unit = next(s.metric for s in row_samples.values() if s is not None)
            cells = []
            values = {}
            for b in backends:
                s = row_samples[b]
                if s is None:
                    cells.append("—")
                else:
                    cells.append(f"{s.value:,.1f}")
                    values[b] = s.value
            if bench == "storage":
                winner = "n/a"
            elif len(values) >= 2:
                fastest = min(values, key=values.get)
                slowest = max(values, key=values.get)
                if values[slowest] == 0:
                    winner = "tie"
                else:
                    ratio = values[slowest] / max(values[fastest], 0.0001)
                    winner = f"**{fastest}** ({ratio:.2f}×)"
            else:
                winner = "n/a"
            lines.append(f"| {bench} | {unit} | " + " | ".join(cells) + f" | {winner} |")
        lines.append("")

    return "\n".join(lines) + "\n"


# --- CLI --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="havn backend benchmarks")
    parser.add_argument("--scales", nargs="+", type=float, default=[0.1, 1.0])
    parser.add_argument("--backends", nargs="+", default=["duckdb", "ducklake"])
    parser.add_argument("--iterations", type=int, default=3,
                        help="warm-scan iteration count (default: 3)")
    parser.add_argument("--out", type=Path, default=None,
                        help="optional directory to write markdown report")
    parser.add_argument("--cache", type=Path, default=Path(".benchmark-cache"),
                        help="Parquet cache directory (default: .benchmark-cache)")
    args = parser.parse_args()

    # 1. Generate (or reuse cached) TPC-H Parquet for each scale.
    parquet_dirs = {sf: generate_tpch(sf, args.cache) for sf in args.scales}

    # 2. Run each (scale, backend) pair.
    all_samples: list[Sample] = []
    for sf in args.scales:
        for kind in args.backends:
            print(f"[run] backend={kind} scale={sf}", flush=True)
            samples = run_one(kind, sf, parquet_dirs[sf], args.iterations)
            for s in samples:
                print(f"  {s.label:<18} {s.value:>10,.2f} {s.metric}")
            all_samples.extend(samples)

    report = format_markdown(all_samples)
    print()
    print(report)

    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        fname = args.out / f"bench_{time.strftime('%Y-%m-%d_%H%M%S')}.md"
        fname.write_text(report, encoding="utf-8")
        print(f"[out] wrote {fname}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

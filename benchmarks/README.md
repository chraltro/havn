# havn backend benchmarks

Side-by-side performance comparison of the two `WarehouseBackend`
implementations — plain DuckDB (single file) and DuckLake (Parquet +
catalog).

## Running

```bash
python benchmarks/bench_backends.py                       # SF 0.1 + 1.0, both backends
python benchmarks/bench_backends.py --scales 0.1          # just one scale
python benchmarks/bench_backends.py --backends duckdb     # just one backend
python benchmarks/bench_backends.py --out results/        # write markdown report
```

Each run generates TPC-H-lite data with the `tpch` DuckDB extension,
materialises it into each backend, and times a standard workload:

| Benchmark       | What it measures                                    |
|-----------------|-----------------------------------------------------|
| load            | `CREATE TABLE ... AS SELECT FROM 'lineitem.parquet'` |
| attach          | connect/ATTACH latency from cold start (3 samples)  |
| cold scan       | `SELECT count(*) FROM lineitem` on fresh connection |
| warm scan       | same query, 3 more iterations, median reported      |
| filtered scan   | 1-predicate scan with 10-20% selectivity            |
| aggregation     | TPC-H Q1 (7 aggregates, 4 group columns)            |
| join            | 3-way join over customer / orders / lineitem        |
| concurrent write| 4 threads each appending 100k rows to 4 tables      |
| storage         | on-disk warehouse size after load                   |

All timings are wall-clock milliseconds, lower is better. Warm scans
report the median of 3 runs; everything else is a single measurement.

DuckLake is configured with a local `.ducklake` catalog and a local
`data/` Parquet directory — no Postgres, no S3, so the numbers reflect
the catalog hop and Parquet footer overhead only, not network latency.

## Scales

- `0.1`  — ~600k lineitem rows, completes in a minute or so.
- `1.0`  — ~6M lineitem rows, completes in a few minutes.
- `10.0` — ~60M lineitem rows; use only on a workstation with 32+ GB RAM.

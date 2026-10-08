# havn benchmarks

- `bench_backends.py`: plain DuckDB vs DuckLake on a TPC-H-lite workload.
- `bench_parse.py`: parse, DAG, validation and lineage over a synthetic
  project of N models.

# Backend benchmarks

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

# Parse and lineage benchmarks

How long the whole-project passes take as the model count grows, so a
regression in parsing or catalog access shows up as a number.

```bash
python benchmarks/bench_parse.py                  # 300 models
python benchmarks/bench_parse.py --models 1000    # the large project
python benchmarks/bench_parse.py --repeat 5       # more iterations, median reported
python benchmarks/bench_parse.py --keep           # leave the project on disk
python benchmarks/bench_parse.py --out results/   # write markdown report
```

A synthetic project is generated in a temp directory: a
landing → bronze → silver → gold chain, roughly 50/30/20 across the three
layers, where every silver and gold model joins two upstreams and gold
also goes through a CTE. Empty tables are materialised for every landing
source and every model so the catalog has the right shape.

| Pass                                  | What it measures                                          |
|---------------------------------------|-----------------------------------------------------------|
| discover_models (parse plus deps)     | read, directive parse, sqlglot parse, dependency extraction |
| build_dag                             | topological sort only                                       |
| validate_models, built catalog        | table and column checks against a populated catalog         |
| column lineage, unbuilt catalog       | AST tracing with no catalog lookups at all                  |
| column lineage, built catalog         | one catalog read per model, as a single-model API call does |
| column lineage, built catalog, shared | one catalog read for the whole pass                         |

The last two rows are the same work with and without a shared catalog;
the gap is what a full-project lineage pass saves by fetching once.

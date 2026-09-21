"""Benchmark harness: parse, DAG, validation and lineage at scale.

Generates a synthetic project of N models in a temp directory -- a
landing -> bronze -> silver -> gold chain where every silver and gold model
joins two upstreams -- and times the passes that run over the whole project:
``discover_models``, ``build_dag``, ``validate_models`` and full column
lineage. Reports wall-clock milliseconds and microseconds per model.

Lineage is measured twice, against an empty catalog and against a built one,
because resolving ``SELECT *`` and unqualified columns only touches
``information_schema`` when the tables exist. That second number is the one
that used to grow with models times dependencies.

Run from the repo root:

    python benchmarks/bench_parse.py

Flags:

    --models 300               model count (default: 300)
    --models 1000              the large project
    --repeat 3                 iterations per pass, median reported
    --keep                     leave the generated project on disk
    --out benchmarks/results   write markdown report to file
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import duckdb

# Run from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from havn.engine.sql_analysis import fetch_column_catalog  # noqa: E402
from havn.engine.transform import (  # noqa: E402
    build_dag,
    discover_models,
    extract_column_lineage,
    validate_models,
)


# --- Synthetic project -------------------------------------------------------


# Bronze reads one landing table and does light cleanup.
BRONZE_SQL = """\
@config materialized=table, schema=bronze
@description Cleaned rows from source {i}

SELECT
    l.id,
    l.name,
    l.region,
    l.amount,
    l.updated_at
FROM landing.source_{i} AS l
WHERE l.amount IS NOT NULL
"""

# Silver joins two bronze models.
SILVER_SQL = """\
@config materialized=table, schema=silver

SELECT
    a.id,
    a.name,
    a.region,
    a.amount AS amount_primary,
    b.amount AS amount_secondary,
    a.amount + b.amount AS amount_total,
    greatest(a.updated_at, b.updated_at) AS updated_at
FROM bronze.b{left} AS a
JOIN bronze.b{right} AS b ON a.id = b.id
"""

# Gold joins two silver models through a CTE and aggregates.
GOLD_SQL = """\
@config materialized=table, schema=gold
@assert row_count >= 0

WITH totals AS (
    SELECT
        s.id,
        s.region,
        s.amount_total
    FROM silver.s{left} AS s
    WHERE s.amount_total > 0
)

SELECT
    t.region,
    count(*) AS order_count,
    sum(t.amount_total) AS amount_total,
    max(o.amount_secondary) AS amount_peak
FROM totals AS t
JOIN silver.s{right} AS o ON t.id = o.id
GROUP BY t.region
"""


def _layer_sizes(n_models: int) -> tuple[int, int, int]:
    """Split N models across bronze / silver / gold, roughly 50/30/20."""
    bronze = max(2, n_models // 2)
    silver = max(1, (n_models * 3) // 10)
    gold = max(1, n_models - bronze - silver)
    return bronze, silver, gold


def generate_project(root: Path, n_models: int) -> tuple[Path, tuple[int, int, int]]:
    """Write a transform/ tree of ``n_models`` SQL files. Returns its path."""
    bronze_n, silver_n, gold_n = _layer_sizes(n_models)
    transform = root / "transform"
    for layer in ("bronze", "silver", "gold"):
        (transform / layer).mkdir(parents=True, exist_ok=True)

    for i in range(bronze_n):
        (transform / "bronze" / f"b{i}.sql").write_text(BRONZE_SQL.format(i=i))

    for i in range(silver_n):
        # Two distinct bronze parents, so the DAG actually branches.
        left = i % bronze_n
        right = (i + 1) % bronze_n
        (transform / "silver" / f"s{i}.sql").write_text(
            SILVER_SQL.format(left=left, right=right)
        )

    for i in range(gold_n):
        left = i % silver_n
        right = (i + 1) % silver_n
        (transform / "gold" / f"g{i}.sql").write_text(
            GOLD_SQL.format(left=left, right=right)
        )

    return transform, (bronze_n, silver_n, gold_n)


def build_catalog(
    db_path: Path,
    transform_dir: Path,
    bronze_n: int,
) -> duckdb.DuckDBPyConnection:
    """Materialise empty tables for every landing source and every model.

    The rows do not matter -- only that ``information_schema`` has the right
    shape, which is what lineage and validation read.
    """
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA IF NOT EXISTS landing")
    for i in range(bronze_n):
        conn.execute(
            f"CREATE OR REPLACE TABLE landing.source_{i} AS SELECT "
            "1 AS id, 'n' AS name, 'r' AS region, 1.0 AS amount, "
            "current_timestamp AS updated_at WHERE false"
        )
    for schema in ("bronze", "silver", "gold"):
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for model in build_dag(discover_models(transform_dir)):
        conn.execute(f"CREATE OR REPLACE TABLE {model.full_name} AS\n{model.query}")
    return conn


# --- Timing helpers ----------------------------------------------------------


def _time_ms(fn, *args, **kwargs) -> tuple[float, object]:
    """Run ``fn`` once, return (wall-ms, return value)."""
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return (time.perf_counter() - t0) * 1000, result


def _repeat_ms(fn, repeat: int) -> float:
    """Median wall-ms over ``repeat`` runs."""
    return median(_time_ms(fn)[0] for _ in range(repeat))


@dataclass
class Sample:
    label: str
    n_models: int
    ms: float

    @property
    def us_per_model(self) -> float:
        return (self.ms * 1000) / self.n_models if self.n_models else 0.0


# --- The passes --------------------------------------------------------------


def lineage_pass(models: list, conn=None, shared_catalog: bool = False) -> int:
    """Full column lineage over every model. Returns columns traced."""
    catalog = fetch_column_catalog(conn) if (conn is not None and shared_catalog) else None
    traced = 0
    for model in models:
        traced += len(
            extract_column_lineage(model, conn=conn, column_catalog=catalog)
        )
    return traced


def run_one(transform_dir: Path, conn, n_models: int, repeat: int) -> list[Sample]:
    samples: list[Sample] = []

    samples.append(Sample(
        "discover_models (parse plus deps)", n_models,
        _repeat_ms(lambda: discover_models(transform_dir), repeat),
    ))

    models = discover_models(transform_dir)
    samples.append(Sample(
        "build_dag", n_models,
        _repeat_ms(lambda: build_dag(models), repeat),
    ))
    samples.append(Sample(
        "validate_models, built catalog", n_models,
        _repeat_ms(lambda: validate_models(conn, models), repeat),
    ))
    samples.append(Sample(
        "column lineage, unbuilt catalog", n_models,
        _repeat_ms(lambda: lineage_pass(models), repeat),
    ))
    samples.append(Sample(
        "column lineage, built catalog", n_models,
        _repeat_ms(lambda: lineage_pass(models, conn=conn), repeat),
    ))
    samples.append(Sample(
        "column lineage, built catalog, shared", n_models,
        _repeat_ms(lambda: lineage_pass(models, conn=conn, shared_catalog=True), repeat),
    ))
    return samples


# --- Reporting ---------------------------------------------------------------


def format_markdown(samples: list[Sample], layers: tuple[int, int, int]) -> str:
    bronze_n, silver_n, gold_n = layers
    lines = [
        "# havn parse and lineage benchmark",
        "",
        f"Synthetic project: {bronze_n} bronze, {silver_n} silver, {gold_n} gold.",
        "",
        "| Pass | Time | Per model |",
        "|---|---|---|",
    ]
    for s in samples:
        lines.append(f"| {s.label} | {s.ms:,.1f} ms | {s.us_per_model:,.0f} us |")
    lines.append("")
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="havn parse and lineage benchmarks")
    parser.add_argument("--models", type=int, default=300,
                        help="number of models to generate (default: 300)")
    parser.add_argument("--repeat", type=int, default=3,
                        help="iterations per pass, median reported (default: 3)")
    parser.add_argument("--keep", action="store_true",
                        help="leave the generated project on disk")
    parser.add_argument("--out", type=Path, default=None,
                        help="optional directory to write markdown report")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="havn-bench-parse-"))
    conn = None
    try:
        print(f"[gen] generating {args.models} models in {workdir}", flush=True)
        ms, (transform_dir, layers) = _time_ms(
            generate_project, workdir, args.models
        )
        print(f"[gen] wrote {sum(layers)} files in {ms:,.0f} ms", flush=True)

        print("[gen] building the catalog", flush=True)
        ms, conn = _time_ms(
            build_catalog, workdir / "warehouse.duckdb", transform_dir, layers[0]
        )
        print(f"[gen] catalog built in {ms:,.0f} ms", flush=True)

        print(f"[run] {args.repeat} iterations per pass", flush=True)
        samples = run_one(transform_dir, conn, sum(layers), args.repeat)
        for s in samples:
            print(f"  {s.label:<40} {s.ms:>10,.1f} ms  {s.us_per_model:>8,.0f} us/model")

        report = format_markdown(samples, layers)
        print()
        print(report)

        if args.out is not None:
            args.out.mkdir(parents=True, exist_ok=True)
            fname = args.out / f"bench_parse_{time.strftime('%Y-%m-%d_%H%M%S')}.md"
            fname.write_text(report, encoding="utf-8")
            print(f"[out] wrote {fname}")
    finally:
        if conn is not None:
            conn.close()
        if args.keep:
            print(f"[keep] project left at {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())

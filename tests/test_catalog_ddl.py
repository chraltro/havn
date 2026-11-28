"""Concurrent runs in one process creating the same schemas and tables.

With ``scheduler.max_concurrent`` above 1, two runs start together on a
fresh warehouse and both create ``landing``, ``bronze`` and their models'
schemas (the ``_havn`` tables come from the migrations, under their own
lock). DuckDB fails the second creator with "Catalog write-write
conflict"; ``engine/catalog_ddl.py`` serialises those steps and retries a
conflict with a writer outside it.
"""

from __future__ import annotations

import textwrap
import threading
from pathlib import Path

import duckdb

from havn.engine import catalog_ddl as catalog_mod
from havn.engine.catalog_ddl import catalog_ddl


def test_a_conflict_with_an_outside_writer_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_mod, "_ATTEMPTS", 200)
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    try:
        outside, ours = conn.cursor(), conn.cursor()
        outside.execute("BEGIN")
        outside.execute("CREATE SCHEMA s")          # uncommitted: ours conflicts
        tries: list[int] = []

        def create():
            tries.append(1)
            if len(tries) == 2:
                outside.execute("COMMIT")           # the other writer finishes
            ours.execute("CREATE SCHEMA IF NOT EXISTS s")

        catalog_ddl(create)
        assert len(tries) >= 2
        assert conn.execute(
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 's'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_other_errors_are_not_retried(tmp_path):
    conn = duckdb.connect(str(tmp_path / "w.duckdb"))
    tries: list[int] = []

    def bad():
        tries.append(1)
        conn.execute("CREATE TABLE nope.t (n INT)")

    try:
        try:
            catalog_ddl(bad)
        except duckdb.CatalogException:
            pass
        assert tries == [1]
    finally:
        conn.close()


PROJECT = """\
    name: t
    database:
      path: warehouse.duckdb
    rewind:
      enabled: false
    streams:
      one:
        steps:
          - ingest: [a]
          - transform: [silver.from_a]
      two:
        steps:
          - ingest: [b]
          - transform: [gold.from_b]
"""


def _project(root: Path) -> Path:
    p = root / "proj"
    for d in ("ingest", "transform/silver", "transform/gold"):
        (p / d).mkdir(parents=True)
    (p / "project.yml").write_text(textwrap.dedent(PROJECT), encoding="utf-8")
    for name in ("a", "b"):
        (p / "ingest" / f"{name}.py").write_text(
            f"db.execute('CREATE OR REPLACE TABLE landing.{name} AS SELECT 1 AS n')\n", encoding="utf-8",
        )
    (p / "transform" / "silver" / "from_a.sql").write_text(
        "@config materialized=table, schema=silver\n\nSELECT n FROM landing.a\n", encoding="utf-8")
    (p / "transform" / "gold" / "from_b.sql").write_text(
        "@config materialized=table, schema=gold\n\nSELECT n FROM landing.b\n", encoding="utf-8")
    return p


def test_two_runs_starting_together_on_a_fresh_warehouse(tmp_path):
    """As two scheduled streams do with max_concurrent: 2."""
    from havn.config import load_project
    from havn.engine.stream_runner import run_stream

    for i in range(4):
        project = _project(tmp_path / str(i))
        config = load_project(project)
        # One connection held open throughout, as the scheduler's lease does,
        # so both runs share one database instance.
        keep = duckdb.connect(str(project / "warehouse.duckdb"))
        start = threading.Barrier(2, timeout=30)
        results: dict[str, object] = {}

        def run(name: str) -> None:
            start.wait()
            results[name] = run_stream(config, project, name, trigger="scheduled")

        threads = [threading.Thread(target=run, args=(n,)) for n in ("one", "two")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        keep.close()
        assert {n: (r.ok, r.error) for n, r in results.items()} == {
            "one": (True, None), "two": (True, None),
        }

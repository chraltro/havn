"""havn migrate — copy a warehouse between DuckDB and DuckLake backends."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml
from rich.table import Table

from havn.cli import _resolve_project, app, console


_MIGRATE_SCHEMAS = ["landing", "bronze", "silver", "gold", "seeds", "_havn"]


@app.command()
def migrate(
    to: Annotated[str, typer.Option("--to", help="Target backend: duckdb or ducklake")],
    project_dir: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory")] = None,
    catalog: Annotated[Optional[str], typer.Option("--catalog", help="DuckLake catalog path (when --to ducklake)")] = None,
    data_path: Annotated[Optional[str], typer.Option("--data-path", help="DuckLake data dir (when --to ducklake)")] = None,
    path: Annotated[Optional[str], typer.Option("--path", help="DuckDB warehouse path (when --to duckdb)")] = None,
) -> None:
    """Migrate the warehouse to a different backend (DuckDB <-> DuckLake).

    Copies every table in landing, bronze, silver, gold, seeds, _havn to
    the destination backend; verifies row counts; rewrites project.yml;
    renames the source to <source>.backup.
    """
    from havn.config import DatabaseConfig, load_project
    from havn.engine.backends import create_backend

    if to not in ("duckdb", "ducklake"):
        console.print(f"[red]Invalid target backend: {to!r}. Must be 'duckdb' or 'ducklake'.[/red]")
        raise typer.Exit(code=1)

    project_dir = _resolve_project(project_dir)
    config = load_project(project_dir)
    src_config = config.database

    if src_config.backend == to:
        console.print(f"[yellow]Warehouse is already on {to} backend. Nothing to do.[/yellow]")
        return

    # Build destination DatabaseConfig
    if to == "ducklake":
        dest_catalog = catalog or ".havn/catalog.ducklake"
        dest_data_path = data_path or ".havn/data"
        (project_dir / ".havn" / "data").mkdir(parents=True, exist_ok=True)
        dest_config = DatabaseConfig(
            backend="ducklake",
            catalog=dest_catalog,
            data_path=dest_data_path,
            encrypted=src_config.encrypted,
            memory_limit=src_config.memory_limit,
            threads=src_config.threads,
        )
    else:
        dest_path = path or "warehouse.duckdb"
        if (project_dir / dest_path).exists():
            dest_path = "warehouse.migrated.duckdb"
            console.print(f"[yellow]warehouse.duckdb exists, writing to {dest_path}[/yellow]")
        dest_config = DatabaseConfig(
            backend="duckdb",
            path=dest_path,
            memory_limit=src_config.memory_limit,
            threads=src_config.threads,
        )

    src_backend = create_backend(src_config, project_dir=project_dir)
    dest_backend = create_backend(dest_config, project_dir=project_dir)

    if not src_backend.exists():
        console.print("[red]Source warehouse not initialized. Nothing to migrate.[/red]")
        raise typer.Exit(code=1)

    console.print(f"[bold]Migrating:[/bold] {src_config.backend} -> {to}")

    src_conn = src_backend.connect(read_only=True)
    dest_conn = dest_backend.connect(read_only=False)

    # Stage via Parquet to avoid materializing whole tables into Python memory.
    import tempfile
    stage_dir = Path(tempfile.mkdtemp(prefix="havn-migrate-"))

    report: list[tuple[str, str, int, int, str]] = []  # schema, table, src_rows, dest_rows, status
    try:
        for schema in _MIGRATE_SCHEMAS:
            rows = src_conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = ? AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
                [schema],
            ).fetchall()
            if not rows:
                continue
            try:
                dest_conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            except Exception as e:
                console.print(f"[red]Failed creating schema {schema}: {e}[/red]")
                raise

            for (table_name,) in rows:
                fq = f'"{schema}"."{table_name}"'
                pq_path = stage_dir / f"{schema}__{table_name}.parquet"
                try:
                    src_count = src_conn.execute(f"SELECT count(*) FROM {fq}").fetchone()[0]
                    # Stream table -> Parquet -> destination
                    src_conn.execute(
                        f"COPY (SELECT * FROM {fq}) TO '{pq_path.as_posix()}' (FORMAT PARQUET)"
                    )
                    dest_conn.execute(
                        f"CREATE OR REPLACE TABLE {fq} AS "
                        f"SELECT * FROM read_parquet('{pq_path.as_posix()}')"
                    )
                    pq_path.unlink(missing_ok=True)
                    dest_count = dest_conn.execute(f"SELECT count(*) FROM {fq}").fetchone()[0]
                    status = "ok" if src_count == dest_count else "MISMATCH"
                    report.append((schema, table_name, src_count, dest_count, status))
                    if status == "MISMATCH":
                        raise RuntimeError(
                            f"Row count mismatch on {fq}: source={src_count}, dest={dest_count}"
                        )
                except Exception as e:
                    report.append((schema, table_name, -1, -1, f"error: {e}"))
                    raise
    except Exception:
        _print_report(report)
        console.print("[red]Migration failed. Destination left in place for inspection; source untouched.[/red]")
        src_conn.close()
        dest_conn.close()
        _cleanup_stage(stage_dir)
        raise typer.Exit(code=1)

    src_conn.close()
    dest_conn.close()
    _cleanup_stage(stage_dir)

    # Rewrite project.yml atomically
    _rewrite_project_yml(project_dir, dest_config)

    # Rename source
    _rename_source(project_dir, src_config)

    _print_report(report)
    console.print(f"[green]Migration complete.[/green] Project now uses {to} backend.")


def _cleanup_stage(stage_dir: Path) -> None:
    import shutil
    try:
        shutil.rmtree(stage_dir, ignore_errors=True)
    except Exception:
        pass


def _rewrite_project_yml(project_dir: Path, dest_config) -> None:
    """Atomically replace the database block in project.yml.

    Writes to a temp file first and renames on top of the target so a
    killed process never leaves a truncated project.yml.
    """
    yml_path = project_dir / "project.yml"
    raw = yaml.safe_load(yml_path.read_text()) or {}
    raw["database"] = dest_config.model_dump(exclude_none=True, exclude_defaults=False)
    if dest_config.backend == "duckdb":
        for k in ("catalog", "data_path", "metadata_schema", "encrypted"):
            raw["database"].pop(k, None)
    else:
        raw["database"].pop("path", None)
    tmp = yml_path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False))
    tmp.replace(yml_path)  # atomic on POSIX; os.replace semantics on Windows


def _rename_source(project_dir: Path, src_config) -> None:
    if src_config.backend == "duckdb":
        src_path = project_dir / src_config.path
        if src_path.exists():
            backup = src_path.with_suffix(src_path.suffix + ".backup")
            try:
                src_path.replace(backup)
                console.print(f"[dim]source backed up to {backup.name}[/dim]")
            except Exception as e:
                console.print(f"[yellow]Could not rename source: {e}[/yellow]")
    else:
        # DuckLake source: rename the catalog file (if local)
        catalog = src_config.catalog or ""
        if catalog and not catalog.startswith(("postgres:", "s3://")):
            p = Path(catalog)
            if not p.is_absolute():
                p = project_dir / p
            if p.exists():
                backup = p.with_suffix(p.suffix + ".backup")
                try:
                    p.replace(backup)
                    console.print(f"[dim]catalog backed up to {backup.name}[/dim]")
                except Exception as e:
                    console.print(f"[yellow]Could not rename source catalog: {e}[/yellow]")


def _print_report(rows: list[tuple[str, str, int, int, str]]) -> None:
    t = Table(title="Migration report")
    t.add_column("schema")
    t.add_column("table")
    t.add_column("source rows", justify="right")
    t.add_column("dest rows", justify="right")
    t.add_column("status")
    for schema, name, src, dst, status in rows:
        color = "green" if status == "ok" else "red"
        t.add_row(schema, name, str(src) if src >= 0 else "-", str(dst) if dst >= 0 else "-", f"[{color}]{status}[/{color}]")
    console.print(t)

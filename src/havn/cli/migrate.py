"""havn migrate — copy a warehouse between DuckDB and DuckLake backends."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
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
    renames the source to <source>.backup. The source's metadata schema is
    brought up to date first, and _havn rows go into the destination's own
    migrated tables.
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

    # project.yml is rewritten at the end; refuse now, before copying any
    # data, if it cannot be edited safely (e.g. it repeats a key).
    from havn import yamlio

    try:
        yamlio.check_editable(project_dir / "project.yml")
    except yamlio.YamlLoadError as e:
        console.print(f"[red]Cannot migrate: {e}[/red]")
        raise typer.Exit(code=1)

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

    from havn.engine.migrations import MigrationError, migration_status

    # Bring the source's metadata schema up to date before copying it. The
    # destination is created at the current version and records every
    # migration as applied, so rows copied from an older source would never
    # get what later migrations do to rows (migration 2 lowercases model
    # keys and drops stale duplicates). Opening the source for writing is what
    # every other havn command does, so this changes nothing a normal run
    # would not; it is refused instead when the source is newer than this
    # havn, because those tables have columns the destination cannot hold.
    try:
        src_conn = src_backend.connect(read_only=False)
    except MigrationError as e:
        console.print(f"[red]Cannot migrate: {e}[/red]")
        raise typer.Exit(code=1)
    try:
        src_status = migration_status(src_conn)
    finally:
        src_conn.close()
    if not src_status.up_to_date:
        console.print(
            f"[red]Cannot migrate: the source's metadata schema is at version "
            f"{src_status.version}, and this havn knows version {src_status.latest}. "
            f"Upgrade havn and run the migration again.[/red]"
        )
        raise typer.Exit(code=1)

    src_conn = src_backend.connect(read_only=True)
    try:
        dest_conn = dest_backend.connect(read_only=False)
    except MigrationError as e:
        src_conn.close()
        console.print(f"[red]Cannot migrate: {e}[/red]")
        raise typer.Exit(code=1)
    dest_is_duckdb = to == "duckdb"

    # Stage via Parquet to avoid materializing whole tables into Python memory.
    import tempfile
    stage_dir = Path(tempfile.mkdtemp(prefix="havn-migrate-"))

    report: list[tuple[str, str, int, int, str]] = []  # schema, table, src_rows, dest_rows, status
    try:
        for schema in _MIGRATE_SCHEMAS:
            rows = src_conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = current_database() "
                "AND table_schema = ? AND table_type = 'BASE TABLE' "
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

            managed = _metadata_tables(dest_conn) if schema == "_havn" else set()
            for (table_name,) in rows:
                if schema == "_havn" and table_name == "schema_migrations":
                    # The destination records its own migrations; copying
                    # the source's rows would only ever make it lie.
                    continue
                fq = f'"{schema}"."{table_name}"'
                pq_path = stage_dir / f"{schema}__{table_name}.parquet"
                try:
                    src_count = src_conn.execute(f"SELECT count(*) FROM {fq}").fetchone()[0]
                    # Stream table -> Parquet -> destination
                    src_conn.execute(
                        f"COPY (SELECT * FROM {fq}) TO '{pq_path.as_posix()}' (FORMAT PARQUET)"
                    )
                    if table_name in managed:
                        _fill_metadata_table(dest_conn, table_name, pq_path, dest_is_duckdb)
                    else:
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
    try:
        _rewrite_project_yml(project_dir, dest_config)
    except yamlio.YamlLoadError as e:  # changed on disk since the check above
        console.print(
            f"[red]The data was copied, but project.yml was not updated: {e}[/red]\n"
            "The source warehouse is untouched; fix project.yml and run the migration again."
        )
        raise typer.Exit(code=1)

    # Rename source
    _rename_source(project_dir, src_config)

    _print_report(report)
    console.print(f"[green]Migration complete.[/green] Project now uses {to} backend.")


def _metadata_tables(conn) -> set[str]:
    """The ``_havn`` tables the destination's migrations created."""
    rows = conn.execute(
        "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() "
        "AND schema_name = '_havn'"
    ).fetchall()
    return {r[0] for r in rows}


def _fill_metadata_table(conn, table: str, pq_path: Path, dest_is_duckdb: bool) -> None:
    """Replace the rows of a migrated ``_havn`` table with the staged ones.

    The table is filled in place rather than recreated, so the keys, NOT NULL
    constraints and defaults the migrations gave it are kept (CREATE TABLE AS
    keeps none of them). Columns are matched by name.
    """
    fq = f'"_havn"."{table}"'
    src = f"read_parquet('{pq_path.as_posix()}')"
    conn.execute(f"DELETE FROM {fq}")
    if table == "audit_log" and dest_is_duckdb:
        # DuckLake audit ids are uuids and DuckDB's are integers from
        # _havn.audit_log_seq. Integer ids (a warehouse that came from
        # DuckDB) are kept; otherwise the rows are numbered afresh by time.
        uncastable = conn.execute(
            f"SELECT count(*) FROM {src} WHERE id IS NOT NULL "
            f"AND TRY_CAST(id AS INTEGER) IS NULL"
        ).fetchall()[0][0]
        if uncastable:
            # The sequence hands out the ids, so it is already past them.
            conn.execute(
                f"INSERT INTO {fq} BY NAME SELECT * EXCLUDE (id) FROM {src} "
                f'ORDER BY "timestamp", id'
            )
        else:
            conn.execute(f"INSERT INTO {fq} BY NAME SELECT * FROM {src}")
            _advance_audit_sequence(conn)
        return
    conn.execute(f"INSERT INTO {fq} BY NAME SELECT * FROM {src}")


def _advance_audit_sequence(conn) -> None:
    """Move ``_havn.audit_log_seq`` past the highest copied id.

    DuckDB 1.x has neither ``setval`` nor ``ALTER SEQUENCE ... RESTART``, and a
    sequence a column default uses cannot be replaced, so values are drawn
    until it is past the maximum (about 30 ms per million).
    """
    top = conn.execute('SELECT max(id) FROM "_havn"."audit_log"').fetchall()[0][0]
    if top is None:
        return
    current = conn.execute("SELECT nextval('_havn.audit_log_seq')").fetchall()[0][0]
    if current < top:
        conn.execute(
            f"SELECT max(nextval('_havn.audit_log_seq')) FROM range({int(top) - int(current)})"
        ).fetchall()


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
    from havn import yamlio

    yml_path = project_dir / "project.yml"
    database = dest_config.model_dump(exclude_none=True, exclude_defaults=False)
    if dest_config.backend == "duckdb":
        for k in ("catalog", "data_path", "metadata_schema", "encrypted"):
            database.pop(k, None)
    else:
        database.pop("path", None)
    # The rest of the file (comments included) is left as written; the
    # database block becomes exactly `database`, keeping comments on the
    # keys that survive. edit_yaml writes through a temp file + os.replace.
    with yamlio.edit_yaml(yml_path) as raw:
        if isinstance(raw.get("database"), dict) and database:
            yamlio.sync_mapping(raw["database"], database)
        else:
            yamlio.set_value(raw, "database", database)


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

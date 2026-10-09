"""Persisted per-model column schemas (``_havn.model_columns``).

The catalog knows the shape of a model only while its built object is still
there, and only for models that have been built at all. The editor asks for
upstream column types on every buffer change, and the bind pass wants a
fallback for a model that is not part of the chain it just bound. Both are
served from a row written once per successful build.

The write is a ``DESCRIBE`` of the object that was just created, which costs
nothing next to the build itself. No shadow bind happens here.
"""

from __future__ import annotations

import json
import logging

import duckdb

from .models import SQLModel

logger = logging.getLogger("havn.transform")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def describe_object(
    conn: duckdb.DuckDBPyConnection, full_name: str
) -> list[tuple[str, str]]:
    """``(name, type)`` for a table or view in the catalog, or ``[]``."""
    parts = full_name.split(".")
    if len(parts) != 2:
        return []
    target = f"{_quote_ident(parts[0])}.{_quote_ident(parts[1])}"
    try:
        rows = conn.execute(f"DESCRIBE {target}").fetchall()
    except Exception as e:
        logger.debug("Could not describe %s: %s", full_name, e)
        return []
    return [(str(r[0]), str(r[1])) for r in rows]


def save_model_columns(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    columns: list[tuple[str, str]] | None = None,
) -> None:
    """Record ``model``'s column names and types after a successful build.

    ``columns`` defaults to a DESCRIBE of the built object. Never raises: a
    failure to record the schema must not turn a good build into a bad one.
    """
    try:
        if columns is None:
            columns = describe_object(conn, model.full_name)
        if not columns:
            return
        payload = json.dumps([{"name": n, "type": t} for n, t in columns])
        params = [model.full_name, model.content_hash, payload]

        from havn.engine.database import _is_ducklake_connection

        if _is_ducklake_connection(conn):
            # DuckLake drops the primary key at table creation, so
            # INSERT OR REPLACE has nothing to match on.
            conn.execute("BEGIN TRANSACTION")
            try:
                conn.execute(
                    "DELETE FROM _havn.model_columns WHERE model_path = ?",
                    [model.full_name],
                )
                conn.execute(
                    "INSERT INTO _havn.model_columns "
                    "(model_path, content_hash, columns, bound_at) "
                    "VALUES (?, ?, ?, current_timestamp)",
                    params,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        else:
            conn.execute(
                "INSERT OR REPLACE INTO _havn.model_columns "
                "(model_path, content_hash, columns, bound_at) "
                "VALUES (?, ?, ?, current_timestamp)",
                params,
            )
    except Exception as e:
        logger.debug("Could not save columns for %s: %s", model.full_name, e)


def load_model_columns(
    conn: duckdb.DuckDBPyConnection, full_name: str
) -> list[dict[str, str]]:
    """The persisted ``[{"name", "type"}]`` for a model, or ``[]``."""
    try:
        row = conn.execute(
            "SELECT columns FROM _havn.model_columns WHERE model_path = ?",
            [full_name],
        ).fetchone()
    except Exception as e:
        logger.debug("Could not read persisted columns for %s: %s", full_name, e)
        return []
    if not row or not row[0]:
        return []
    try:
        parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        {"name": str(c.get("name", "")), "type": str(c.get("type", ""))}
        for c in parsed
        if isinstance(c, dict)
    ]

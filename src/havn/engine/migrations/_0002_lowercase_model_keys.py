"""Migration 2: model names in metadata become lowercase.

Discovery used to keep a file name's case (``silver.Customers``), so metadata
recorded under it would no longer be found once names were lowercased: the
model would rebuild as new and, worse, a microbatch model would reprocess from
``begin``. Until this migration existed the same fix ran on every
``ensure_meta_table`` call and ignored its own errors.

For the keyed tables a row is renamed to its lowercase key unless a row for
that key exists already, in which case the mixed-case row is stale and is
dropped. When several mixed-case rows share a lowercase key and no lowercase
row exists (``A.b`` and ``a.B``), one of them is kept, so the rename never
collides on a primary key.
"""

from __future__ import annotations

import duckdb

KEYED_MODEL_TABLES = (
    "model_state", "model_columns", "model_profiles", "model_blocked", "batch_state",
)
HISTORY_MODEL_TABLES = (
    ("assertion_results", "model_path"),
    ("profile_history", "model_path"),
    ("source_freshness", "model_path"),
    ("anomaly_log", "model_name"),
)


def up(conn: duckdb.DuckDBPyConnection, ctx) -> None:
    for table in KEYED_MODEL_TABLES:
        if table == "batch_state":
            # Keyed by (model, window): rows for different windows are not
            # duplicates of each other, so only exact clashes are dropped.
            conn.execute(
                "DELETE FROM _havn.batch_state AS t WHERE t.model_path <> lower(t.model_path) "
                "AND EXISTS (SELECT 1 FROM _havn.batch_state AS l "
                "WHERE l.model_path = lower(t.model_path) AND l.window_start = t.window_start)"
            )
        else:
            conn.execute(
                f"DELETE FROM _havn.{table} AS t WHERE t.model_path <> lower(t.model_path) "
                f"AND EXISTS (SELECT 1 FROM _havn.{table} AS l "
                f"WHERE lower(l.model_path) = lower(t.model_path) "
                f"AND (l.model_path = lower(l.model_path) OR l.model_path > t.model_path))"
            )
        conn.execute(
            f"UPDATE _havn.{table} SET model_path = lower(model_path) "
            f"WHERE model_path <> lower(model_path)"
        )
    for table, column in HISTORY_MODEL_TABLES:
        conn.execute(
            f"UPDATE _havn.{table} SET {column} = lower({column}) "
            f"WHERE {column} <> lower({column})"
        )

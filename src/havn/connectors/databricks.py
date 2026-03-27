"""Databricks connector — syncs tables from Databricks SQL Warehouse via databricks-sql-connector."""

from __future__ import annotations

from typing import Any

from havn.engine.connector import (
    BaseConnector,
    DiscoveredResource,
    ParamSpec,
    register_connector,
    validate_identifier,
)


@register_connector
class DatabricksConnector(BaseConnector):
    name = "databricks"
    display_name = "Databricks"
    description = "Import tables from a Databricks SQL Warehouse or Unity Catalog."
    default_schedule = "0 6 * * *"  # daily at 6 AM

    params = [
        ParamSpec("host", "Databricks workspace hostname (e.g. adb-123.4.azuredatabricks.net)", example="adb-123456789.4.azuredatabricks.net"),
        ParamSpec("http_path", "SQL warehouse HTTP path (e.g. /sql/1.0/warehouses/abc123)", example="/sql/1.0/warehouses/abc123"),
        ParamSpec("access_token", "Personal access token or service principal token", secret=True),
        ParamSpec("catalog", "Unity Catalog name", required=False, default="hive_metastore", example="main"),
        ParamSpec("schema", "Schema to import from", required=False, default="default", example="default"),
        ParamSpec("cdc_column", "Column for incremental sync (e.g. updated_at)", required=False, example="updated_at"),
    ]

    def test_connection(self, config: dict[str, Any]) -> dict:
        host = config.get("host", "")
        http_path = config.get("http_path", "")
        access_token = config.get("access_token", "")

        if not host or not http_path or not access_token:
            return {"success": False, "error": "host, http_path, and access_token are required"}

        try:
            from databricks import sql as databricks_sql

            conn = databricks_sql.connect(
                server_hostname=host,
                http_path=http_path,
                access_token=access_token,
                auth_type="access_token",
            )
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            conn.close()
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "databricks-sql-connector is not installed. Run: pip install databricks-sql-connector"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def discover(self, config: dict[str, Any]) -> list[DiscoveredResource]:
        host = config.get("host", "")
        http_path = config.get("http_path", "")
        access_token = config.get("access_token", "")
        catalog = config.get("catalog", "hive_metastore")
        schema = config.get("schema", "default")

        try:
            from databricks import sql as databricks_sql

            conn = databricks_sql.connect(
                server_hostname=host,
                http_path=http_path,
                access_token=access_token,
                auth_type="access_token",
                catalog=catalog,
                schema=schema,
            )
            cursor = conn.cursor()
            cursor.execute(f"SHOW TABLES IN {catalog}.{schema}")
            rows = cursor.fetchall()
            resources = [
                DiscoveredResource(name=row[1], schema=schema, description=f"{catalog}.{schema}.{row[1]}")
                for row in rows
            ]
            cursor.close()
            conn.close()
            return resources
        except ImportError:
            return []
        except Exception:
            return []

    def generate_script(
        self,
        config: dict[str, Any],
        tables: list[str],
        target_schema: str = "landing",
    ) -> str:
        validate_identifier(target_schema, "target schema")
        for t in tables:
            validate_identifier(t, "table name")

        host = config.get("host", "")
        http_path = config.get("http_path", "")
        catalog = config.get("catalog", "hive_metastore")
        src_schema = config.get("schema", "default")
        validate_identifier(src_schema, "source schema")

        cdc_column = config.get("cdc_column", "")
        if cdc_column:
            validate_identifier(cdc_column, "cdc_column")

        # Access token comes from .env via the connector framework.
        # setup_connector passes ${CONN_NAME_ACCESS_TOKEN} so we extract the env var name.
        token_env = config.get("access_token", "")
        if isinstance(token_env, str) and token_env.startswith("${") and token_env.endswith("}"):
            env_var = token_env[2:-1]
        else:
            env_var = "DATABRICKS_ACCESS_TOKEN"
        token_line = f'access_token = os.environ.get("{env_var}", "")'

        table_list = ", ".join(f'"{t}"' for t in tables)

        if cdc_column:
            sync_block = _incremental_sync_block(target_schema, catalog, src_schema, cdc_column)
        else:
            sync_block = _full_refresh_sync_block(target_schema, catalog, src_schema)

        return f'''\
"""Auto-generated Databricks ingest script.

Syncs tables from {catalog}.{src_schema} into {target_schema}.* via databricks-sql-connector.
Data is fetched as Arrow batches and loaded efficiently into DuckDB.
"""

import os
import time

try:
    from databricks import sql as databricks_sql
except ImportError:
    raise ImportError(
        "databricks-sql-connector is required for this connector. "
        "Install it with: pip install databricks-sql-connector"
    )

{token_line}

server_hostname = "{host}"
http_path = "{http_path}"
catalog = "{catalog}"
src_schema = "{src_schema}"


def _connect_with_retry(max_retries=3):
    """Connect to Databricks with retry and exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            conn = databricks_sql.connect(
                server_hostname=server_hostname,
                http_path=http_path,
                access_token=access_token,
                auth_type="access_token",
                catalog=catalog,
                schema=src_schema,
            )
            return conn
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            print(f"  Connection failed ({{e}}), retrying in {{wait}}s... ({{attempt + 1}}/{{max_retries}})")
            time.sleep(wait)


def _fetch_table_as_arrow(dbx_conn, full_table_name, where_clause=""):
    """Fetch a table from Databricks as a PyArrow table."""
    query = f"SELECT * FROM {{full_table_name}}"
    if where_clause:
        query += f" WHERE {{where_clause}}"
    cursor = dbx_conn.cursor()
    cursor.execute(query)
    arrow_table = cursor.fetchall_arrow()
    cursor.close()
    return arrow_table


dbx = _connect_with_retry()
db.execute("CREATE SCHEMA IF NOT EXISTS {target_schema}")

tables = [{table_list}]

{sync_block}

dbx.close()

if errors:
    print(f"\\nCompleted with {{len(errors)}} error(s):")
    for table, err in errors:
        print(f"  {{table}}: {{err}}")
    raise RuntimeError(f"{{len(errors)}} table(s) failed to sync")
else:
    print(f"Loaded {{total_rows}} rows total from Databricks ({{len(tables)}} tables)")
'''


def _full_refresh_sync_block(
    target_schema: str,
    catalog: str,
    src_schema: str,
) -> str:
    return f'''\
total_rows = 0
errors = []
for table in tables:
    src = f"{catalog}.{src_schema}.{{table}}"
    dest = f"{target_schema}.{{table}}"
    try:
        arrow_table = _fetch_table_as_arrow(dbx, src)
        db.execute(f"CREATE OR REPLACE TABLE {{dest}} AS SELECT * FROM arrow_table")
        rows = db.execute(f"SELECT COUNT(*) FROM {{dest}}").fetchone()[0]
        total_rows += rows
        print(f"Loaded {{rows}} rows into {{dest}}")
    except Exception as e:
        print(f"  ERROR syncing {{table}}: {{e}}")
        errors.append((table, str(e)))'''


def _incremental_sync_block(
    target_schema: str,
    catalog: str,
    src_schema: str,
    cdc_column: str,
) -> str:
    return f'''\
# Incremental sync via high-watermark on "{cdc_column}"
from havn.engine.cdc import ensure_cdc_table, get_watermark, update_watermark
ensure_cdc_table(db)

CONNECTOR_NAME = "databricks_sync"

total_rows = 0
errors = []
for table in tables:
    src = f"{catalog}.{src_schema}.{{table}}"
    dest = f"{target_schema}.{{table}}"
    full_name = f"{target_schema}.{{table}}"
    try:
        watermark = get_watermark(db, CONNECTOR_NAME, full_name)

        if watermark:
            safe_wm = watermark.replace("'", "''")
            where_clause = f"`{cdc_column}` > '{{safe_wm}}'"
            arrow_table = _fetch_table_as_arrow(dbx, src, where_clause)

            exists = db.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = '{target_schema}' AND table_name = '" + table + "'"
            ).fetchone()[0] > 0

            if exists:
                db.execute(f"INSERT INTO {{dest}} SELECT * FROM arrow_table")
            else:
                db.execute(f"CREATE TABLE {{dest}} AS SELECT * FROM arrow_table")
        else:
            arrow_table = _fetch_table_as_arrow(dbx, src)
            db.execute(f"CREATE OR REPLACE TABLE {{dest}} AS SELECT * FROM arrow_table")

        rows = db.execute(f"SELECT COUNT(*) FROM {{dest}}").fetchone()[0]
        total_rows += rows

        try:
            new_wm = db.execute(
                f"SELECT MAX(\\"{cdc_column}\\")::VARCHAR FROM {{dest}}"
            ).fetchone()
            if new_wm and new_wm[0]:
                update_watermark(db, CONNECTOR_NAME, full_name, "high_watermark", new_wm[0], rows_synced=rows)
        except Exception as wm_err:
            print(f"  Warning: could not update watermark for {{table}}: {{wm_err}}")

        suffix = " (incremental)" if watermark else " (full)"
        print(f"Loaded {{rows}} rows into {{dest}}{{suffix}}")
    except Exception as e:
        print(f"  ERROR syncing {{table}}: {{e}}")
        errors.append((table, str(e)))'''

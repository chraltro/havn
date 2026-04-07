"""Snowflake connector -- syncs tables via snowflake-connector-python with Arrow transfer."""

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
class SnowflakeConnector(BaseConnector):
    name = "snowflake"
    display_name = "Snowflake"
    description = "Import tables from a Snowflake data warehouse."
    default_schedule = "0 6 * * *"

    params = [
        ParamSpec("account", "Snowflake account identifier (e.g. xy12345.eu-west-1)", example="xy12345.eu-west-1"),
        ParamSpec("user", "Username", example="ETL_USER"),
        ParamSpec("password", "Password", secret=True),
        ParamSpec("warehouse", "Compute warehouse name", example="COMPUTE_WH"),
        ParamSpec("database", "Database name", example="ANALYTICS"),
        ParamSpec("schema", "Schema name", required=False, default="PUBLIC", example="PUBLIC"),
        ParamSpec("role", "Role to use", required=False, example="ANALYST"),
        ParamSpec("cdc_column", "Column for incremental sync (e.g. UPDATED_AT)", required=False, example="UPDATED_AT"),
    ]

    def test_connection(self, config: dict[str, Any]) -> dict:
        account = config.get("account", "")
        user = config.get("user", "")
        password = config.get("password", "")
        warehouse = config.get("warehouse", "")
        database = config.get("database", "")

        if not account or not user or not password:
            return {"success": False, "error": "account, user, and password are required"}

        try:
            import snowflake.connector

            conn = snowflake.connector.connect(
                account=account,
                user=user,
                password=password,
                warehouse=warehouse or None,
                database=database or None,
            )
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            conn.close()
            return {"success": True}
        except ImportError:
            return {
                "success": False,
                "error": "snowflake-connector-python is not installed. Run: pip install snowflake-connector-python",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def discover(self, config: dict[str, Any]) -> list[DiscoveredResource]:
        account = config.get("account", "")
        user = config.get("user", "")
        password = config.get("password", "")
        warehouse = config.get("warehouse", "")
        database = config.get("database", "")
        schema = config.get("schema", "PUBLIC")

        try:
            import snowflake.connector

            conn = snowflake.connector.connect(
                account=account,
                user=user,
                password=password,
                warehouse=warehouse or None,
                database=database or None,
                schema=schema,
            )
            cursor = conn.cursor()
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_TYPE "
                "FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = %s "
                "ORDER BY TABLE_NAME",
                (schema.upper(),),
            )
            rows = cursor.fetchall()
            resources = [
                DiscoveredResource(
                    name=r[0],
                    schema=schema,
                    description=f"{database}.{schema}.{r[0]} ({r[1]})",
                )
                for r in rows
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

        account = config.get("account", "")
        warehouse = config.get("warehouse", "")
        database = config.get("database", "")
        src_schema = config.get("schema", "PUBLIC")
        validate_identifier(src_schema, "source schema")
        role = config.get("role", "")

        cdc_column = config.get("cdc_column", "")
        if cdc_column:
            validate_identifier(cdc_column, "cdc_column")

        # Resolve secret env vars
        user_env = config.get("user", "")
        if isinstance(user_env, str) and user_env.startswith("${") and user_env.endswith("}"):
            user_line = f'user = os.environ.get("{user_env[2:-1]}", "")'
        else:
            user_line = f'user = "{user_env}"'

        password_env = config.get("password", "")
        if isinstance(password_env, str) and password_env.startswith("${") and password_env.endswith("}"):
            password_line = f'password = os.environ.get("{password_env[2:-1]}", "")'
        else:
            password_line = f'password = os.environ.get("SNOWFLAKE_PASSWORD", "")'

        table_list = ", ".join(f'"{t}"' for t in tables)

        role_line = f'    role="{role}",' if role else ""

        if cdc_column:
            sync_block = _incremental_sync_block(target_schema, database, src_schema, cdc_column)
        else:
            sync_block = _full_refresh_sync_block(target_schema, database, src_schema)

        return f'''\
"""Auto-generated Snowflake ingest script.

Syncs tables from {database}.{src_schema} into {target_schema}.* via snowflake-connector-python.
Data is fetched as Arrow batches for efficient columnar transfer to DuckDB.
"""

import os
import time

try:
    import snowflake.connector
except ImportError:
    raise ImportError(
        "snowflake-connector-python is required for this connector. "
        "Install it with: pip install snowflake-connector-python"
    )

{user_line}
{password_line}

account = "{account}"
warehouse = "{warehouse}"
database = "{database}"
src_schema = "{src_schema}"


def _connect_with_retry(max_retries=3):
    """Connect to Snowflake with retry and exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            conn = snowflake.connector.connect(
                account=account,
                user=user,
                password=password,
                warehouse=warehouse or None,
                database=database or None,
                schema=src_schema,
{role_line}
            )
            return conn
        except Exception as e:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            print(f"  Connection failed ({{e}}), retrying in {{wait}}s... ({{attempt + 1}}/{{max_retries}})")
            time.sleep(wait)


def _fetch_table_as_arrow(sf_conn, table_name, where_clause=""):
    """Fetch a table from Snowflake as a PyArrow table."""
    query = f"SELECT * FROM {{table_name}}"
    if where_clause:
        query += f" WHERE {{where_clause}}"
    cursor = sf_conn.cursor()
    cursor.execute(query)
    arrow_table = cursor.fetch_arrow_all()
    cursor.close()
    return arrow_table


sf = _connect_with_retry()
db.execute("CREATE SCHEMA IF NOT EXISTS {target_schema}")

tables = [{table_list}]

{sync_block}

sf.close()

if errors:
    print(f"\\nCompleted with {{len(errors)}} error(s):")
    for table, err in errors:
        print(f"  {{table}}: {{err}}")
    raise RuntimeError(f"{{len(errors)}} table(s) failed to sync")
else:
    print(f"Loaded {{total_rows}} rows total from Snowflake ({{len(tables)}} tables)")
'''


def _full_refresh_sync_block(
    target_schema: str,
    database: str,
    src_schema: str,
) -> str:
    return f'''\
total_rows = 0
errors = []
for table in tables:
    src = f"{database}.{src_schema}.{{table}}"
    dest = f"{target_schema}.{{table}}"
    try:
        arrow_table = _fetch_table_as_arrow(sf, src)
        if arrow_table is not None and len(arrow_table) > 0:
            db.execute(f"CREATE OR REPLACE TABLE {{dest}} AS SELECT * FROM arrow_table")
            rows = db.execute(f"SELECT COUNT(*) FROM {{dest}}").fetchone()[0]
        else:
            print(f"  No data returned for {{table}}, skipping")
            continue
        total_rows += rows
        print(f"Loaded {{rows}} rows into {{dest}}")
    except Exception as e:
        print(f"  ERROR syncing {{table}}: {{e}}")
        errors.append((table, str(e)))'''


def _incremental_sync_block(
    target_schema: str,
    database: str,
    src_schema: str,
    cdc_column: str,
) -> str:
    return f'''\
# Incremental sync via high-watermark on "{cdc_column}"
from havn.engine.cdc import ensure_cdc_table, get_watermark, update_watermark
ensure_cdc_table(db)

CONNECTOR_NAME = "snowflake_sync"

total_rows = 0
errors = []
for table in tables:
    src = f"{database}.{src_schema}.{{table}}"
    dest = f"{target_schema}.{{table}}"
    full_name = f"{target_schema}.{{table}}"
    try:
        watermark = get_watermark(db, CONNECTOR_NAME, full_name)

        if watermark:
            safe_wm = watermark.replace("'", "''")
            where_clause = f'"{cdc_column}" > \\'{{safe_wm}}\\''
            arrow_table = _fetch_table_as_arrow(sf, src, where_clause)

            exists = db.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = '{target_schema}' AND table_name = '" + table + "'"
            ).fetchone()[0] > 0

            if exists and arrow_table is not None and len(arrow_table) > 0:
                db.execute(f"INSERT INTO {{dest}} SELECT * FROM arrow_table")
            elif not exists and arrow_table is not None and len(arrow_table) > 0:
                db.execute(f"CREATE TABLE {{dest}} AS SELECT * FROM arrow_table")
            else:
                print(f"  No new rows for {{table}}")
                continue
        else:
            arrow_table = _fetch_table_as_arrow(sf, src)
            if arrow_table is not None and len(arrow_table) > 0:
                db.execute(f"CREATE OR REPLACE TABLE {{dest}} AS SELECT * FROM arrow_table")
            else:
                db.execute(f"CREATE OR REPLACE TABLE {{dest}} AS SELECT * FROM (SELECT) WHERE false")

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

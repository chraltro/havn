"""Google BigQuery connector -- syncs tables via google-cloud-bigquery with Arrow transfer."""

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
class BigQueryConnector(BaseConnector):
    name = "bigquery"
    display_name = "Google BigQuery"
    description = "Import tables from Google BigQuery."
    default_schedule = "0 6 * * *"

    params = [
        ParamSpec("project", "GCP project ID", example="my-project-123"),
        ParamSpec("dataset", "BigQuery dataset name", example="analytics"),
        ParamSpec("credentials_json", "Service account JSON key (base64-encoded)", secret=True),
        ParamSpec("location", "Dataset location", required=False, default="US", example="EU"),
        ParamSpec("cdc_column", "Column for incremental sync (e.g. updated_at)", required=False, example="updated_at"),
    ]

    def _get_client(self, config: dict[str, Any]) -> Any:
        """Build a BigQuery client from config. Raises ImportError if SDK missing."""
        import base64
        import json

        from google.cloud import bigquery
        from google.oauth2 import service_account

        creds_b64 = config.get("credentials_json", "")
        if not creds_b64:
            raise ValueError("credentials_json is required")

        try:
            creds_json = json.loads(base64.b64decode(creds_b64))
        except Exception:
            # Try as raw JSON (not base64)
            try:
                creds_json = json.loads(creds_b64)
            except Exception:
                raise ValueError(
                    "credentials_json must be a base64-encoded or raw JSON service account key"
                )

        credentials = service_account.Credentials.from_service_account_info(creds_json)
        project = config.get("project", creds_json.get("project_id", ""))
        location = config.get("location", "US")

        return bigquery.Client(
            project=project,
            credentials=credentials,
            location=location,
        )

    def test_connection(self, config: dict[str, Any]) -> dict:
        project = config.get("project", "")
        if not project:
            return {"success": False, "error": "project is required"}

        try:
            client = self._get_client(config)
            query_job = client.query("SELECT 1")
            list(query_job.result())
            client.close()
            return {"success": True}
        except ImportError:
            return {
                "success": False,
                "error": (
                    "google-cloud-bigquery is not installed. Run: "
                    "pip install google-cloud-bigquery google-auth"
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def discover(self, config: dict[str, Any]) -> list[DiscoveredResource]:
        dataset = config.get("dataset", "")
        if not dataset:
            return []

        try:
            client = self._get_client(config)
            project = config.get("project", "")
            dataset_ref = f"{project}.{dataset}"

            tables = list(client.list_tables(dataset_ref))
            resources = [
                DiscoveredResource(
                    name=t.table_id,
                    schema=dataset,
                    description=f"{dataset_ref}.{t.table_id} ({t.table_type})",
                )
                for t in tables
            ]
            client.close()
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

        project = config.get("project", "")
        dataset = config.get("dataset", "")
        validate_identifier(dataset, "dataset")
        location = config.get("location", "US")

        cdc_column = config.get("cdc_column", "")
        if cdc_column:
            validate_identifier(cdc_column, "cdc_column")

        # Credentials env var
        creds_env = config.get("credentials_json", "")
        if isinstance(creds_env, str) and creds_env.startswith("${") and creds_env.endswith("}"):
            env_var = creds_env[2:-1]
        else:
            env_var = "BIGQUERY_CREDENTIALS_JSON"
        creds_line = f'credentials_b64 = os.environ.get("{env_var}", "")'

        table_list = ", ".join(f'"{t}"' for t in tables)

        if cdc_column:
            sync_block = _incremental_sync_block(target_schema, project, dataset, cdc_column)
        else:
            sync_block = _full_refresh_sync_block(target_schema, project, dataset)

        return f'''\
"""Auto-generated BigQuery ingest script.

Syncs tables from {project}.{dataset} into {target_schema}.* via google-cloud-bigquery.
Data is fetched as Arrow tables for efficient columnar transfer to DuckDB.
"""

import base64
import json
import os

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except ImportError:
    raise ImportError(
        "google-cloud-bigquery is required for this connector. "
        "Install it with: pip install google-cloud-bigquery google-auth"
    )

{creds_line}

project = "{project}"
dataset = "{dataset}"
location = "{location}"


def _get_client():
    """Build BigQuery client from credentials."""
    if not credentials_b64:
        raise ValueError("BigQuery credentials not found in environment")
    try:
        creds_json = json.loads(base64.b64decode(credentials_b64))
    except Exception:
        creds_json = json.loads(credentials_b64)
    credentials = service_account.Credentials.from_service_account_info(creds_json)
    return bigquery.Client(project=project, credentials=credentials, location=location)


def _fetch_table_as_arrow(client, table_ref, where_clause=""):
    """Fetch a BigQuery table as a PyArrow table."""
    query = f"SELECT * FROM `{{table_ref}}`"
    if where_clause:
        query += f" WHERE {{where_clause}}"
    return client.query(query).to_arrow()


bq = _get_client()
db.execute("CREATE SCHEMA IF NOT EXISTS {target_schema}")

tables = [{table_list}]

{sync_block}

bq.close()

if errors:
    print(f"\\nCompleted with {{len(errors)}} error(s):")
    for table, err in errors:
        print(f"  {{table}}: {{err}}")
    raise RuntimeError(f"{{len(errors)}} table(s) failed to sync")
else:
    print(f"Loaded {{total_rows}} rows total from BigQuery ({{len(tables)}} tables)")
'''


def _full_refresh_sync_block(
    target_schema: str,
    project: str,
    dataset: str,
) -> str:
    return f'''\
total_rows = 0
errors = []
for table in tables:
    src = f"{project}.{dataset}.{{table}}"
    dest = f"{target_schema}.{{table}}"
    try:
        arrow_table = _fetch_table_as_arrow(bq, src)
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
    project: str,
    dataset: str,
    cdc_column: str,
) -> str:
    return f'''\
# Incremental sync via high-watermark on "{cdc_column}"
from havn.engine.cdc import ensure_cdc_table, get_watermark, update_watermark
ensure_cdc_table(db)

CONNECTOR_NAME = "bigquery_sync"

total_rows = 0
errors = []
for table in tables:
    src = f"{project}.{dataset}.{{table}}"
    dest = f"{target_schema}.{{table}}"
    full_name = f"{target_schema}.{{table}}"
    try:
        watermark = get_watermark(db, CONNECTOR_NAME, full_name)

        if watermark:
            safe_wm = watermark.replace("'", "''")
            where_clause = f"`{cdc_column}` > '{{safe_wm}}'"
            arrow_table = _fetch_table_as_arrow(bq, src, where_clause)

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
            arrow_table = _fetch_table_as_arrow(bq, src)
            if arrow_table is not None and len(arrow_table) > 0:
                db.execute(f"CREATE OR REPLACE TABLE {{dest}} AS SELECT * FROM arrow_table")
            else:
                print(f"  No data returned for {{table}}, skipping")
                continue

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

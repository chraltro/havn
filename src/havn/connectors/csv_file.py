"""CSV/file connector — imports data from local files or URLs."""

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
class CSVConnector(BaseConnector):
    name = "csv"
    display_name = "CSV / File Upload"
    description = "Import data from CSV, Parquet, or JSON files (local path or URL)."
    default_schedule = None  # typically one-shot

    params = [
        ParamSpec("path", "File path or URL (supports CSV, Parquet, JSON)", param_type="path", example="/data/customers.csv"),
        ParamSpec("format", "File format: csv, parquet, json (auto-detected if omitted)", required=False, param_type="enum", enum_values=["csv", "parquet", "json"], example="csv"),
        ParamSpec("table_name", "Target table name", required=False, example="customers"),
    ]

    def test_connection(self, config: dict[str, Any]) -> dict:
        path = config.get("path", "")
        if not path:
            return {"success": False, "error": "path is required"}

        # URL test
        if path.startswith("http://") or path.startswith("https://"):
            from urllib.request import urlopen
            try:
                with urlopen(path, timeout=15) as resp:
                    if resp.status < 400:
                        return {"success": True}
                    return {"success": False, "error": f"HTTP {resp.status}"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        # Local file test
        from pathlib import Path
        if Path(path).exists():
            return {"success": True}
        return {"success": False, "error": f"File not found: {path}"}

    def discover(self, config: dict[str, Any]) -> list[DiscoveredResource]:
        from pathlib import Path

        path = config.get("path", "")
        table_name = config.get("table_name")
        if not table_name:
            if path.startswith("http"):
                table_name = path.split("/")[-1].split("?")[0].split(".")[0]
            else:
                table_name = Path(path).stem
            table_name = table_name.replace("-", "_").replace(" ", "_").lower()
        return [DiscoveredResource(name=table_name, description=path)]

    _FORMATS = ("csv", "parquet", "json")

    def _detect_format(self, config: dict[str, Any]) -> str:
        # Saved connections may hold "CSV" or " parquet"; normalise first so
        # `connectors regenerate` keeps working for them.
        fmt = str(config.get("format") or "").strip().lower()
        if fmt:
            # The format lands in generated code (reader name, temp-file
            # suffix), so only the known values are accepted.
            if fmt not in self._FORMATS:
                raise ValueError(
                    f"Unknown format {fmt!r}; expected one of: {', '.join(self._FORMATS)}"
                )
            return fmt
        lower = config.get("path", "").lower()
        if lower.endswith(".parquet") or lower.endswith(".pq"):
            return "parquet"
        if lower.endswith(".json") or lower.endswith(".jsonl") or lower.endswith(".ndjson"):
            return "json"
        return "csv"

    def _reader_call(self, fmt: str, path_var: str) -> str:
        # path_var names a variable in the generated script that already holds
        # the path with single quotes doubled for a SQL string literal.
        if fmt == "parquet":
            return f"read_parquet('{{{path_var}}}')"
        if fmt == "json":
            return f"read_json('{{{path_var}}}', auto_detect=true)"
        return f"read_csv('{{{path_var}}}', auto_detect=true)"

    def generate_script(
        self,
        config: dict[str, Any],
        tables: list[str],
        target_schema: str = "landing",
    ) -> str:
        validate_identifier(target_schema, "target schema")
        for t in tables:
            validate_identifier(t, "table name")

        path = config.get("path", "")
        fmt = self._detect_format(config)
        table_name = tables[0] if tables else "data"
        is_url = path.startswith("http://") or path.startswith("https://")

        # The path is user input embedded in two languages. repr() gives a
        # valid Python literal for any string (quotes, backslashes, newlines);
        # the script then doubles single quotes at runtime for the SQL literal.
        # Hand-escaping only backslashes broke on O'Brien/data.csv.
        path_literal = repr(path)
        sql_escape = """.replace("'", "''")"""

        if is_url:
            reader = self._reader_call(fmt, "sql_path")
            lines = [
                '"""Auto-generated CSV/file ingest script.',
                "",
                f"Imports data from the URL below into {target_schema}.{table_name}.",
                '"""',
                "",
                "import os",
                "import tempfile",
                "from urllib.request import urlopen",
                "",
                f"url = {path_literal}",
                "",
                'print(f"Downloading {url}...")',
                "with urlopen(url, timeout=60) as resp:",
                "    data = resp.read()",
                "",
                f'with tempfile.NamedTemporaryFile(mode="wb", suffix=".{fmt}", delete=False) as f:',
                "    f.write(data)",
                "    tmp_path = f.name",
                # The temp dir sits under the user's profile, which can hold a quote too.
                f"sql_path = tmp_path{sql_escape}",
                "",
                f'db.execute("CREATE SCHEMA IF NOT EXISTS {target_schema}")',
                'db.execute(f"""',
                f"    CREATE OR REPLACE TABLE {target_schema}.{table_name} AS",
                f"    SELECT * FROM {reader}",
                '""")',
                "",
                "os.unlink(tmp_path)",
                f'rows = db.execute("SELECT COUNT(*) FROM {target_schema}.{table_name}").fetchone()[0]',
                f'print(f"Loaded {{rows}} rows into {target_schema}.{table_name}")',
                "",
            ]
            return "\n".join(lines)
        else:
            reader = self._reader_call(fmt, "sql_path")
            lines = [
                '"""Auto-generated CSV/file ingest script.',
                "",
                f"Imports data from the file below into {target_schema}.{table_name}.",
                '"""',
                "",
                f"file_path = {path_literal}",
                f"sql_path = file_path{sql_escape}",
                "",
                'print(f"Reading {file_path}...")',
                f'db.execute("CREATE SCHEMA IF NOT EXISTS {target_schema}")',
                'db.execute(f"""',
                f"    CREATE OR REPLACE TABLE {target_schema}.{table_name} AS",
                f"    SELECT * FROM {reader}",
                '""")',
                "",
                f'rows = db.execute("SELECT COUNT(*) FROM {target_schema}.{table_name}").fetchone()[0]',
                f'print(f"Loaded {{rows}} rows into {target_schema}.{table_name}")',
                "",
            ]
            return "\n".join(lines)

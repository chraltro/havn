"""Read-only SQL safety validation, shared by every ad-hoc query surface.

The web API (``/api/query``), dashboards, collaboration cells, the semantic
layer, and the MCP server all accept user/agent-supplied SQL that must stay
read-only. This module is the single validation point: it strips strings and
comments, splits top-level statements, and rejects mutations, multi-statement
batches, and file/network-access functions.

Raises :class:`ReadOnlyQueryError` (a ``ValueError``) so callers outside
FastAPI don't need HTTP machinery; the server routes convert it to an
``HTTPException`` with the carried ``status_code``.
"""

from __future__ import annotations

import re

_FORBIDDEN_STATEMENT_KEYWORDS = frozenset({
    "insert", "update", "delete", "drop", "create", "alter", "truncate", "merge",
    "copy", "attach", "detach", "install", "load", "export", "import",
    "grant", "revoke", "set", "reset", "vacuum", "checkpoint", "pragma",
    "call", "execute",
    # DuckDB accepts a FORCE prefix on INSTALL/CHECKPOINT, which put the verb in
    # second position where the leading-keyword check can't see it.
    "force",
    # Transaction and prepared-statement control. Harmless on their own, but they
    # let a caller hold a transaction open or stage a statement for later
    # execution, neither of which a read-only query surface should permit.
    "begin", "start", "commit", "rollback", "abort", "prepare", "deallocate",
    # State mutations that aren't DML: USE switches the active catalog/schema,
    # COMMENT ON writes catalog metadata, ANALYZE rewrites statistics.
    "use", "comment", "analyze",
})

_DANGEROUS_FUNCTION_NAMES = frozenset({
    "read_csv_auto", "read_csv", "read_parquet", "read_json_auto", "read_json",
    "read_json_objects", "read_json_objects_auto", "read_ndjson",
    "read_ndjson_auto", "read_ndjson_objects",
    "read_blob", "read_text", "read_xlsx",
    "write_csv", "write_parquet",
    "iceberg_scan", "iceberg_metadata", "iceberg_snapshots",
    "delta_scan", "parquet_scan", "csv_scan",
    "http_get", "http_post",
    # Re-entrant SQL execution. json_serialize_sql turns a string into a plan and
    # json_execute_serialized_sql runs it, so anything nested in a string literal
    # bypasses this validator entirely (string literals are stripped before the
    # scans below ever run).
    "json_serialize_sql", "json_execute_serialized_sql", "query", "query_table",
    # Filesystem enumeration and metadata readers. These don't return file
    # *contents* wholesale, but glob() lists the disk and sniff_csv() /
    # parquet_schema() leak header rows and column names from arbitrary paths.
    "glob", "sniff_csv",
    "parquet_metadata", "parquet_schema", "parquet_file_metadata",
    "parquet_kv_metadata", "parquet_bloom_probe",
    "duckdb_external_file_cache", "duckdb_temporary_files",
    # Whole-database and foreign-database attach/scan paths.
    "read_duckdb", "sqlite_scan", "sqlite_attach",
    "postgres_scan", "postgres_scan_pushdown", "postgres_query",
    "mysql_scan", "mysql_query",
    # In-process pointer scans and credential helpers.
    "arrow_scan", "arrow_scan_dumb", "load_aws_credentials",
    # Spatial extension file readers.
    "st_read", "st_read_meta", "st_readosm", "shapefile_meta",
})


class ReadOnlyQueryError(ValueError):
    """SQL was rejected by the read-only validator.

    ``status_code`` carries the HTTP status the server routes should use
    (400 for malformed input, 403 for forbidden operations).
    """

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


def strip_sql_comments_and_strings(sql: str) -> str:
    """Remove string literals and comments so keyword/function scans cannot
    be fooled by content inside quotes or comments."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nx = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nx == "-":
            j = sql.find("\n", i)
            if j < 0:
                break
            i = j + 1
            continue
        if c == "/" and nx == "*":
            j = sql.find("*/", i + 2)
            if j < 0:
                break
            i = j + 2
            continue
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'" and (i + 1 < n and sql[i + 1] == "'"):
                    i += 2
                    continue
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            if j < 0:
                break
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_FUNCTION_CALL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(')
_QUOTED_FUNCTION_CALL_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*\(')
# A string literal (already normalised to '') in table position — i.e. right
# after FROM or JOIN, optionally wrapped in one layer of parentheses. This is
# DuckDB's file replacement-scan syntax.
_REPLACEMENT_SCAN_RE = re.compile(r"\b(?:from|join)\s*\(?\s*''", re.IGNORECASE)
# DuckDB also resolves a DOUBLE-quoted path in table position as a replacement
# scan (FROM "/etc/passwd" reads the file just like FROM '/etc/passwd'). Double
# quotes are not stripped above because they normally delimit identifiers, so
# match only file-shaped ones. Three shapes count:
#   - a URL scheme            FROM "https://host/x.csv"
#   - any slash or backslash  FROM "../secrets/.env"
#   - a bare filename ending in a data-file extension, which resolves relative
#     to the server's working directory  ->  FROM "warehouse.duckdb"
# Plain quoted identifiers (FROM "my table", FROM "gold"."orders") stay legal.
_DATA_FILE_EXT = (
    r"csv|tsv|txt|parquet|json|jsonl|ndjson|duckdb|ddb|db|sqlite|sqlite3"
    r"|xlsx|arrow|feather|avro|orc|env"
)
_DQUOTE_PATH_SCAN_RE = re.compile(
    r'\b(?:from|join)\s*\(?\s*"(?:'
    r"[a-z][a-z0-9+.-]*://"          # URL scheme
    r'|[^"]*[/\\]'                    # any path separator
    rf'|[^"]*\.(?:{_DATA_FILE_EXT})(?:\.(?:gz|zst|bz2|br))?'  # bare data file
    r')[^"]*"',
    re.IGNORECASE,
)


def split_statements(sql: str) -> list[str]:
    """Split top-level SQL statements on ;, respecting strings/comments
    (the input here is already stripped of those)."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for c in sql:
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        if c == ";" and depth == 0:
            text = "".join(buf).strip()
            if text:
                parts.append(text)
            buf = []
            continue
        buf.append(c)
    text = "".join(buf).strip()
    if text:
        parts.append(text)
    return parts


def leading_statement_keyword(stmt: str) -> str:
    """Return the lowercased leading keyword of a statement.

    For statements starting with WITH, walks the CTE definitions
    ``name [AS] (...)``, possibly comma-separated and possibly leading
    with ``RECURSIVE``, then returns the first keyword after the last
    CTE body (the real statement verb: SELECT / DELETE / INSERT / ...).
    """
    s = stmt.lstrip()
    m = _IDENT_RE.match(s)
    if not m:
        return ""
    head = m.group(0).lower()
    if head != "with":
        return head
    i = m.end()
    n = len(s)

    def _skip_ws(j: int) -> int:
        while j < n and s[j].isspace():
            j += 1
        return j

    def _skip_parens(j: int) -> int:
        if j >= n or s[j] != "(":
            return j
        depth = 1
        j += 1
        while j < n and depth > 0:
            if s[j] == "(":
                depth += 1
            elif s[j] == ")":
                depth -= 1
            j += 1
        return j

    i = _skip_ws(i)
    if i < n:
        rec = _IDENT_RE.match(s, i)
        if rec and rec.group(0).lower() == "recursive":
            i = rec.end()
            i = _skip_ws(i)

    while True:
        m2 = _IDENT_RE.match(s, i)
        if not m2:
            return "with"
        i = m2.end()
        i = _skip_ws(i)
        if i < n:
            opt_as = _IDENT_RE.match(s, i)
            if opt_as and opt_as.group(0).lower() == "as":
                i = opt_as.end()
                i = _skip_ws(i)
        if i >= n or s[i] != "(":
            return m2.group(0).lower()
        i = _skip_parens(i)
        i = _skip_ws(i)
        if i < n and s[i] == ",":
            i += 1
            i = _skip_ws(i)
            continue
        next_m = _IDENT_RE.match(s, i)
        if next_m:
            return next_m.group(0).lower()
        return "with"


def validate_read_only_query(sql: str) -> None:
    """Reject SQL that is not a safe read-only query.

    Strips strings and comments, splits top-level statements, then rejects:
      - multi-statement queries
      - any statement whose leading keyword (after a CTE) is a mutation verb
      - calls to file-access functions (read_csv, read_parquet, http_*)

    Unknown leading keywords are passed through to DuckDB, which will return
    a parse error so the caller gets a 400 (not a misleading 403).
    """
    cleaned = strip_sql_comments_and_strings(sql)
    statements = split_statements(cleaned)
    if not statements:
        raise ReadOnlyQueryError("Empty query.", status_code=400)
    if len(statements) > 1:
        raise ReadOnlyQueryError("Multi-statement queries are not allowed.")
    stmt = statements[0]
    head = leading_statement_keyword(stmt)
    if head in _FORBIDDEN_STATEMENT_KEYWORDS:
        raise ReadOnlyQueryError(
            "Only SELECT queries are allowed through the query interface."
        )
    # Note: at this point string literals are already stripped to '', so we
    # can scan the cleaned statement directly. We also have to catch quoted
    # identifiers: DuckDB happily accepts `"read_csv"(...)` and treats the
    # quoted identifier as the same builtin.
    for fname in _FUNCTION_CALL_RE.findall(stmt):
        if fname.lower() in _DANGEROUS_FUNCTION_NAMES:
            raise ReadOnlyQueryError(
                "File-access functions (read_csv, read_parquet, etc.) are not allowed.",
            )
    for fname in _QUOTED_FUNCTION_CALL_RE.findall(stmt):
        if fname.lower() in _DANGEROUS_FUNCTION_NAMES:
            raise ReadOnlyQueryError(
                "File-access functions (read_csv, read_parquet, etc.) are not allowed.",
            )
    if re.search(r'\bhttpfs_', stmt, re.IGNORECASE):
        raise ReadOnlyQueryError("HTTPFS access is not allowed through the query interface.")
    # DuckDB reads local/remote files via a "replacement scan" on a bare string
    # path — ``SELECT * FROM '/etc/passwd'`` or ``FROM 'https://…/x.csv'`` — with
    # no function call to catch above. String literals are already stripped to
    # ``''`` here, and a string literal in table position (directly after FROM or
    # JOIN) is only ever a replacement scan, never valid otherwise. Reject it so
    # the read-only surfaces can't be used to exfiltrate server files or SSRF.
    if _REPLACEMENT_SCAN_RE.search(stmt) or _DQUOTE_PATH_SCAN_RE.search(stmt):
        raise ReadOnlyQueryError(
            "Reading files by path (FROM '<path>') is not allowed through the query interface.",
        )

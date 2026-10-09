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
import threading

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
    # Process environment and stored credentials. The server loads .env into
    # its environment, so getenv() would hand out every project secret.
    "getenv", "duckdb_secrets", "which_secret",
})


class ReadOnlyQueryError(ValueError):
    """SQL was rejected by the read-only validator.

    ``status_code`` carries the HTTP status the server routes should use
    (400 for malformed input, 403 for forbidden operations).
    """

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


# Characters that continue an identifier in DuckDB's (Postgres-derived) lexer.
# An E prefix or a $tag$ opener only starts a literal when it is not glued to
# a preceding identifier: ``nameE'x'`` and ``x$$`` are identifiers.
_IDENT_CONT_RE = re.compile(r"[A-Za-z0-9_$\u0080-\U0010ffff]")
_DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")


def strip_sql_comments_and_strings(sql: str) -> str:
    """Remove string literals and comments so keyword/function scans cannot
    be fooled by content inside quotes or comments.

    This must lex exactly as DuckDB does: any place where it ends a literal or
    comment somewhere DuckDB doesn't lets a ``;`` hide from the statement
    splitter. So it follows DuckDB's lexer on the points that differ from the
    naive reading: ``--`` comments end at a line feed *or* a carriage return, block comments
    nest, ``E'..'`` strings take backslash escapes, ``$tag$..$tag$`` is a
    string, and ``"`` identifiers double ``""`` to embed a quote.

    Quoted identifiers are kept (the function and path scans below need them),
    but the ``;``, ``(`` and ``)`` inside them are neutralised so they cannot
    unbalance the parenthesis depth the splitter relies on.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nx = sql[i + 1] if i + 1 < n else ""
        glued = i > 0 and _IDENT_CONT_RE.match(sql[i - 1]) is not None
        if c == "-" and nx == "-":
            j = i + 2
            while j < n and sql[j] not in "\n\r":
                j += 1
            if j >= n:
                break
            i = j + 1
            out.append(" ")
            continue
        if c == "/" and nx == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                break
            i = j
            out.append(" ")
            continue
        if c in "eE" and nx == "'" and not glued:
            # Escape string: backslash escapes the next character, '' too.
            i += 2
            while i < n:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
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
        if c == "$" and not glued:
            m = _DOLLAR_QUOTE_RE.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j < 0:
                    break
                i = j + len(tag)
                out.append("''")
                continue
        if c == '"':
            j = i + 1
            body: list[str] = []
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        body.append("_")
                        j += 2
                        continue
                    break
                body.append("_" if sql[j] in ";()" else sql[j])
                j += 1
            if j >= n:
                break
            out.append('"' + "".join(body) + '"')
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
    # Callers wrap the query (``SELECT * FROM (<sql>) AS _q LIMIT n``), so an
    # unbalanced ``SELECT 1) AS a, '<path>' AS b, (SELECT 1`` is a parse error
    # here but a valid, different query once wrapped. Balanced SQL can't
    # escape the wrapper's parentheses; unbalanced SQL is never valid anyway.
    depth = 0
    for ch in cleaned:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        raise ReadOnlyQueryError("Unbalanced parentheses in query.", status_code=400)
    statements = split_statements(cleaned)
    if not statements:
        raise ReadOnlyQueryError("Empty query.", status_code=400)
    if len(statements) > 1:
        raise ReadOnlyQueryError("Multi-statement queries are not allowed.")
    stmt = statements[0]
    head = leading_statement_keyword(stmt)
    # EXPLAIN ANALYZE executes the statement it profiles, so EXPLAIN is only as
    # read-only as what it wraps: judge the inner statement's verb instead.
    while head == "explain":
        inner = _EXPLAIN_PREFIX_RE.sub("", stmt, count=1)
        if inner == stmt:
            break
        stmt = inner
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
    _check_with_duckdb_parser(sql)


_EXPLAIN_PREFIX_RE = re.compile(
    r"^\s*explain(?![A-Za-z0-9_])\s*(?:analy[sz]e(?![A-Za-z0-9_]))?\s*(?:\([^()]*\)\s*)?",
    re.IGNORECASE,
)

_parser_local = threading.local()


def _parser_conn():
    """A per-thread in-memory DuckDB used only for parsing (never executes)."""
    conn = getattr(_parser_local, "conn", None)
    if conn is None:
        import duckdb

        conn = duckdb.connect(":memory:")
        _parser_local.conn = conn
    return conn


def _explain_target(query: str) -> str | None:
    """Return the statement text an ``EXPLAIN [ANALYZE] [(opts)]`` wraps."""
    import duckdb

    tokens = duckdb.tokenize(query)
    k = 1  # tokens[0] is EXPLAIN itself
    if k < len(tokens):
        m = _IDENT_RE.match(query, tokens[k][0])
        if m and m.group(0).lower() in ("analyze", "analyse"):
            k += 1
    if k < len(tokens) and query[tokens[k][0]] == "(":
        depth = 0
        while k < len(tokens):
            ch = query[tokens[k][0]]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
    if k >= len(tokens):
        return None
    return query[tokens[k][0]:]


def _check_with_duckdb_parser(sql: str, *, _depth: int = 0) -> None:
    """Second opinion from DuckDB's own parser on statement count and type.

    The lexer above has to track DuckDB's lexer by hand, and every divergence
    so far (escape strings, nested comments, ...) was a way to smuggle a second
    statement past it. Asking DuckDB which statements it would run closes that
    class of bug. It only parses; nothing is bound or executed.

    A ParserException passes: execution would fail on the same parse, and some
    callers validate SQL that is not executed verbatim (model bodies with
    ``{start}`` placeholders). Any other failure means DuckDB parsed the text
    but the binding could not describe it, so it is refused rather than
    trusted to the hand lexer alone.
    """
    try:
        import duckdb
    except ImportError:  # pragma: no cover - duckdb is a hard dependency
        return
    try:
        statements = _parser_conn().extract_statements(sql)
    except duckdb.ParserException:
        return
    except Exception as e:
        raise ReadOnlyQueryError(f"Query could not be parsed: {e}", status_code=400)
    st = duckdb.StatementType
    reads = 0
    for stmt in statements:
        if stmt.type == st.SELECT:
            reads += 1
            # A dynamic PIVOT's SELECT carries no text of its own; judge the
            # whole input instead.
            _check_parse_tree(stmt.query if stmt.query.strip() else sql)
        elif stmt.type == st.EXPLAIN:
            reads += 1
            target = _explain_target(stmt.query)
            if target is None or _depth > 2:
                raise ReadOnlyQueryError("Only SELECT queries can be explained.")
            _check_with_duckdb_parser(target, _depth=_depth + 1)
            continue
        elif stmt.type in (st.CREATE, st.SET) and not stmt.query.strip():
            # A dynamic PIVOT expands into CREATE TYPE ... AS ENUM statements
            # (wrapped in SETs once the connection has run a query) ahead of
            # its SELECT. Those carry no source text; anything a user wrote
            # does.
            continue
        else:
            raise ReadOnlyQueryError(
                "Only SELECT queries are allowed through the query interface."
            )
    if reads > 1:
        raise ReadOnlyQueryError("Multi-statement queries are not allowed.")


# Characters that make a table name a file path or URL rather than a catalog
# name. DuckDB resolves an unknown name like 'x.csv' or "C:/data/x" as a
# replacement scan, i.e. it reads the file.
_PATH_CHARS = frozenset("./\\:")
_PATH_LIKE_RE = re.compile(
    r"[/\\]|^[a-z][a-z0-9+.-]*:|\.(?:" + _DATA_FILE_EXT + r"|wal|ddb)\b",
    re.IGNORECASE,
)


def _check_parse_tree(query: str) -> None:
    """Refuse file scans and dangerous calls found in DuckDB's own parse tree.

    The regex scans above look at the text; this looks at what DuckDB actually
    parsed, so table position is known exactly (comma joins, aliases, nested
    FROMs, DESCRIBE/SUMMARIZE/TABLE targets) and quoting tricks around a
    function name (U&"read_csv") do not matter. Only SELECT-shaped statements
    serialise; for the rest (a dynamic PIVOT) any path-shaped literal or
    identifier is refused instead.
    """
    import json

    try:
        raw = _parser_conn().execute("SELECT json_serialize_sql(?)", [query]).fetchone()[0]
        tree = json.loads(raw)
    except Exception:
        tree = {"error": True}
    if tree.get("error"):
        if _has_path_like_token(query):
            raise ReadOnlyQueryError(
                "Reading files by path is not allowed through the query interface.",
            )
        return

    stack: list = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if node.get("type") == "BASE_TABLE":
            name = str(node.get("table_name") or "")
            if any(ch in _PATH_CHARS for ch in name):
                raise ReadOnlyQueryError(
                    "Reading files by path (FROM '<path>') is not allowed through "
                    "the query interface.",
                )
        fname = node.get("function_name")
        if isinstance(fname, str) and fname.lower() in _DANGEROUS_FUNCTION_NAMES:
            raise ReadOnlyQueryError(
                "File-access functions (read_csv, read_parquet, etc.) are not allowed.",
            )
        stack.extend(v for v in node.values() if isinstance(v, (dict, list)))


def _has_path_like_token(query: str) -> bool:
    """Whether any string literal or quoted identifier looks like a file path."""
    import duckdb

    try:
        tokens = duckdb.tokenize(query)
    except Exception:
        return False
    for k, (offset, kind) in enumerate(tokens):
        end = tokens[k + 1][0] if k + 1 < len(tokens) else len(query)
        text = query[offset:end].strip()
        if kind == duckdb.token_type.string_const or text.startswith('"'):
            if _PATH_LIKE_RE.search(text.strip("'\"$")):
                return True
    return False

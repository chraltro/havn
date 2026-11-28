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

import json
import re
import threading

from havn.engine.sql_functions import table_function_allowed

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

# Names refused wherever they are called, by the text scan as well as in the
# parse tree. Extension functions that may not be loaded when the classified
# lists in havn.engine.sql_functions were taken. Table functions are
# allowlisted on top of this (see _check_parse_tree), so this list is only
# the first of two layers for them.
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


# DuckDB 2.0 accepts INSERT / UPDATE / DELETE / COPY (and may accept more) as a
# CTE body, ``WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d``, and the
# statement as a whole still reports as a SELECT. These are the verbs that
# can sit nested inside a statement and write.
_NESTED_WRITE_VERBS = frozenset({"insert", "update", "delete", "merge", "copy"})
# Tokens of the cleaned text (strings already '' and comments gone): words,
# double-quoted identifiers kept whole, and single punctuation characters.
_TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_$]*|"[^"]*"|\S')


def _is_word(tok: str) -> bool:
    return bool(tok) and (tok[0].isalpha() or tok[0] in '_"')


def _skip_name(low: list[str], j: int) -> int:
    """Index after a possibly qualified name (``a``, ``a.b``, ``"a"."b"``), or ``j``."""
    if j >= len(low) or not _is_word(low[j]):
        return j
    j += 1
    while j + 1 < len(low) and low[j] == "." and _is_word(low[j + 1]):
        j += 2
    return j


def _skip_group(low: list[str], j: int) -> int:
    """Index after the parenthesised group opening at ``j``, or ``j``."""
    if j >= len(low) or low[j] != "(":
        return j
    depth = 0
    while j < len(low):
        if low[j] == "(":
            depth += 1
        elif low[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _write_shape(low: list[str], i: int) -> bool:
    """Whether the verb at ``low[i]`` opens a statement rather than naming a column."""
    verb = low[i]
    nxt = low[i + 1] if i + 1 < len(low) else ""
    if verb == "insert":
        return nxt in ("into", "or")
    if verb == "delete":
        return nxt == "from"
    if verb == "merge":
        return nxt == "into"
    if verb == "update":
        # UPDATE name [AS] [alias] SET
        j = _skip_name(low, i + 1)
        if j == i + 1:
            return False
        if j < len(low) and low[j] == "as":
            j += 1
        if j < len(low) and low[j] != "set" and _is_word(low[j]):
            j += 1
        return j < len(low) and low[j] == "set"
    if verb == "copy":
        if nxt == "from" and i + 2 < len(low) and low[i + 2] == "database":
            return True
        j = _skip_group(low, i + 1) if nxt == "(" else _skip_name(low, i + 1)
        if j == i + 1:
            return False
        j = _skip_group(low, j)
        return j < len(low) and low[j] in ("to", "from")
    return False


def _nested_write_in_cleaned(cleaned: str) -> str | None:
    """The write verb nested anywhere in ``cleaned``, if there is one.

    ``cleaned`` is the output of :func:`strip_sql_comments_and_strings`, so a
    keyword inside a string or a comment is already gone and a quoted
    identifier (``"delete"``) is one token that never matches. Two positions
    can open a nested statement:

    * right after ``(`` -- a CTE body, ``AS [NOT] MATERIALIZED (``, a
      subquery, a parenthesised body;
    * right after the ``)`` that closes a CTE body, where a nested
      ``WITH x AS (...) DELETE ...`` puts its verb.

    Directly inside a CTE body (``AS (`` / ``MATERIALIZED (``) only a statement
    can stand, so any non-read verb there is refused outright. Elsewhere the
    verb must also have its statement shape (``DELETE FROM``, ``UPDATE t SET``,
    ``INSERT INTO``, ``COPY x TO``), so a column called ``update`` passes.
    """
    low = [t.lower() for t in _TOKEN_RE.findall(cleaned)]
    cte_open: list[bool] = []
    closes_cte: set[int] = set()
    for i, tok in enumerate(low):
        if tok == "(":
            cte_open.append(i > 0 and low[i - 1] in ("as", "materialized"))
        elif tok == ")" and cte_open:
            if cte_open.pop():
                closes_cte.add(i)
    for i in range(1, len(low)):
        tok = low[i]
        prev = low[i - 1]
        if prev == "(" and i >= 2 and low[i - 2] in ("as", "materialized"):
            if tok in _FORBIDDEN_STATEMENT_KEYWORDS:
                return tok
        if tok in _NESTED_WRITE_VERBS and (prev == "(" or (i - 1) in closes_cte):
            if _write_shape(low, i):
                return tok
    return None


def find_nested_write(sql: str) -> str | None:
    """Return the data-modifying verb nested inside ``sql`` (a CTE body, a
    subquery, a nested WITH), or None.

    The top-level verb of a plain ``DELETE FROM t`` is not nested and is not
    reported; :func:`validate_read_only_query` judges that one separately.
    Governed write sessions use this to keep a write from hiding inside a
    statement that reports itself as a SELECT, or inside another write.
    """
    return _nested_write_in_cleaned(strip_sql_comments_and_strings(sql))


_NESTED_WRITE_MESSAGE = (
    "Data-modifying statements (INSERT, UPDATE, DELETE, MERGE, COPY) are not "
    "allowed through the query interface, including inside a CTE or subquery."
)


def _tree_has_write(tree) -> bool:
    """Whether a ``json_serialize_sql`` tree holds any non-read query node."""
    stack: list = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        for key in ("node", "query_node"):
            child = node.get(key)
            if isinstance(child, dict) and child.get("type") not in _READ_QUERY_NODES:
                return True
        ntype = node.get("type")
        if (isinstance(ntype, str) and ntype.endswith("_QUERY_NODE")) or "info_type" in node:
            return True
        stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return False


def refuse_nested_writes(sql: str) -> None:
    """Raise :class:`ReadOnlyQueryError` when a write hides inside ``sql``.

    For a statement DuckDB reports as a SELECT or EXPLAIN that is not checked
    by :func:`validate_read_only_query` (governed sessions, which may write,
    but only through statements that say so). Checks the text with the lexer,
    then DuckDB's parse tree; when there is no tree (a dynamic PIVOT), any
    write verb in the text is refused.
    """
    if find_nested_write(sql) is not None:
        raise ReadOnlyQueryError(_NESTED_WRITE_MESSAGE)
    import json

    target = sql
    if _first_keyword(sql) == "explain":
        target = _explain_target(sql) or ""
        if leading_statement_keyword(strip_sql_comments_and_strings(target)) in _FORBIDDEN_STATEMENT_KEYWORDS:
            raise ReadOnlyQueryError("Only SELECT queries can be explained.")
    try:
        raw = _parser_conn().execute("SELECT json_serialize_sql(?)", [target]).fetchall()[0][0]
        tree = json.loads(raw)
    except Exception:
        tree = {"error": True}
    if tree.get("error"):
        if _write_shape_anywhere(strip_sql_comments_and_strings(target)) is not None:
            raise ReadOnlyQueryError(_NESTED_WRITE_MESSAGE)
        return
    if _tree_has_write(tree):
        raise ReadOnlyQueryError(_NESTED_WRITE_MESSAGE)


def validate_read_only_query(sql: str, *, admin: bool = False, conn=None) -> None:
    """Reject SQL that is not a safe read-only query.

    Strips strings and comments, splits top-level statements, then rejects:
      - multi-statement queries
      - any statement whose leading keyword (after a CTE) is a mutation verb
      - a write nested anywhere in the statement (DuckDB 2.0 writing CTEs)
      - table functions not on the allowlist, and refused scalar functions
        (see ``havn.engine.sql_functions``)

    ``admin`` additionally allows the functions that read instance settings
    and internals (``duckdb_settings()``, ``current_setting()``, ``SHOW
    <setting>``, ...) and catalog listings of the ``_havn`` schema.

    ``conn`` is the connection the query will run on. With it, a function or
    type name its catalog does not already have is refused: DuckDB would
    otherwise download and load the extension that provides it
    (``'1.2.3.4'::INET`` installs inet). Governed reads always pass it.

    Unknown leading keywords are passed through to DuckDB, which will return
    a parse error so the caller gets a 400 (not a misleading 403).
    """
    cleaned = strip_sql_comments_and_strings(sql)
    # Callers wrap the query (``SELECT * FROM (<sql>) AS _q LIMIT n``), so an
    # unbalanced ``SELECT 1) AS a, '<path>' AS b, (SELECT 1`` is a parse error
    # here but a valid, different query once wrapped. Balanced SQL can't
    # escape the wrapper's parentheses; unbalanced SQL is never valid anyway.
    depth = 0
    deepest = 0
    for ch in cleaned:
        if ch == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif ch == ")":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        raise ReadOnlyQueryError("Unbalanced parentheses in query.", status_code=400)
    # DuckDB's parser and the tree walk below recurse per nesting level;
    # thousands of parentheses ended in a RecursionError (a 500).
    if deepest > MAX_NESTING:
        raise ReadOnlyQueryError(
            f"Query is nested too deeply (more than {MAX_NESTING} levels of parentheses).",
            status_code=400,
        )
    if len(sql) > MAX_QUERY_LENGTH:
        raise ReadOnlyQueryError(
            f"Query is too long (more than {MAX_QUERY_LENGTH:,} characters).", status_code=400,
        )
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
    # The leading verb of a WITH statement is the one after its CTEs, but on
    # DuckDB 2.0 a CTE body can itself be an INSERT / UPDATE / DELETE / COPY
    # and the whole statement still reports as a SELECT.
    if _nested_write_in_cleaned(stmt) is not None:
        raise ReadOnlyQueryError(_NESTED_WRITE_MESSAGE)
    # Note: at this point string literals are already stripped to '', so we
    # can scan the cleaned statement directly. We also have to catch quoted
    # identifiers: DuckDB happily accepts `"read_csv"(...)` and treats the
    # quoted identifier as the same builtin.
    for schema, fname in _called_functions(stmt):
        if _function_refused(fname, admin=admin, schema=schema):
            raise ReadOnlyQueryError(_refused_function_message(fname))
    if re.search(r'\bhttpfs_', stmt, re.IGNORECASE):
        raise ReadOnlyQueryError("HTTPFS access is not allowed through the query interface.")
    # File reads by path (``FROM '/etc/passwd'``, a replacement scan). DuckDB's
    # parse tree is exact about table position (_check_parse_tree); the text
    # check here also covers forms the parser rejects, and skips FROM inside
    # expressions (IS DISTINCT FROM 'x', extract(... FROM ...), trim, substring).
    _check_from_strings(stmt)
    _check_with_duckdb_parser(sql, admin=admin, conn=conn)


MAX_NESTING = 200
MAX_QUERY_LENGTH = 200_000

# ``name(`` that is a CTE or alias with a column list, not a call:
# ``WITH checkpoint(a) AS (...)``, ``FROM t AS glob(a, b)``.
_COLUMN_LIST_AFTER_RE = re.compile(
    r"\s*\([^()]*\)\s*(?:as\s*(?:not\s+)?(?:materialized\s*)?\(|using\s+key\b)", re.IGNORECASE
)
_NAME_CALL_RE = re.compile(
    r'(?:(?:"([A-Za-z_][A-Za-z0-9_]*)"|\b([A-Za-z_][A-Za-z0-9_]*))\s*\.\s*)?'
    r'(?:"([A-Za-z_][A-Za-z0-9_]*)"|\b([A-Za-z_][A-Za-z0-9_]*))\s*\('
)


def _called_functions(cleaned: str) -> list[tuple[str, str]]:
    """``(schema, name)`` of everything called as a function in ``cleaned``.

    Quoted names count. A name followed by a column list and ``AS (`` is a
    CTE (``WITH x(a) AS (...)``) and one right after ``AS`` is an alias with
    column names; neither calls anything.
    """
    out: list[tuple[str, str]] = []
    for m in _NAME_CALL_RE.finditer(cleaned):
        schema = m.group(1) or m.group(2) or ""
        name = m.group(3) or m.group(4)
        paren = m.end() - 1
        if _COLUMN_LIST_AFTER_RE.match(cleaned, paren):
            continue
        before = cleaned[:m.start()].rstrip()
        if re.search(r"\bas$", before, re.IGNORECASE):
            continue
        out.append((schema, name))
    return out


def _called_function_names(cleaned: str) -> list[str]:
    return [name for _schema, name in _called_functions(cleaned)]


def _function_refused(name: str, *, admin: bool = False, schema: str = "") -> bool:
    """Whether calling ``[schema.]name`` (in any position) is refused."""
    from havn.engine.sql_functions import scalar_function_refused

    return name.lower() in _DANGEROUS_FUNCTION_NAMES or scalar_function_refused(
        name, admin=admin, schema=schema
    )


def _refused_function_message(name: str) -> str:
    return (
        f"File-access functions and functions with side effects are not allowed: {name}() "
        "reads files or other state, or changes the database or its settings."
    )


def _refused_table_function_message(name: str) -> str:
    return (
        f"The table function {name}() is not available through the query interface. "
        "Read-only queries can use tables, views, project table macros and a fixed set "
        "of built-in table functions (range, generate_series, unnest, json_each, the "
        "catalog listings)."
    )


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


def _multi_statement_type():
    import duckdb

    return getattr(duckdb.StatementType, "MULTI", None)


_MULTI = _multi_statement_type()

# Words a read-only statement can open with. A MULTI statement (see
# _check_with_duckdb_parser) is accepted only when its text starts with one.
# WITH is not a read by itself on DuckDB 2.0 (a CTE body can be a DELETE);
# _check_parse_tree refuses any write verb in a MULTI statement's text, since
# its parse tree cannot be inspected. DESCRIBE / SUMMARIZE / SHOW of a dynamic
# PIVOT are MULTI on 2.0 and SELECT-typed on 1.x; both are reads.
_READ_LEAD_KEYWORDS = frozenset({
    "select", "with", "from", "pivot", "unpivot", "values", "table", "(",
    "describe", "summarize", "show",
})

# Query node types a read may contain. DuckDB 2.0 serialises a CTE body that
# writes as INSERT_QUERY_NODE / UPDATE_QUERY_NODE / DELETE_QUERY_NODE /
# COPY_QUERY_NODE (and whatever it adds next); anything not listed here is
# refused rather than trusted.
_READ_QUERY_NODES = frozenset({"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", "CTE_NODE"})


def _first_keyword(query: str) -> str:
    """The first token of ``query`` lowercased, comments skipped by DuckDB's lexer."""
    import duckdb

    try:
        tokens = duckdb.tokenize(query)
    except Exception:
        return ""
    for k, (offset, kind) in enumerate(tokens):
        if kind == duckdb.token_type.comment:
            continue
        end = tokens[k + 1][0] if k + 1 < len(tokens) else len(query)
        text = query[offset:end].strip()
        if text.startswith("("):
            return "("
        m = _IDENT_RE.match(text)
        return m.group(0).lower() if m else text[:1]
    return ""


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


def _check_with_duckdb_parser(sql: str, *, admin: bool = False, conn=None, _depth: int = 0) -> None:
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
            _check_parse_tree(stmt.query if stmt.query.strip() else sql, admin=admin, conn=conn)
        elif stmt.type == st.EXPLAIN:
            reads += 1
            target = _explain_target(stmt.query)
            if target is None or _depth > 2:
                raise ReadOnlyQueryError("Only SELECT queries can be explained.")
            _check_with_duckdb_parser(target, admin=admin, conn=conn, _depth=_depth + 1)
            continue
        elif _MULTI is not None and stmt.type == _MULTI:
            # DuckDB 2.0 reports a statement that expands into several (a
            # dynamic PIVOT, wherever it sits) as one MULTI statement carrying
            # the user's text; 1.x listed the expansion with empty text. The
            # expansion is hidden, so judge the text: CREATE TABLE AS PIVOT
            # and INSERT ... PIVOT are MULTI as well, and must not pass.
            if _first_keyword(stmt.query or sql) not in _READ_LEAD_KEYWORDS:
                raise ReadOnlyQueryError(
                    "Only SELECT queries are allowed through the query interface."
                )
            reads += 1
            _check_parse_tree(stmt.query or sql, admin=admin, conn=conn)
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


# A function call in table position: after FROM / JOIN (optionally LATERAL
# or a schema qualifier), or as the source of PIVOT / UNPIVOT / DESCRIBE /
# SUMMARIZE. Used only where no parse tree is available (a dynamic PIVOT).
# A table function after a FROM-list comma is not seen here; refused names
# are still caught anywhere by the text scan.
_TABLE_POSITION_CALL_RE = re.compile(
    r"(?:\bfrom|\bjoin|\bpivot|\bunpivot|\bdescribe|\bsummarize)\s*\(?\s*(?:lateral\s+)?"
    r"(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)*"
    r'("?[A-Za-z_][A-Za-z0-9_]*"?)\s*\(',
    re.IGNORECASE,
)
# The internal schema named in a catalog function's arguments.
_INTERNAL_SCHEMA_RE = re.compile(r"_havn\b", re.IGNORECASE)


def _check_parse_tree(query: str, *, admin: bool = False, conn=None) -> None:
    """Refuse file scans and dangerous calls found in DuckDB's own parse tree.

    The regex scans above look at the text; this looks at what DuckDB actually
    parsed, so table position is known exactly (comma joins, aliases, nested
    FROMs, DESCRIBE/SUMMARIZE/TABLE targets) and quoting tricks around a
    function name (U&"read_csv") do not matter. Only SELECT-shaped statements
    serialise; for the rest (a dynamic PIVOT) see :func:`_check_without_tree`.
    """
    import json

    try:
        # fetchall, not fetchone: on DuckDB 2.0 a half-read result keeps the
        # parser connection busy for extract_statements on the next call.
        raw = _parser_conn().execute("SELECT json_serialize_sql(?)", [query]).fetchall()[0][0]
        tree = json.loads(raw)
    except RecursionError:
        raise ReadOnlyQueryError("Query is nested too deeply.", status_code=400)
    except Exception:
        tree = {"error": True}
    if tree.get("error"):
        _check_without_tree(query, admin=admin)
        return

    # DuckDB 2.0 parses a CTE body that writes into INSERT/UPDATE/DELETE/COPY
    # query nodes, while the statement itself still reports as a SELECT.
    if _tree_has_write(tree):
        raise ReadOnlyQueryError(_NESTED_WRITE_MESSAGE)

    functions: set[str] = set()
    types: set[str] = set()
    stack: list = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype == "BASE_TABLE":
            _check_base_table(node, admin=admin)
        elif ntype == "SHOW_REF":
            _check_show_ref(node, admin=admin, conn=conn)
        # Table functions are allowlisted (havn.engine.sql_functions): DuckDB
        # keeps its file readers, loggers, profilers and checkpoints there,
        # and new releases keep adding to them.
        elif ntype == "TABLE_FUNCTION":
            fn = node.get("function") if isinstance(node.get("function"), dict) else {}
            tname = str(fn.get("function_name") or "")
            schema = str(fn.get("schema") or "")
            if not table_function_allowed(tname, admin=admin, schema=schema):
                raise ReadOnlyQueryError(_refused_table_function_message(tname))
            if not admin and tname.lower() in _INTROSPECTION_FUNCTIONS:
                _check_introspection_args(fn)
            if tname:
                functions.add(tname.lower())
        cls = node.get("class")
        fname = node.get("function_name")
        if isinstance(fname, str) and cls != "WINDOW":
            schema = str(node.get("schema") or "")
            if ntype != "TABLE_FUNCTION" and _function_refused_in_tree(fname, schema, admin=admin):
                raise ReadOnlyQueryError(_refused_function_message(fname))
            if not node.get("is_operator") and cls in ("FUNCTION", "AGGREGATE", None):
                functions.add(fname.lower())
        # Type names: DuckDB 2.0 has TYPE nodes, 1.x UNBOUND type infos.
        if cls == "TYPE" and isinstance(node.get("type_name"), str):
            types.add(node["type_name"].lower())
        if node.get("type") == "UNBOUND_TYPE_INFO" and isinstance(node.get("name"), str):
            types.add(node["name"].lower())
        stack.extend(v for v in node.values() if isinstance(v, (dict, list)))

    if conn is not None:
        from havn.engine.sql_functions import unknown_catalog_names

        unknown_fns, unknown_types = unknown_catalog_names(conn, functions, types)
        if unknown_fns or unknown_types:
            names = sorted(unknown_fns | unknown_types)
            raise ReadOnlyQueryError(
                f"Unknown function or type: {', '.join(names)}. Read-only queries can use "
                "what is already loaded; an extension that provides it has to be installed "
                "and loaded by an admin first.",
                status_code=400,
            )


def _function_refused_in_tree(name: str, schema: str, *, admin: bool) -> bool:
    from havn.engine.sql_functions import scalar_function_refused

    return name.lower() in _DANGEROUS_FUNCTION_NAMES or scalar_function_refused(
        name, admin=admin, schema=schema
    )


# Catalog listings whose argument names one object: for non-admins it must be
# a literal, so the _havn check below cannot be dodged with chr(95)||'havn'.
_INTROSPECTION_FUNCTIONS = frozenset({
    "pragma_table_info", "pragma_show", "pragma_storage_info", "pragma_metadata_info",
})
_INTERNAL_SCHEMA = "_havn"


def _function_args(fn: dict) -> list:
    """A function node's argument expressions (1.x ``children``, 2.0 ``arguments``)."""
    out = []
    for arg in (fn.get("children") or []) + (fn.get("arguments") or []):
        if isinstance(arg, dict) and isinstance(arg.get("expression"), dict) and "class" not in arg:
            arg = arg["expression"]
        out.append(arg)
    return out


def _check_introspection_args(fn: dict) -> None:
    args = _function_args(fn)
    for arg in args:
        if not isinstance(arg, dict) or arg.get("class") not in ("CONSTANT", "COLUMN_REF"):
            raise ReadOnlyQueryError(
                f"{fn.get('function_name')}() takes a table name written out as a string.",
            )
    if _INTERNAL_SCHEMA_RE.search(str(args)):
        raise ReadOnlyQueryError("The _havn schema holds havn's own metadata and is admin-only.")


def _check_base_table(node: dict, *, admin: bool) -> None:
    name = str(node.get("table_name") or "")
    qualified = bool(node.get("schema_name") or node.get("catalog_name"))
    # DuckDB resolves an unknown unqualified name like 'x.csv' or
    # "C:/data/x" as a replacement scan, i.e. it reads the file. A qualified
    # name (landing."orders.csv") is always a catalog table.
    if not qualified and any(ch in _PATH_CHARS for ch in name):
        raise ReadOnlyQueryError(
            "Reading files by path (FROM '<path>') is not allowed through the query interface.",
        )


# SHOW variants that list the catalog rather than name one object.
_SHOW_SPECIAL_TABLES = frozenset({'"tables"', '"databases"', "__show_tables_expanded", "tables", "databases"})


def _check_show_ref(node: dict, *, admin: bool, conn=None) -> None:
    """SHOW / DESCRIBE / SUMMARIZE targets.

    On DuckDB 2.0 ``SHOW <name>`` parses as a SHOW_REF without a query, and
    the binder resolves the name as a *setting* when no table has it:
    ``SHOW s3_secret_access_key`` returned the secret. For non-admins a bare
    ``SHOW <name>`` is accepted only when ``conn``'s catalog has a table or
    view of that name (without a connection, only a qualified name).
    """
    name = str(node.get("table_name") or "")
    schema = str(node.get("schema_name") or "")
    catalog = str(node.get("catalog_name") or "")
    if not schema and not catalog and any(ch in _PATH_CHARS for ch in name):
        raise ReadOnlyQueryError(
            "Reading files by path (FROM '<path>') is not allowed through the query interface.",
        )
    if admin:
        return
    if schema.lower() == _INTERNAL_SCHEMA:
        raise ReadOnlyQueryError("The _havn schema holds havn's own metadata and is admin-only.")
    if node.get("query") is not None:
        # DESCRIBE / SUMMARIZE / SHOW of a table or query: the layout of a
        # _havn table is admin-only (SELECT on one is refused by governance).
        if re.search(r'"schema_name":\s*"_havn"', json.dumps(node["query"]), re.IGNORECASE):
            raise ReadOnlyQueryError("The _havn schema holds havn's own metadata and is admin-only.")
        return
    if not name:
        return
    if name.lower() in _SHOW_SPECIAL_TABLES:
        return  # SHOW TABLES, SHOW ALL TABLES, SHOW DATABASES
    if schema or catalog:
        return
    if conn is not None and _is_catalog_relation(conn, name):
        return
    raise ReadOnlyQueryError(
        f"SHOW {name} is not available: it does not name a table or view, and showing "
        "settings is admin-only. Use DESCRIBE <table>.",
    )


def _is_catalog_relation(conn, name: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM duckdb_tables() WHERE lower(table_name) = lower(?) "
            "UNION ALL SELECT 1 FROM duckdb_views() WHERE lower(view_name) = lower(?) LIMIT 1",
            [name, name],
        ).fetchall()
    except Exception:
        return False
    return bool(rows)


# --- Fallback when there is no parse tree (a dynamic PIVOT) -----------------

# Keywords that put FROM inside an expression rather than in table position.
_FROM_SYNTAX_FUNCTIONS = frozenset({"extract", "trim", "substring", "overlay", "position", "date_part"})


def _from_is_expression_syntax(cleaned: str, at: int) -> bool:
    """Whether the FROM keyword at ``at`` belongs to an expression.

    ``IS [NOT] DISTINCT FROM``, and FROM inside ``extract(... FROM ...)``,
    ``trim(... FROM ...)``, ``substring(... FROM ...)``, ``overlay(...)``.
    """
    before = cleaned[:at].rstrip()
    if re.search(r"\bdistinct$", before, re.IGNORECASE):
        return True
    depth = 0
    for k in range(at - 1, -1, -1):
        ch = cleaned[k]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", cleaned[:k])
                return bool(m and m.group(1).lower() in _FROM_SYNTAX_FUNCTIONS)
            depth -= 1
    return False


_NOT_A_FUNCTION_KEYWORDS = frozenset({
    "pivot", "unpivot", "select", "values", "with", "lateral", "table", "from",
    "describe", "summarize", "show",
})

_FROM_STRING_RE = re.compile(r"\b(from|join)\s*\(?\s*(''|\"[^\"]*\")", re.IGNORECASE)


def _check_from_strings(cleaned: str) -> None:
    """Refuse a string, or a file-shaped quoted name, in table position."""
    for m in _FROM_STRING_RE.finditer(cleaned):
        if _from_is_expression_syntax(cleaned, m.start()):
            continue
        if m.group(2) == "''" or _DQUOTE_PATH_SCAN_RE.search(m.group(0)):
            raise ReadOnlyQueryError(
                "Reading files by path (FROM '<path>') is not allowed through the query interface.",
            )


def _check_without_tree(query: str, *, admin: bool) -> None:
    """Judge a statement DuckDB's parser cannot serialise (a dynamic PIVOT).

    Fails closed where the text cannot tell: a path-shaped literal anywhere, a
    string or file-shaped quoted name in table position, a write verb with
    its statement shape anywhere, a table function call in table position
    that is not allowed, and any refused table function named anywhere.
    """
    from havn.engine.sql_functions import REFUSED_TABLE_FUNCTIONS

    if _has_path_like_token(query):
        raise ReadOnlyQueryError("Reading files by path is not allowed through the query interface.")
    cleaned = strip_sql_comments_and_strings(query)
    _check_from_strings(cleaned)
    verb = _write_shape_anywhere(cleaned)
    if verb is not None:
        raise ReadOnlyQueryError(
            f"This query could not be verified as read-only (it contains {verb.upper()}).",
        )
    for m in _TABLE_POSITION_CALL_RE.finditer(cleaned):
        if _from_is_expression_syntax(cleaned, m.start()):
            continue
        tname = m.group(1).strip('"')
        if tname.lower() in _NOT_A_FUNCTION_KEYWORDS:
            continue  # FROM (PIVOT (...)), FROM (SELECT ...), JOIN LATERAL (...)
        if not table_function_allowed(tname, admin=admin):
            raise ReadOnlyQueryError(_refused_table_function_message(tname))
    for name in _called_function_names(cleaned):
        if name.lower() in REFUSED_TABLE_FUNCTIONS and name.lower() not in _scalar_names():
            raise ReadOnlyQueryError(_refused_table_function_message(name))


def _write_shape_anywhere(cleaned: str) -> str | None:
    """A write verb with its statement shape anywhere in ``cleaned``.

    Without a tree a column called ``copy`` or ``merge`` (``PIVOT t ON copy``)
    must still pass, so only ``DELETE FROM``, ``UPDATE t SET``, ``INSERT
    INTO``, ``MERGE INTO`` and ``COPY x TO|FROM`` count.
    """
    nested = _nested_write_in_cleaned(cleaned)
    if nested is not None:
        return nested
    low = [t.lower() for t in _TOKEN_RE.findall(cleaned)]
    for i, tok in enumerate(low):
        if tok in _NESTED_WRITE_VERBS and _write_shape(low, i):
            return tok
    return None


_scalar_names_cache: frozenset[str] | None = None


def _scalar_names() -> frozenset[str]:
    """Names DuckDB has as scalar, aggregate or window functions (histogram is both)."""
    global _scalar_names_cache
    if _scalar_names_cache is None:
        try:
            rows = _parser_conn().execute(
                "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
                "WHERE function_type IN ('scalar', 'aggregate', 'macro', 'window')"
            ).fetchall()
            _scalar_names_cache = frozenset(r[0] for r in rows)
        except Exception:
            return frozenset()
    return _scalar_names_cache


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

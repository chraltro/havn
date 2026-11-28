"""Which DuckDB functions a read-only query may call.

``sql_safety.validate_read_only_query`` consults this module. Two rules:

* **Table functions are allowlisted.** A ``TABLE_FUNCTION`` in DuckDB's parse
  tree must be on :data:`ALLOWED_TABLE_FUNCTIONS` (or, for admins,
  :data:`ADMIN_TABLE_FUNCTIONS`), or be a project table macro whose body
  passed the same check (:func:`refresh_user_macros`). Anything else, a
  function a new DuckDB release or an extension adds included, is refused.
  Table functions are where DuckDB keeps its file readers, loggers,
  profilers and checkpoints (``read_single_csv_file``, ``enable_logging``,
  ``checkpoint()``), and a blocklist kept missing new ones.
* **Scalar functions are blocklisted** (DuckDB has well over a thousand):
  :data:`REFUSED_SCALAR_FUNCTIONS` for everyone, :data:`ADMIN_SCALAR_FUNCTIONS`
  for non-admins. To keep a new release from slipping a side-effecting
  scalar past the list, every scalar whose name or description matches
  :data:`SCALAR_REVIEW_HEURISTIC` must be classified here, refused or in
  :data:`REVIEWED_SAFE_SCALAR_FUNCTIONS`; ``tests/test_sql_safety_functions.py``
  fails on an unclassified one, on DuckDB 1.5 and 2.x alike.

Every built-in table function, table macro and pragma of both supported
DuckDB versions is classified below; the same test fails on one that is not.
Pragmas are only reachable through the ``PRAGMA`` / ``CALL`` statements, both
of which the read-only check refuses outright.
"""

from __future__ import annotations

import re
import threading

# Safe for any reader: generators, JSON helpers and catalog listings. The
# catalog listings show names and types, never rows.
ALLOWED_TABLE_FUNCTIONS = frozenset({
    "range", "generate_series", "unnest", "repeat", "repeat_row",
    "json_each", "json_tree",
    "icu_calendar_names", "pg_timezone_names",
    "duckdb_columns", "duckdb_constraints", "duckdb_dependencies", "duckdb_functions",
    "duckdb_indexes", "duckdb_keywords", "duckdb_schemas", "duckdb_sequences",
    "duckdb_tables", "duckdb_types", "duckdb_views", "duckdb_triggers",
    "duckdb_optimizers", "duckdb_dialects", "duckdb_coordinate_systems",
    "pragma_table_info", "pragma_show", "pragma_collations", "pragma_version",
    "pragma_platform", "pragma_user_agent", "pragma_database_size",
})

# Instance settings, attached database paths, extensions, memory and
# connection internals: admins only.
ADMIN_TABLE_FUNCTIONS = frozenset({
    "duckdb_settings", "duckdb_databases", "duckdb_extensions", "duckdb_extension_repositories",
    "duckdb_memory", "duckdb_connection_count", "duckdb_approx_database_count",
    "duckdb_eviction_queues", "duckdb_available_metrics", "duckdb_live_query_metrics",
    "duckdb_variables", "duckdb_prepared_statements", "duckdb_secret_types",
    "duckdb_secret_type_parameters", "duckdb_external_resource_types",
    "duckdb_external_resources", "duckdb_grammar_extensions", "duckdb_profiling_settings",
    "pragma_storage_info", "pragma_metadata_info",
})

# Refused for everyone. Grouped by why.
REFUSED_TABLE_FUNCTIONS = frozenset({
    # Read files, URLs or other databases.
    "read_csv", "read_csv_auto", "read_json", "read_json_auto", "read_json_objects",
    "read_json_objects_auto", "read_ndjson", "read_ndjson_auto", "read_ndjson_objects",
    "read_parquet", "read_blob", "read_text", "read_duckdb", "read_single_csv_file",
    "read_single_json_file", "read_single_parquet_file", "parquet_scan", "parquet_metadata",
    "parquet_file_metadata", "parquet_full_metadata", "parquet_kv_metadata", "parquet_schema",
    "parquet_page_index", "parquet_bloom_probe", "sniff_csv", "glob",
    # Change instance state or write files: logging, profiling, checkpoints,
    # grammar extensions and external resources.
    "enable_logging", "disable_logging", "truncate_duckdb_logs", "enable_profiling",
    "disable_profiling", "checkpoint", "force_checkpoint", "add_grammar_extension",
    "remove_grammar_extension", "create_external_resource", "register_external_resource",
    "register_external_resource_type", "deregister_external_resource", "destroy_external_resource",
    # Logs (other users' queries), secrets, caches and temporary files.
    "duckdb_logs", "duckdb_logs_parsed", "duckdb_log_contexts", "duckdb_secrets", "which_secret",
    "duckdb_external_file_cache", "duckdb_temporary_files",
    # Run SQL from a string, or scan a table around governance.
    "query", "query_table", "json_execute_serialized_sql", "seq_scan", "duckdb_table_sample",
    "summary", "histogram", "histogram_values",
    # In-process pointers and Python objects.
    "arrow_scan", "arrow_scan_dumb", "pandas_scan", "python_arrow_scan", "python_map_function",
    # Test fixtures.
    "test_all_types", "test_vector_types",
})

# Scalars with side effects, or that reach files, the network, the process
# or other users' state.
REFUSED_SCALAR_FUNCTIONS = frozenset({
    "nextval", "setval", "currval", "setseed", "write_log",
    "pg_sleep", "sleep_ms", "json_serialize_sql", "json_serialize_plan",
    "json_execute_serialized_sql", "getenv", "http_get", "http_post",
    "write_csv", "write_parquet", "load_aws_credentials", "duckdb_secrets", "which_secret",
})

# Scalars that read instance configuration: admins only.
ADMIN_SCALAR_FUNCTIONS = frozenset({"current_setting"})

# Scalar names or descriptions that hint at I/O, configuration or state.
# Every match must be classified (refused, admin-only or reviewed safe).
SCALAR_REVIEW_HEURISTIC = re.compile(
    r"file|path|read|write|log|setting|seq|val$|env|secret|extension|external|checkpoint"
    r"|profil|python|pandas|arrow|http|s3|cache|temp|glob|attach|load|install|query|sql"
    r"|execut|serial|seed|config|resource|grammar|variable|current_|disk|director|url"
    r"|network|sleep|system|memory|thread",
    re.IGNORECASE,
)

# Matched the heuristic, reviewed, and safe: pure functions over their
# arguments, or session facts such as the current schema.
REVIEWED_SAFE_SCALAR_FUNCTIONS = frozenset({
    "aggregate", "approx_count_distinct", "array_aggr", "array_aggregate", "array_reduce",
    "current_catalog", "current_connection_id", "current_database", "current_date",
    "current_localtime", "current_localtimestamp", "current_query", "current_query_id",
    "current_role", "current_schema", "current_schemas", "current_transaction_id",
    "current_user", "damerau_levenshtein", "decimal_division", "entropy", "epoch", "epoch_ms",
    "epoch_ns", "epoch_us", "equi_width_bins", "formatReadableDecimalSize",
    "formatReadableSize", "format_bytes", "get_current_time", "get_current_timestamp",
    "getvariable", "has_sequence_privilege", "hive_partition_component", "in_search_path",
    "index_key", "json_deserialize_sql", "json_extract_path", "json_extract_path_text",
    "lgamma", "list_aggr", "list_aggregate", "list_reduce", "ln", "log", "log10", "log2",
    "mad", "normalized_interval", "parse_dirname", "parse_dirpath",
    "parse_duckdb_log_message", "parse_filename", "parse_formatted_bytes", "parse_path",
    "path_join", "pg_conf_load_time", "pg_is_other_temp_schema", "pg_my_temp_schema",
    "pg_ts_config_is_visible", "pg_ts_template_is_visible", "reduce", "regexp_escape",
    "regr_avgx", "regr_avgy", "st_crs", "st_setcrs", "stats", "struct_insert",
    "struct_pack", "struct_update", "to_centuries", "to_days", "to_decades", "to_hours",
    "to_microseconds", "to_millennia", "to_milliseconds", "to_minutes", "to_months",
    "to_quarters", "to_seconds", "to_weeks", "to_years", "txid_current", "union_value",
    "url_decode", "url_encode", "variant_comparator",
})


# ---------------------------------------------------------------------------
# Project macros
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# Project / package / stdlib table macros whose body passed the read-only
# check: callable like an allowed table function.
_safe_user_table_macros: frozenset[str] = frozenset()
# Project macros (scalar or table) whose body calls something refused.
_refused_user_macros: frozenset[str] = frozenset()


def safe_user_table_macros() -> frozenset[str]:
    return _safe_user_table_macros


def refused_user_macros() -> frozenset[str]:
    return _refused_user_macros


def table_function_allowed(name: str, *, admin: bool = False) -> bool:
    key = name.lower()
    if key in ALLOWED_TABLE_FUNCTIONS or key in _safe_user_table_macros:
        return True
    return admin and key in ADMIN_TABLE_FUNCTIONS


def scalar_function_refused(name: str, *, admin: bool = False) -> bool:
    key = name.lower()
    if key in REFUSED_SCALAR_FUNCTIONS or key in REFUSED_TABLE_FUNCTIONS or key in _refused_user_macros:
        return True
    return not admin and (key in ADMIN_SCALAR_FUNCTIONS or key in ADMIN_TABLE_FUNCTIONS)


def refresh_user_macros(conn) -> None:
    """Classify the non-built-in macros defined on ``conn``.

    A table macro (``@table_macro`` from the project, a package or havn's
    stdlib, or ``CREATE MACRO ... AS TABLE`` in ``macros/*.sql``) becomes
    callable from read-only queries once its body passes the read-only check.
    A body that reads files or calls anything refused -- directly or through
    another macro -- makes that macro refused. Viewers cannot define macros:
    ``CREATE`` is refused on every read surface, and governed sessions refuse
    ``CREATE MACRO``.

    Called after havn registers macros on a connection. Until it has run in a
    process, project table macros are refused (fail closed).
    """
    global _safe_user_table_macros, _refused_user_macros
    from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

    try:
        rows = conn.execute(
            "SELECT DISTINCT lower(function_name), function_type, macro_definition "
            "FROM duckdb_functions() WHERE NOT internal "
            "AND function_type IN ('macro', 'table_macro')"
        ).fetchall()
    except Exception:
        return
    tables = {n: d for n, t, d in rows if t == "table_macro" and d}
    scalars = {n: d for n, t, d in rows if t == "macro" and d}
    safe: set[str] = set()
    refused: set[str] = set()
    with _lock:
        old_safe, old_refused = _safe_user_table_macros, _refused_user_macros
        try:
            # Fixed point: a macro calling another project macro is decided
            # once that one is.
            changed = True
            while changed:
                changed = False
                _safe_user_table_macros, _refused_user_macros = frozenset(safe), frozenset(refused)
                for name, body in list(tables.items()) + list(scalars.items()):
                    if name in safe or name in refused:
                        continue
                    sql = body if name in tables else f"SELECT ({body})"
                    try:
                        validate_read_only_query(sql)
                    except ReadOnlyQueryError as e:
                        if "parse" in str(e).lower():
                            continue
                        if name in tables and _only_waits_on_tables(body, tables, safe, refused):
                            continue
                        refused.add(name)
                        changed = True
                    else:
                        if name in tables:
                            safe.add(name)
                            changed = True
            # Table macros still undecided (cycles, unparseable bodies) stay
            # unlisted, i.e. refused.
            _safe_user_table_macros = frozenset(safe)
            _refused_user_macros = frozenset(refused | (set(tables) - safe))
        except Exception:
            _safe_user_table_macros, _refused_user_macros = old_safe, old_refused
            raise


def _only_waits_on_tables(body: str, tables: dict, safe: set, refused: set) -> bool:
    """Whether ``body`` calls an undecided project table macro (decide it later)."""
    called = {m.lower() for m in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", body)}
    return any(n in tables and n not in safe and n not in refused for n in called)


def reset_user_macros() -> None:
    """Forget project macros (tests)."""
    global _safe_user_table_macros, _refused_user_macros
    with _lock:
        _safe_user_table_macros = frozenset()
        _refused_user_macros = frozenset()

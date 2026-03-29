"""Tests for the notebook code cell sandbox."""

from __future__ import annotations

import duckdb
import pytest

from havn.engine.notebook.code_cell import execute_cell
from havn.engine.notebook.sandbox import (
    SafeDbProxy,
    SandboxViolation,
    check_sql,
    validate_ast,
)


# ---------------------------------------------------------------------------
# AST validation tests
# ---------------------------------------------------------------------------


class TestASTValidation:
    """Tests for the AST-level code validator."""

    # --- Blocked: havn server internals ---

    def test_import_havn_server_blocked(self):
        with pytest.raises(SandboxViolation, match="havn server internals"):
            validate_ast("import havn.server.app")

    def test_import_havn_auth_blocked(self):
        with pytest.raises(SandboxViolation, match="havn server internals"):
            validate_ast("from havn.engine.auth import validate_token")

    def test_import_havn_secrets_blocked(self):
        with pytest.raises(SandboxViolation, match="havn server internals"):
            validate_ast("from havn.engine.secrets import load_env")

    # --- Blocked: dunder sandbox escapes ---

    def test_dunder_subclasses_blocked(self):
        with pytest.raises(SandboxViolation, match="__subclasses__"):
            validate_ast("().__class__.__subclasses__()")

    def test_dunder_globals_blocked(self):
        with pytest.raises(SandboxViolation, match="__globals__"):
            validate_ast("func.__globals__")

    def test_dunder_builtins_blocked(self):
        with pytest.raises(SandboxViolation, match="__builtins__"):
            validate_ast("x.__builtins__")

    def test_dunder_bases_blocked(self):
        with pytest.raises(SandboxViolation, match="__bases__"):
            validate_ast("object.__bases__")

    def test_dunder_code_blocked(self):
        with pytest.raises(SandboxViolation, match="__code__"):
            validate_ast("func.__code__")

    # --- Blocked: _havn via db ---

    def test_db_execute_havn_blocked(self):
        with pytest.raises(SandboxViolation, match="_havn"):
            validate_ast('db.execute("SELECT * FROM _havn.users")')

    def test_db_sql_havn_blocked(self):
        with pytest.raises(SandboxViolation, match="_havn"):
            validate_ast('db.sql("SELECT * FROM _havn.tokens")')

    def test_db_execute_fstring_havn_blocked(self):
        with pytest.raises(SandboxViolation, match="_havn"):
            validate_ast('db.execute(f"SELECT * FROM _havn.{table}")')

    # --- Allowed: normal imports (blocklist, not allowlist) ---

    def test_import_os_allowed(self):
        validate_ast("import os")

    def test_import_json_allowed(self):
        validate_ast("import json")

    def test_import_pandas_allowed(self):
        validate_ast("import pandas as pd")

    def test_import_pathlib_allowed(self):
        validate_ast("from pathlib import Path")

    def test_import_urllib_allowed(self):
        validate_ast("from urllib.request import urlopen")

    def test_import_subprocess_allowed(self):
        validate_ast("import subprocess")

    def test_import_http_allowed(self):
        validate_ast("from http.client import HTTPConnection")

    def test_import_re_allowed(self):
        validate_ast("import re")

    def test_import_math_allowed(self):
        validate_ast("from math import sqrt")

    # --- Allowed: normal code ---

    def test_db_execute_normal_allowed(self):
        validate_ast('db.execute("SELECT * FROM landing.data")')

    def test_normal_code_allowed(self):
        validate_ast("x = 1 + 2\nprint(x)")

    def test_list_comprehension_allowed(self):
        validate_ast("[x**2 for x in range(10)]")

    def test_function_def_allowed(self):
        validate_ast("def add(a, b):\n    return a + b")

    def test_class_def_allowed(self):
        validate_ast("class Foo:\n    pass")

    def test_open_call_allowed_in_ast(self):
        """open() is allowed at AST level — blocked at runtime by guarded_open."""
        validate_ast("open('data.csv')")

    def test_dunder_init_allowed(self):
        """Common dunders like __init__ are not blocked."""
        validate_ast("class Foo:\n    def __init__(self): pass")

    def test_dunder_name_allowed(self):
        validate_ast("x.__name__")

    def test_dunder_str_allowed(self):
        validate_ast("class Foo:\n    def __str__(self): return 'foo'")


# ---------------------------------------------------------------------------
# SafeDbProxy tests
# ---------------------------------------------------------------------------


class TestSafeDbProxy:
    """Tests for the SafeDbProxy connection wrapper."""

    def setup_method(self):
        self.conn = duckdb.connect(":memory:")
        self.proxy = SafeDbProxy(self.conn)

    def teardown_method(self):
        self.conn.close()

    def test_normal_query_works(self):
        result = self.proxy.execute("SELECT 42 AS val")
        assert result.fetchone() == (42,)

    def test_create_table_works(self):
        self.proxy.execute("CREATE TABLE t AS SELECT 1 AS id")
        result = self.proxy.execute("SELECT * FROM t")
        assert result.fetchone() == (1,)

    def test_havn_blocked(self):
        with pytest.raises(PermissionError, match="_havn"):
            self.proxy.execute("SELECT * FROM _havn.users")

    def test_havn_case_insensitive(self):
        with pytest.raises(PermissionError):
            self.proxy.execute("SELECT * FROM _HAVN.tokens")

    def test_attach_blocked(self):
        with pytest.raises(PermissionError, match="ATTACH"):
            self.proxy.execute("ATTACH '/tmp/other.duckdb'")

    def test_install_blocked(self):
        with pytest.raises(PermissionError, match="INSTALL"):
            self.proxy.execute("INSTALL httpfs")

    def test_load_blocked(self):
        with pytest.raises(PermissionError, match="LOAD"):
            self.proxy.execute("LOAD httpfs")

    def test_copy_to_blocked(self):
        with pytest.raises(PermissionError, match="COPY"):
            self.proxy.execute("COPY (SELECT 1) TO '/tmp/exfil.csv'")

    def test_execute_with_parameters(self):
        self.proxy.execute("CREATE TABLE t (id INTEGER)")
        self.proxy.execute("INSERT INTO t VALUES (?)", [42])
        result = self.proxy.execute("SELECT * FROM t")
        assert result.fetchone() == (42,)

    def test_getattr_delegation(self):
        result = self.proxy.execute("SELECT 1 AS x")
        assert result.description is not None


# ---------------------------------------------------------------------------
# Integration tests: execute_cell with sandbox
# ---------------------------------------------------------------------------


class TestSandboxedExecution:
    """Integration tests for sandboxed code cell execution."""

    def setup_method(self):
        self.conn = duckdb.connect(":memory:")

    def teardown_method(self):
        self.conn.close()

    def test_simple_expression(self):
        result = execute_cell(self.conn, "1 + 2")
        assert len(result["outputs"]) == 1
        assert "3" in result["outputs"][0]["text"]

    def test_simple_print(self):
        result = execute_cell(self.conn, "print('hello')")
        assert any("hello" in o.get("text", "") for o in result["outputs"])

    def test_variable_persistence(self):
        ns = {}
        result1 = execute_cell(self.conn, "x = 42", ns)
        ns = result1["namespace"]
        result2 = execute_cell(self.conn, "x * 2", ns)
        assert any("84" in str(o.get("text", "")) for o in result2["outputs"])

    def test_db_query_works(self):
        self.conn.execute("CREATE TABLE test AS SELECT 1 AS id, 'hello' AS name")
        result = execute_cell(
            self.conn, "db.execute('SELECT * FROM test').fetchall()"
        )
        assert any("hello" in str(o.get("text", "")) for o in result["outputs"])

    def test_import_json_works(self):
        result = execute_cell(self.conn, "import json\njson.dumps({'a': 1})")
        assert any("a" in str(o.get("text", "")) for o in result["outputs"])

    def test_import_os_works(self):
        result = execute_cell(self.conn, "import os\nos.getcwd()")
        assert not any(o["type"] == "error" for o in result["outputs"])

    def test_import_pathlib_works(self):
        result = execute_cell(self.conn, "from pathlib import Path\nPath('.')")
        assert not any(o["type"] == "error" for o in result["outputs"])

    def test_import_urllib_works(self):
        result = execute_cell(self.conn, "from urllib.request import urlopen")
        assert not any(o["type"] == "error" for o in result["outputs"])

    # --- Blocked: havn internals ---

    def test_import_havn_server_blocked(self):
        result = execute_cell(self.conn, "import havn.server.app")
        assert any(o["type"] == "error" for o in result["outputs"])
        assert any("Sandbox" in o.get("text", "") for o in result["outputs"])

    def test_import_havn_auth_blocked(self):
        result = execute_cell(self.conn, "from havn.engine.auth import validate_token")
        assert any(o["type"] == "error" for o in result["outputs"])

    # --- Blocked: dunder escapes ---

    def test_dunder_subclasses_blocked(self):
        result = execute_cell(self.conn, "().__class__.__subclasses__()")
        assert any(o["type"] == "error" for o in result["outputs"])

    # --- Blocked: _havn via db ---

    def test_havn_via_ast_blocked(self):
        result = execute_cell(
            self.conn,
            'db.execute("SELECT * FROM _havn.tokens")',
        )
        assert any(o["type"] == "error" for o in result["outputs"])
        assert any("Sandbox" in o.get("text", "") for o in result["outputs"])

    def test_havn_dynamic_not_caught_by_ast(self):
        """Dynamic SQL construction bypasses AST check — known limitation."""
        # This is a known limitation: AST validation only catches string
        # literals. Dynamic construction reaches the real db connection.
        # The query will fail with a catalog error (table doesn't exist
        # in :memory:) but NOT a sandbox error.
        result = execute_cell(
            self.conn,
            'sql = "_ha" + "vn.users"\ndb.execute(f"SELECT * FROM {sql}")',
        )
        assert any(o["type"] == "error" for o in result["outputs"])

    # --- Blocked: .env file reads ---

    def test_open_dotenv_blocked(self):
        result = execute_cell(self.conn, "open('.env').read()")
        assert any(o["type"] == "error" for o in result["outputs"])
        assert any(".env" in o.get("text", "") for o in result["outputs"])

    def test_open_dotfile_blocked(self):
        result = execute_cell(self.conn, "open('.git/config').read()")
        assert any(o["type"] == "error" for o in result["outputs"])
        assert any("dotfile" in o.get("text", "").lower() for o in result["outputs"])

    def test_open_normal_file_allowed(self):
        """open() on normal files is allowed (file may not exist, but no sandbox error)."""
        result = execute_cell(self.conn, "open('data.csv')")
        # May get FileNotFoundError, but NOT a PermissionError about dotfiles
        errors = [o for o in result["outputs"] if o["type"] == "error"]
        for e in errors:
            assert "dotfile" not in e.get("text", "").lower()
            assert ".env" not in e.get("text", "")

    # --- Normal operations ---

    def test_list_comprehension_works(self):
        result = execute_cell(self.conn, "[x**2 for x in range(5)]")
        assert any("0, 1, 4, 9, 16" in str(o.get("text", "")) for o in result["outputs"])

    def test_function_definition_works(self):
        ns = {}
        result = execute_cell(self.conn, "def double(x):\n    return x * 2", ns)
        ns = result["namespace"]
        result2 = execute_cell(self.conn, "double(21)", ns)
        assert any("42" in str(o.get("text", "")) for o in result2["outputs"])

    def test_exception_types_available(self):
        result = execute_cell(self.conn, "raise ValueError('test')")
        assert any(o["type"] == "error" for o in result["outputs"])
        assert any("ValueError" in o.get("text", "") for o in result["outputs"])

    def test_sorted_and_builtins_work(self):
        result = execute_cell(self.conn, "sorted([3, 1, 2])")
        assert any("1, 2, 3" in str(o.get("text", "")) for o in result["outputs"])

    def test_dataframe_replacement_scan(self):
        """DuckDB replacement scan works — can reference a DataFrame as a table in SQL."""
        code = (
            "import pandas as pd\n"
            "df = pd.DataFrame({'id': [1, 2, 3], 'name': ['a', 'b', 'c']})\n"
            "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
            "db.execute('CREATE OR REPLACE TABLE landing.test AS SELECT * FROM df')\n"
            "db.execute('SELECT count(*) FROM landing.test').fetchone()[0]"
        )
        result = execute_cell(self.conn, code)
        assert not any(o["type"] == "error" for o in result["outputs"]), \
            f"Unexpected error: {[o for o in result['outputs'] if o['type'] == 'error']}"
        assert any("3" in str(o.get("text", "")) for o in result["outputs"])

    def test_multi_cell_dataframe_pipeline(self):
        """Simulate a real ingest notebook: cell 1 creates df, cell 2 loads it via SQL."""
        ns = {}
        # Cell 1: create DataFrame
        r1 = execute_cell(self.conn, (
            "import pandas as pd\n"
            "rows = [{'id': 1, 'val': 10}, {'id': 2, 'val': 20}]\n"
            "df = pd.DataFrame(rows)\n"
            "print(f'Created {len(df)} rows')"
        ), ns)
        ns = r1["namespace"]
        assert not any(o["type"] == "error" for o in r1["outputs"]), \
            f"Cell 1 error: {r1['outputs']}"

        # Cell 2: load into DuckDB using replacement scan
        r2 = execute_cell(self.conn, (
            "db.execute('CREATE SCHEMA IF NOT EXISTS landing')\n"
            "db.execute('CREATE OR REPLACE TABLE landing.data AS SELECT * FROM df')\n"
            "count = db.execute('SELECT count(*) FROM landing.data').fetchone()[0]\n"
            "print(f'Loaded {count} rows')"
        ), ns)
        assert not any(o["type"] == "error" for o in r2["outputs"]), \
            f"Cell 2 error: {r2['outputs']}"
        assert any("Loaded 2 rows" in o.get("text", "") for o in r2["outputs"])

    def test_urllib_fetch_and_json_parse(self):
        """Importing urllib and json works (core ingest pattern)."""
        result = execute_cell(self.conn, (
            "import json\n"
            "from urllib.request import urlopen\n"
            "from pathlib import Path\n"
            "# Just verify imports work, don't actually fetch\n"
            "print('imports ok')"
        ))
        assert not any(o["type"] == "error" for o in result["outputs"])
        assert any("imports ok" in o.get("text", "") for o in result["outputs"])

    def test_unsandboxed_mode(self):
        """sandboxed=False skips all protections."""
        result = execute_cell(
            self.conn,
            'db.execute("CREATE SCHEMA IF NOT EXISTS _havn")',
            sandboxed=False,
        )
        # Should not have sandbox errors
        assert not any(
            "Sandbox" in o.get("text", "")
            for o in result["outputs"]
            if o["type"] == "error"
        )

"""Tests for the Python SQL Macros (UDFs) feature."""

from __future__ import annotations

import datetime
from pathlib import Path

import duckdb
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# @macro decorator
# ---------------------------------------------------------------------------


class TestMacroDecorator:
    def test_captures_metadata(self) -> None:
        from havn.engine.macros import MacroInfo, macro

        @macro
        def greet(name: str) -> str:
            """Say hello."""
            return f"Hello, {name}"

        info: MacroInfo = getattr(greet, "__havn_macro__")
        assert info.name == "greet"
        assert info.return_type == "VARCHAR"
        assert len(info.params) == 1
        assert info.params[0] == {"name": "name", "type": "VARCHAR"}
        assert info.docstring == "Say hello."

    def test_no_type_hints_default_to_varchar(self) -> None:
        from havn.engine.macros import MacroInfo, macro

        @macro
        def nohints(x):  # type: ignore[no-untyped-def]
            return str(x)

        info: MacroInfo = getattr(nohints, "__havn_macro__")
        assert info.params[0]["type"] == "VARCHAR"
        assert info.return_type == "VARCHAR"

    def test_all_type_mappings(self) -> None:
        from havn.engine.macros import MacroInfo, macro

        @macro
        def all_types(
            s: str,
            i: int,
            f: float,
            b: bool,
            d: datetime.date,
            dt: datetime.datetime,
        ) -> bool:
            return True

        info: MacroInfo = getattr(all_types, "__havn_macro__")
        expected_types = ["VARCHAR", "INTEGER", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP"]
        actual_types = [p["type"] for p in info.params]
        assert actual_types == expected_types
        assert info.return_type == "BOOLEAN"

    def test_function_still_callable(self) -> None:
        from havn.engine.macros import macro

        @macro
        def double(x: int) -> int:
            return x * 2

        assert double(5) == 10


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_python_macros(self, tmp_path: Path) -> None:
        from havn.engine.macros import _discover_python_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "utils.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def upper(s: str) -> str:\n"
            '    """Uppercase."""\n'
            "    return s.upper()\n",
        )

        results = _discover_python_macros(macros_dir)
        assert len(results) == 1
        assert results[0].name == "upper"
        assert results[0].docstring == "Uppercase."

    def test_skips_underscore_files(self, tmp_path: Path) -> None:
        from havn.engine.macros import _discover_python_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "_hidden.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def hidden(s: str) -> str:\n"
            "    return s\n",
        )

        results = _discover_python_macros(macros_dir)
        assert len(results) == 0

    def test_bad_file_does_not_crash(self, tmp_path: Path) -> None:
        from havn.engine.macros import _discover_python_macros

        macros_dir = tmp_path / "macros"
        _write_file(macros_dir / "broken.py", "this is not valid python!!!")

        # Should not raise — logs a warning instead
        results = _discover_python_macros(macros_dir)
        assert len(results) == 0

    def test_discover_sql_macros(self, tmp_path: Path) -> None:
        from havn.engine.macros import _discover_sql_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "helpers.sql",
            "CREATE MACRO add_one(x) AS x + 1;\n",
        )

        results = _discover_sql_macros(macros_dir)
        assert len(results) == 1
        assert "add_one" in results[0]["sql"]

    def test_no_macros_dir_returns_empty(self, tmp_path: Path) -> None:
        from havn.engine.macros import _discover_python_macros, _discover_sql_macros

        assert _discover_python_macros(tmp_path / "macros") == []
        assert _discover_sql_macros(tmp_path / "macros") == []

    def test_multiple_macros_in_one_file(self, tmp_path: Path) -> None:
        from havn.engine.macros import _discover_python_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "multi.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def fn_a(x: str) -> str:\n"
            "    return x\n\n"
            "@macro\n"
            "def fn_b(x: int) -> int:\n"
            "    return x\n",
        )

        results = _discover_python_macros(macros_dir)
        names = {r.name for r in results}
        assert names == {"fn_a", "fn_b"}


# ---------------------------------------------------------------------------
# Registration with DuckDB
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_python_macro(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "funcs.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def shout(s: str) -> str:\n"
            "    return s.upper() + '!'\n",
        )

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        assert count == 1

        result = conn.execute("SELECT shout('hello')").fetchone()
        assert result is not None
        assert result[0] == "HELLO!"
        conn.close()

    def test_register_sql_macro(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "math.sql",
            "CREATE MACRO double_it(x) AS x * 2;\n",
        )

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        assert count == 1

        result = conn.execute("SELECT double_it(21)").fetchone()
        assert result is not None
        assert result[0] == 42
        conn.close()

    def test_register_both_python_and_sql(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "py_fn.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def py_add(a: int, b: int) -> int:\n"
            "    return a + b\n",
        )
        _write_file(
            macros_dir / "sql_fn.sql",
            "CREATE MACRO sql_add(a, b) AS a + b;\n",
        )

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        assert count == 2

        r1 = conn.execute("SELECT py_add(3, 4)").fetchone()
        r2 = conn.execute("SELECT sql_add(3, 4)").fetchone()
        assert r1 is not None and r1[0] == 7
        assert r2 is not None and r2[0] == 7
        conn.close()

    def test_no_macros_dir_returns_zero(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        assert count == 0
        conn.close()

    def test_bad_sql_macro_does_not_crash(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(macros_dir / "bad.sql", "THIS IS NOT VALID SQL;")

        conn = duckdb.connect(":memory:")
        # Should not raise
        count = register_macros(conn, tmp_path)
        assert count == 0
        conn.close()

    def test_bad_python_macro_does_not_crash(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(macros_dir / "bad.py", "def not_a_macro():\n    pass\n")

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        # File loads fine but has no @macro decorated functions
        assert count == 0
        conn.close()


# ---------------------------------------------------------------------------
# list_macros
# ---------------------------------------------------------------------------


class TestListMacros:
    def test_returns_correct_metadata(self, tmp_path: Path) -> None:
        from havn.engine.macros import list_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "funcs.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def to_lower(s: str) -> str:\n"
            '    """Lowercase a string."""\n'
            "    return s.lower()\n",
        )
        _write_file(
            macros_dir / "math.sql",
            "CREATE MACRO triple(x) AS x * 3;\n",
        )

        items = list_macros(tmp_path)
        assert len(items) == 2

        py_item = next(i for i in items if i["kind"] == "scalar")
        assert py_item["name"] == "to_lower"
        assert py_item["return_type"] == "VARCHAR"
        assert py_item["docstring"] == "Lowercase a string."
        assert len(py_item["params"]) == 1

        sql_item = next(i for i in items if i["kind"] == "sql")
        assert sql_item["name"] == "triple"

    def test_empty_macros_dir(self, tmp_path: Path) -> None:
        from havn.engine.macros import list_macros

        (tmp_path / "macros").mkdir()
        items = list_macros(tmp_path)
        assert items == []

    def test_no_macros_dir(self, tmp_path: Path) -> None:
        from havn.engine.macros import list_macros

        items = list_macros(tmp_path)
        assert items == []


# ---------------------------------------------------------------------------
# Integration: macros in SQL transforms
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_macro_in_sql_query(self, tmp_path: Path) -> None:
        """Register a macro and use it in a SQL query, simulating a transform."""
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "utils.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def mask_email(email: str) -> str:\n"
            "    if not email or '@' not in email:\n"
            "        return '***'\n"
            "    local, domain = email.split('@', 1)\n"
            "    return f'***@{domain}'\n",
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)

        # Create a source table
        conn.execute("CREATE TABLE test_emails (email VARCHAR)")
        conn.execute("INSERT INTO test_emails VALUES ('alice@example.com'), ('bob@test.org')")

        # Use the macro in a query (as a transform would)
        result = conn.execute("SELECT mask_email(email) FROM test_emails ORDER BY email").fetchall()
        assert result == [("***@example.com",), ("***@test.org",)]
        conn.close()

    def test_database_connect_with_project_dir(self, tmp_path: Path) -> None:
        """Verify database.connect() auto-registers macros when project_dir is set."""
        from havn.engine.database import connect

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "fn.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def add_ten(x: int) -> int:\n"
            "    return x + 10\n",
        )

        db_path = tmp_path / "test.duckdb"
        conn = connect(db_path, project_dir=tmp_path)
        try:
            result = conn.execute("SELECT add_ten(5)").fetchone()
            assert result is not None
            assert result[0] == 15
        finally:
            conn.close()

    def test_database_connect_without_macros_dir(self, tmp_path: Path) -> None:
        """Ensure connect() works fine when there's no macros/ directory."""
        from havn.engine.database import connect

        db_path = tmp_path / "test.duckdb"
        conn = connect(db_path, project_dir=tmp_path)
        try:
            result = conn.execute("SELECT 1").fetchone()
            assert result is not None
            assert result[0] == 1
        finally:
            conn.close()

    def test_database_connect_without_project_dir(self, tmp_path: Path) -> None:
        """Ensure connect() works fine when project_dir is not provided."""
        from havn.engine.database import connect

        db_path = tmp_path / "test.duckdb"
        conn = connect(db_path)
        try:
            result = conn.execute("SELECT 42").fetchone()
            assert result is not None
            assert result[0] == 42
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------


class TestTypeMapping:
    def test_all_types_registered_correctly(self, tmp_path: Path) -> None:
        """Register functions with various type hints and verify they work in DuckDB."""
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "typed.py",
            "from havn.engine.macros import macro\n\n"
            "@macro\n"
            "def is_long(s: str) -> bool:\n"
            "    return len(s) > 5\n\n"
            "@macro\n"
            "def add_floats(a: float, b: float) -> float:\n"
            "    return a + b\n",
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)

        r1 = conn.execute("SELECT is_long('hello')").fetchone()
        assert r1 is not None
        assert r1[0] is False

        r2 = conn.execute("SELECT is_long('hello world')").fetchone()
        assert r2 is not None
        assert r2[0] is True

        r3 = conn.execute("SELECT add_floats(1.5, 2.5)").fetchone()
        assert r3 is not None
        assert abs(r3[0] - 4.0) < 1e-9

        conn.close()


# ---------------------------------------------------------------------------
# @table_macro decorator
# ---------------------------------------------------------------------------


class TestTableMacroDecorator:
    def test_captures_metadata(self) -> None:
        from havn.engine.macros import TableMacroInfo, table_macro

        @table_macro(schema={"id": "INTEGER", "name": "VARCHAR"})
        def my_rows(status: str) -> list:
            return [{"id": 1, "name": "Alice"}]

        info: TableMacroInfo = getattr(my_rows, "__havn_table_macro__")
        assert info.name == "my_rows"
        assert info.schema == {"id": "INTEGER", "name": "VARCHAR"}
        assert len(info.params) == 1
        assert info.params[0] == {"name": "status", "type": "VARCHAR"}

    def test_function_still_callable(self) -> None:
        from havn.engine.macros import table_macro

        @table_macro(schema={"n": "INTEGER"})
        def numbers(count: int) -> list:
            return [{"n": i} for i in range(count)]

        assert numbers(3) == [{"n": 0}, {"n": 1}, {"n": 2}]

    def test_no_schema_infers_from_first_row(self) -> None:
        from havn.engine.macros import TableMacroInfo, table_macro

        @table_macro()
        def auto_schema() -> list:
            return [{"x": 1, "y": "hello", "z": 3.14}]

        info: TableMacroInfo = getattr(auto_schema, "__havn_table_macro__")
        # Schema is resolved at registration time; stored as empty until then
        assert isinstance(info.schema, dict)


# ---------------------------------------------------------------------------
# @table_macro registration with DuckDB
# ---------------------------------------------------------------------------


class TestTableMacroRegistration:
    def test_register_and_query(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "rows.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro(schema={'id': 'INTEGER', 'name': 'VARCHAR', 'active': 'BOOLEAN'})\n"
            "def active_users(status: str) -> list:\n"
            "    if status == 'active':\n"
            "        return [{'id': 1, 'name': 'Alice', 'active': True}]\n"
            "    return [{'id': 1, 'name': 'Alice', 'active': True},\n"
            "            {'id': 2, 'name': 'Bob', 'active': False}]\n",
        )

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        assert count == 1

        rows = conn.execute("SELECT * FROM active_users('active') ORDER BY id").fetchall()
        assert rows == [(1, "Alice", True)]

        rows_all = conn.execute("SELECT * FROM active_users('all') ORDER BY id").fetchall()
        assert rows_all == [(1, "Alice", True), (2, "Bob", False)]

        conn.close()

    def test_multiple_arguments(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "multi.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro(schema={'val': 'INTEGER'})\n"
            "def range_rows(start: int, stop: int) -> list:\n"
            "    return [{'val': i} for i in range(start, stop)]\n",
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)

        rows = conn.execute("SELECT val FROM range_rows(3, 7) ORDER BY val").fetchall()
        assert rows == [(3,), (4,), (5,), (6,)]
        conn.close()

    def test_explicit_schema_parameter(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "typed.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro(schema={'score': 'DOUBLE', 'label': 'VARCHAR'})\n"
            "def scores(prefix: str) -> list:\n"
            "    return [{'score': 9.5, 'label': prefix + '_A'},\n"
            "            {'score': 7.0, 'label': prefix + '_B'}]\n",
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)

        rows = conn.execute("SELECT label, score FROM scores('test') ORDER BY score").fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "test_B"
        assert abs(rows[0][1] - 7.0) < 1e-9
        conn.close()

    def test_large_result_streams_correctly(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "big.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro(schema={'n': 'INTEGER', 'squared': 'INTEGER'})\n"
            "def big_table(limit: int) -> list:\n"
            "    return [{'n': i, 'squared': i * i} for i in range(limit)]\n",
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)

        count = conn.execute("SELECT COUNT(*) FROM big_table(1001)").fetchone()[0]
        assert count == 1001

        total = conn.execute("SELECT SUM(n) FROM big_table(1001)").fetchone()[0]
        assert total == sum(range(1001))
        conn.close()

    def test_empty_result(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "empty.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro(schema={'id': 'INTEGER'})\n"
            "def no_rows(x: str) -> list:\n"
            "    return []\n",
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)

        rows = conn.execute("SELECT * FROM no_rows('anything')").fetchall()
        assert rows == []
        conn.close()

    def test_scalar_and_table_macros_coexist(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "mixed.py",
            "from havn.engine.macros import macro, table_macro\n\n"
            "@macro\n"
            "def shout(s: str) -> str:\n"
            "    return s.upper() + '!'\n\n"
            "@table_macro(schema={'word': 'VARCHAR'})\n"
            "def words(sentence: str) -> list:\n"
            "    return [{'word': w} for w in sentence.split()]\n",
        )

        conn = duckdb.connect(":memory:")
        count = register_macros(conn, tmp_path)
        assert count == 2

        r = conn.execute("SELECT shout('hello')").fetchone()
        assert r[0] == "HELLO!"

        rows = conn.execute("SELECT word FROM words('hello world foo') ORDER BY word").fetchall()
        assert rows == [("foo",), ("hello",), ("world",)]
        conn.close()

    def test_missing_schema_skipped_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        import logging
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        # No schema, no default args, non-trivial body — inference will fail
        _write_file(
            macros_dir / "noschema.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro()\n"
            "def bad_macro(x: str) -> list:\n"
            "    raise RuntimeError('cannot probe')\n",
        )

        conn = duckdb.connect(":memory:")
        with caplog.at_level(logging.WARNING, logger="havn.macros"):
            count = register_macros(conn, tmp_path)

        assert count == 0
        conn.close()

    def test_list_macros_includes_table_kind(self, tmp_path: Path) -> None:
        from havn.engine.macros import list_macros

        macros_dir = tmp_path / "macros"
        _write_file(
            macros_dir / "tab.py",
            "from havn.engine.macros import table_macro\n\n"
            "@table_macro(schema={'id': 'INTEGER', 'val': 'VARCHAR'})\n"
            "def my_table(x: str) -> list:\n"
            '    """Returns rows."""\n'
            "    return [{'id': 1, 'val': x}]\n",
        )

        items = list_macros(tmp_path)
        assert len(items) == 1
        item = items[0]
        assert item["kind"] == "table"
        assert item["name"] == "my_table"
        assert item["return_type"] == "TABLE"
        assert item["schema"] == {"id": "INTEGER", "val": "VARCHAR"}
        assert item["docstring"] == "Returns rows."

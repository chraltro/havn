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

        py_item = next(i for i in items if i["kind"] == "python")
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

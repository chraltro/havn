"""Tests for macro hot-reload: file-watcher triggers re-registration on live connections.

Each test uses real DuckDB in-memory connections and a real FileWatcher thread.
The 2-second debounce means tests that rely on watchdog events sleep ~2.5s.
Total test budget: < 30s.
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scalar_macro_src(fn_name: str, body_expr: str) -> str:
    """Generate source for a single @macro Python function."""
    return (
        "from havn.engine.macros import macro\n\n"
        "@macro\n"
        f"def {fn_name}(x: str) -> str:\n"
        f"    return {body_expr}\n"
    )


def _table_macro_src(fn_name: str, row_expr: str) -> str:
    """Generate source for a single @table_macro Python function."""
    return (
        "from havn.engine.macros import table_macro\n\n"
        "@table_macro(schema={'val': 'VARCHAR'})\n"
        f"def {fn_name}(x: str) -> list:\n"
        f"    return [{row_expr}]\n"
    )


# ---------------------------------------------------------------------------
# Re-registration idempotency (no watcher, just calling register_macros twice)
# ---------------------------------------------------------------------------


class TestIdempotentRegistration:
    """register_macros must be safely callable multiple times on the same connection."""

    def test_scalar_reregister_updates_behaviour(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write(macros_dir / "fn.py", _scalar_macro_src("greet", "'hello ' + x"))

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)
        assert conn.execute("SELECT greet('world')").fetchone()[0] == "hello world"

        # Edit the file and re-register on the same connection.
        _write(macros_dir / "fn.py", _scalar_macro_src("greet", "'hi ' + x"))
        register_macros(conn, tmp_path)

        result = conn.execute("SELECT greet('world')").fetchone()[0]
        assert result == "hi world", f"Expected 'hi world', got {result!r}"
        conn.close()

    def test_scalar_reregister_does_not_raise(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write(macros_dir / "fn.py", _scalar_macro_src("echo", "x"))

        conn = duckdb.connect(":memory:")
        for _ in range(3):
            register_macros(conn, tmp_path)  # must not raise

        assert conn.execute("SELECT echo('ok')").fetchone()[0] == "ok"
        conn.close()

    def test_table_macro_reregister_updates_behaviour(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write(
            macros_dir / "rows.py",
            _table_macro_src("get_rows", "{'val': 'v1'}"),
        )

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)
        rows = conn.execute("SELECT val FROM get_rows('x')").fetchall()
        assert rows == [("v1",)]

        # Change return value and re-register.
        _write(
            macros_dir / "rows.py",
            _table_macro_src("get_rows", "{'val': 'v2'}"),
        )
        register_macros(conn, tmp_path)

        rows2 = conn.execute("SELECT val FROM get_rows('x')").fetchall()
        assert rows2 == [("v2",)], f"Expected [('v2',)], got {rows2}"
        conn.close()

    def test_table_macro_reregister_does_not_raise(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write(
            macros_dir / "rows.py",
            _table_macro_src("my_rows", "{'val': 'ok'}"),
        )

        conn = duckdb.connect(":memory:")
        for _ in range(3):
            register_macros(conn, tmp_path)

        rows = conn.execute("SELECT val FROM my_rows('x')").fetchall()
        assert rows == [("ok",)]
        conn.close()

    def test_sql_macro_reregister_does_not_raise(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write(macros_dir / "math.sql", "CREATE MACRO add_one(x) AS x + 1;\n")

        conn = duckdb.connect(":memory:")
        for _ in range(3):
            register_macros(conn, tmp_path)

        assert conn.execute("SELECT add_one(9)").fetchone()[0] == 10
        conn.close()

    def test_adding_new_macro_file_on_reload(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros

        macros_dir = tmp_path / "macros"
        _write(macros_dir / "fn.py", _scalar_macro_src("fn_a", "'a'"))

        conn = duckdb.connect(":memory:")
        register_macros(conn, tmp_path)
        assert conn.execute("SELECT fn_a('')").fetchone()[0] == "a"

        # Add a second file and reload.
        _write(macros_dir / "fn2.py", _scalar_macro_src("fn_b", "'b'"))
        register_macros(conn, tmp_path)

        assert conn.execute("SELECT fn_a('')").fetchone()[0] == "a"
        assert conn.execute("SELECT fn_b('')").fetchone()[0] == "b"
        conn.close()


# ---------------------------------------------------------------------------
# FileWatcher integration (uses watchdog + real debounce)
# ---------------------------------------------------------------------------


class TestFileWatcherMacroReload:
    """FileWatcher detects macro file changes and re-registers via callback."""

    # Debounce is 2 s; we give watchdog 1 extra second of headroom.
    WAIT = 3.5
    # Brief pause after starting the watcher so watchdog's observer thread
    # is ready before we write files.
    STARTUP_WAIT = 0.5

    def _make_watcher(self, tmp_path: Path, reload_calls: list):
        """Create and start a FileWatcher whose macro callback appends to reload_calls."""
        from havn.engine.scheduler import FileWatcher

        # macros/ directory must exist before the watcher starts so watchdog
        # schedules a watch on it.
        (tmp_path / "macros").mkdir(exist_ok=True)

        def _on_macro_change(project_dir: Path) -> None:
            reload_calls.append(project_dir)

        watcher = FileWatcher(tmp_path, on_macro_change=_on_macro_change)
        watcher.start()
        # Give the observer thread time to start and schedule the directory watch.
        time.sleep(self.STARTUP_WAIT)
        return watcher

    def test_watcher_fires_callback_on_py_change(self, tmp_path: Path) -> None:
        reload_calls: list = []
        watcher = self._make_watcher(tmp_path, reload_calls)
        try:
            _write(
                tmp_path / "macros" / "fn.py",
                _scalar_macro_src("fn_x", "'x'"),
            )
            time.sleep(self.WAIT)
            assert len(reload_calls) >= 1, "Callback not called after .py write"
        finally:
            watcher.stop()
            watcher.join(timeout=5)

    def test_watcher_fires_callback_on_sql_change(self, tmp_path: Path) -> None:
        # Pre-create a SQL file so the watcher sees a modification event.
        _write(
            tmp_path / "macros" / "math.sql",
            "CREATE MACRO sq(x) AS x * x;\n",
        )

        reload_calls: list = []
        watcher = self._make_watcher(tmp_path, reload_calls)
        try:
            # Overwrite with new content to trigger on_modified.
            _write(
                tmp_path / "macros" / "math.sql",
                "CREATE MACRO sq(x) AS x * x * 2;\n",
            )
            time.sleep(self.WAIT)
            assert len(reload_calls) >= 1, "Callback not called after .sql write"
        finally:
            watcher.stop()
            watcher.join(timeout=5)

    def test_watcher_does_not_fire_for_non_macro_dirs(self, tmp_path: Path) -> None:
        """Changes to transform/ must not invoke the macro reload callback."""
        (tmp_path / "transform" / "bronze").mkdir(parents=True, exist_ok=True)

        reload_calls: list = []
        watcher = self._make_watcher(tmp_path, reload_calls)
        try:
            _write(
                tmp_path / "transform" / "bronze" / "model.sql",
                "SELECT 1 AS x",
            )
            time.sleep(self.WAIT)
            assert reload_calls == [], (
                f"Macro callback fired for transform/ change: {reload_calls}"
            )
        finally:
            watcher.stop()
            watcher.join(timeout=5)


# ---------------------------------------------------------------------------
# End-to-end: watcher changes file → macro behaviour changes in live conn
# ---------------------------------------------------------------------------


class TestEndToEndHotReload:
    """Full loop: write file → watcher fires → macro executes new logic."""

    WAIT = 3.5
    STARTUP_WAIT = 0.5

    def test_scalar_macro_hot_reload_e2e(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros
        from havn.engine.scheduler import FileWatcher

        macros_dir = tmp_path / "macros"
        macros_dir.mkdir()

        conn = duckdb.connect(":memory:")

        def _reload(project_dir: Path) -> None:
            register_macros(conn, project_dir)

        _write(macros_dir / "fn.py", _scalar_macro_src("tag", "'v1:' + x"))
        register_macros(conn, tmp_path)
        assert conn.execute("SELECT tag('a')").fetchone()[0] == "v1:a"

        watcher = FileWatcher(tmp_path, on_macro_change=_reload)
        watcher.start()
        time.sleep(self.STARTUP_WAIT)
        try:
            _write(macros_dir / "fn.py", _scalar_macro_src("tag", "'v2:' + x"))
            time.sleep(self.WAIT)

            result = conn.execute("SELECT tag('a')").fetchone()[0]
            assert result == "v2:a", (
                f"Scalar macro not reloaded: expected 'v2:a', got {result!r}"
            )
        finally:
            watcher.stop()
            watcher.join(timeout=5)
            conn.close()

    def test_table_macro_hot_reload_e2e(self, tmp_path: Path) -> None:
        from havn.engine.macros import register_macros
        from havn.engine.scheduler import FileWatcher

        macros_dir = tmp_path / "macros"
        macros_dir.mkdir()

        conn = duckdb.connect(":memory:")

        def _reload(project_dir: Path) -> None:
            register_macros(conn, project_dir)

        _write(macros_dir / "rows.py", _table_macro_src("get_tag", "{'val': 'r1'}"))
        register_macros(conn, tmp_path)
        assert conn.execute("SELECT val FROM get_tag('x')").fetchall() == [("r1",)]

        watcher = FileWatcher(tmp_path, on_macro_change=_reload)
        watcher.start()
        time.sleep(self.STARTUP_WAIT)
        try:
            _write(macros_dir / "rows.py", _table_macro_src("get_tag", "{'val': 'r2'}"))
            time.sleep(self.WAIT)

            rows = conn.execute("SELECT val FROM get_tag('x')").fetchall()
            assert rows == [("r2",)], (
                f"Table macro not reloaded: expected [('r2',)], got {rows}"
            )
        finally:
            watcher.stop()
            watcher.join(timeout=5)
            conn.close()

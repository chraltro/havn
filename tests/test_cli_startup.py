"""Startup cost of ``import havn`` / ``import havn.cli``.

Every CLI invocation imports ``havn.cli``, and user macro and model files
import ``havn``. These tests pin what those imports must *not* load, so a
top-level import of a heavy dependency (duckdb, pydantic via havn.config,
yaml, ...) in the package root or a CLI module shows up as a failure instead
of a slow ``havn --help``. They check ``sys.modules`` in a fresh interpreter,
not timings, so they are deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# Modules that cost real time to import and that no command needs before it
# runs. Each is imported inside the function that uses it.
HEAVY_MODULES = [
    "duckdb",
    "yaml",
    "sqlglot",
    "fastapi",
    "starlette",
    "uvicorn",
    "pyarrow",
    "numpy",
    "pandas",
    "polars",
    "pydantic",
    "sqlfluff",
    "jinja2",
    "huey",
    "httpx",
    "requests",
    "importlib.metadata",
    "email",
    "http.client",
    "urllib.request",
    "tarfile",
    "zipfile",
    "tempfile",
    "havn.config",
    "havn.engine",
    "havn.engine.macros",
    "havn.engine.model_decorator",
]

# The command tree as registered before startup imports were made lazy.
# A command missing from this list after a change means a CLI module stopped
# registering (for example an import moved out of a module that was only
# imported for its side effect). New commands may be added freely.
BASELINE_COMMANDS = [
    'ask', 'assertions', 'backup', 'backup-list', 'backup-restore', 'backup-verify',
    'branch', 'branch build', 'branch clean', 'branch diff', 'branch list',
    'branch reset', 'branch status', 'cdc', 'changes', 'changes apply',
    'changes discard', 'changes list', 'changes show', 'changes verify', 'check',
    'checkpoint', 'ci', 'ci comment', 'ci diff-comment', 'ci generate', 'clear',
    'connect', 'connectors', 'connectors available', 'connectors list',
    'connectors regenerate', 'connectors remove', 'connectors sync', 'connectors test',
    'context', 'contracts', 'debug', 'deploy', 'diff', 'env', 'explain', 'export',
    'flight', 'freshness', 'history', 'impact', 'ingest', 'init', 'jobs',
    'jobs disable', 'jobs enable', 'jobs history', 'jobs preview', 'jobs run',
    'lineage', 'lint', 'live', 'live advance', 'live pause', 'live resume',
    'live status', 'ls', 'macros', 'mask', 'mcp', 'metrics', 'metrics list',
    'metrics query', 'metrics sql', 'migrate', 'packages', 'packages install',
    'packages list', 'packages remove', 'perf', 'pii', 'poll', 'poll once',
    'poll start', 'poll status', 'poll stop', 'pr', 'pr approve', 'pr build',
    'pr close', 'pr comment', 'pr create', 'pr diff', 'pr merge', 'pr request-changes',
    'pr review', 'pr show', 'profile', 'promote', 'query', 'rename-column', 'reports',
    'reports list', 'reports preview', 'reports send', 'restore', 'rewind', 'rls',
    'run', 'schedule', 'secrets', 'secrets delete', 'secrets list', 'secrets set',
    'seed', 'sentinel', 'serve', 'shell', 'snapshot', 'snapshot create',
    'snapshot delete', 'snapshot list', 'status', 'stream', 'tables', 'test',
    'transform', 'users', 'users create', 'users delete', 'users list', 'validate',
    'version', 'watch',
]


def _run_python(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter against the source tree under test."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, *args, "-c", code] if code else [sys.executable, *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )


def _loaded_after(import_stmt: str) -> dict:
    code = (
        "import json, sys\n"
        f"{import_stmt}\n"
        "import havn\n"
        "print(json.dumps({'file': havn.__file__, "
        f"'loaded': [m for m in {HEAVY_MODULES!r} if m in sys.modules]}}))\n"
    )
    proc = _run_python(code)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # Guard against measuring the editable install of another checkout.
    assert Path(result["file"]).resolve().is_relative_to(SRC)
    return result


@pytest.mark.parametrize("import_stmt", ["import havn", "import havn.cli"])
def test_import_does_not_load_heavy_modules(import_stmt):
    result = _loaded_after(import_stmt)
    assert result["loaded"] == [], (
        f"`{import_stmt}` loaded {result['loaded']}; import them inside the "
        "function that needs them"
    )


def test_public_names_still_resolve():
    code = (
        "import havn\n"
        "from havn import macro, table_macro, model, setup_logging\n"
        "import havn.engine.macros as m, havn.engine.model_decorator as d\n"
        "assert macro is m.macro and table_macro is m.table_macro\n"
        "assert model is d.model and havn.model is d.model\n"
        "assert havn.macro is macro and callable(havn.setup_logging)\n"
        "assert isinstance(havn.__version__, str) and havn.__version__\n"
        "from havn import __version__\n"
        "assert __version__ == havn.__version__\n"
        "assert {'macro', 'table_macro', 'model', '__version__', 'setup_logging'} <= set(dir(havn))\n"
        "ns = {}\n"
        "exec('from havn import *', ns)\n"
        "assert {'macro', 'table_macro', 'model', 'setup_logging'} <= set(ns)\n"
        "try:\n"
        "    havn.no_such_attribute\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('missing attribute did not raise')\n"
        "print('ok')\n"
    )
    proc = _run_python(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_decorators_work_after_lazy_import():
    code = (
        "from havn import macro, model\n"
        "@macro\n"
        "def double(x: int) -> int:\n"
        "    return x * 2\n"
        "@model(materialized='table')\n"
        "def m(ref):\n"
        "    return None\n"
        "assert double(2) == 4\n"
        "print('ok')\n"
    )
    proc = _run_python(code)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(flag):
    import havn

    proc = _run_python("", "-m", "havn", flag)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"havn {havn.__version__}"


def test_version_falls_back_without_metadata(monkeypatch):
    import importlib.metadata

    import havn

    def _missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    assert havn._read_version() == "0.0.0+unknown"


def _command_paths() -> set[str]:
    import click
    import typer

    from havn.cli import app

    def walk(cmd: click.Command, prefix: str = "") -> set[str]:
        out: set[str] = set()
        for name, sub in getattr(cmd, "commands", {}).items():
            path = f"{prefix} {name}".strip()
            out.add(path)
            # Duck-typed: newer Typer groups are not click.Group subclasses.
            if getattr(sub, "commands", None):
                out |= walk(sub, path)
        return out

    return walk(typer.main.get_command(app))


def test_every_baseline_command_is_registered():
    missing = sorted(set(BASELINE_COMMANDS) - _command_paths())
    assert missing == []


def test_help_lists_every_top_level_command():
    proc = _run_python("", "-m", "havn", "--help")
    assert proc.returncode == 0, proc.stderr
    for name in (c for c in BASELINE_COMMANDS if " " not in c):
        assert name in proc.stdout, f"{name} missing from havn --help"


def test_unknown_environment_from_a_command_exits_1_with_a_message(capsys):
    """``_HavnTyper.__call__`` turns UnknownEnvironmentError into exit 1.

    CliRunner calls the click command directly and never reaches
    ``__call__``, so this drives the Typer object the way the ``havn``
    entry point does.
    """
    import pytest

    from havn.cli import _HavnTyper
    from havn.config import UnknownEnvironmentError

    cli = _HavnTyper()

    @cli.command()
    def boom() -> None:
        raise UnknownEnvironmentError("Unknown environment 'prdo'")

    with pytest.raises(SystemExit) as exc:
        cli(args=[], standalone_mode=False)
    assert exc.value.code == 1
    assert "Unknown environment 'prdo'" in capsys.readouterr().out


def test_other_errors_from_a_command_are_not_swallowed():
    import pytest

    from havn.cli import _HavnTyper

    cli = _HavnTyper()

    @cli.command()
    def boom() -> None:
        raise RuntimeError("not an environment problem")

    with pytest.raises(RuntimeError, match="not an environment problem"):
        cli(args=[], standalone_mode=False)

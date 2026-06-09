"""Helpers for guiding users to install missing third-party Python packages.

havn is frequently installed as an isolated tool (``uv tool install havn`` or
``pipx install havn``). In that case the project's ingest/export scripts and
notebook cells run inside havn's own virtualenv, so a bare ``import pandas``
fails with ``ModuleNotFoundError`` and there is no obvious place to add the
dependency. These helpers turn that raw error into an actionable hint that
matches however the running copy of havn was installed.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _install_method() -> str:
    """Best-effort guess at how the running havn was installed.

    Returns one of ``"uv-tool"``, ``"pipx"``, or ``"pip"``. The guess is based
    on where the interpreter lives; it is only used to phrase a suggestion, so
    a wrong guess is harmless (we always also mention the generic fallback).
    """
    prefix = Path(sys.prefix).as_posix().lower()
    if "/uv/tools/" in prefix or "uv/tools" in prefix:
        return "uv-tool"
    if "/pipx/venvs/" in prefix or "pipx/venvs" in prefix:
        return "pipx"
    return "pip"


def missing_module_hint(module: str) -> str:
    """Return a one-line, copy-pasteable hint for installing ``module``.

    ``module`` is the top-level package name from the failed import (e.g.
    ``"pandas"`` for ``import pandas.core``).
    """
    pkg = (module or "").split(".")[0] or "the package"
    method = _install_method()

    if method == "uv-tool":
        return (
            f"havn looks installed via uv. Add the dependency with:\n"
            f"    uv tool install havn --with {pkg}\n"
            f"(re-run the same command later with extra --with flags to add more)."
        )
    if method == "pipx":
        return (
            f"havn looks installed via pipx. Add the dependency with:\n"
            f"    pipx inject havn {pkg}"
        )
    return (
        f"Install the dependency into havn's environment with:\n"
        f"    {Path(sys.executable).name} -m pip install {pkg}"
    )


def module_name_from_error(exc: BaseException) -> str | None:
    """Extract the missing module name from a ModuleNotFoundError/ImportError."""
    name = getattr(exc, "name", None)
    if name:
        return name
    # Fall back to parsing the message: "No module named 'pandas'"
    msg = str(exc)
    if "No module named" in msg:
        tail = msg.split("No module named", 1)[1].strip()
        return tail.strip("'\" ").split(".")[0] or None
    return None


def augment_import_error(text: str, exc: BaseException) -> str:
    """Append an install hint to ``text`` when ``exc`` is a missing-module error.

    ``text`` is the formatted traceback/error string shown to the user. The hint
    is appended only when the *top-level* package genuinely can't be imported, so
    we don't tell users to ``pip install os`` for ``from os import nope`` (a plain
    ImportError on an existing package) or ``pip install json`` for
    ``import json.nope`` (a bad submodule of an installed package). Otherwise the
    text is returned unchanged.
    """
    if not isinstance(exc, ModuleNotFoundError):
        return text
    module = module_name_from_error(exc)
    if not module:
        return text
    top = module.split(".")[0]
    # If the top-level package resolves, the real problem is a bad submodule or
    # symbol path, not a missing install — don't suggest installing it.
    import importlib.util
    try:
        if importlib.util.find_spec(top) is not None:
            return text
    except (ImportError, ValueError, ModuleNotFoundError):
        pass  # find_spec itself failed → treat as genuinely missing
    hint = missing_module_hint(top)
    return f"{text}\n\nhavn: '{top}' is not installed. {hint}"

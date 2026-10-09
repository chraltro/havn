"""Python models: ``transform/<layer>/<name>.py`` files as DAG nodes.

A Python model is a file in ``transform/`` that defines a function returning
a DuckDB relation, a pandas or polars DataFrame, or a pyarrow Table::

    # transform/silver/customer_scores.py
    from havn import model

    @model(materialized="table", tags=["daily"], assertions=["unique(customer_id)"])
    def customer_scores(db, ref):
        orders = ref("bronze.orders")
        return orders.aggregate("customer_id, sum(amount) AS score")

Everything about the model that the DAG needs is read *statically*, from the
file's syntax tree, here at discovery time: the ``@model(...)`` keywords, the
``ref("...")`` calls (its dependencies), the local helper modules it imports
and a fingerprint of its code for change detection. Nothing runs until the
model is built, and then it runs in-process on the build connection, like a
macro, under the same hard and idle timeouts as an ingest script.

The result is staged into a TEMP table, and the existing SQL writers do the
rest: a table is ``CREATE OR REPLACE TABLE ... AS SELECT * FROM <staged>``,
an incremental model goes through the same delete+insert / merge / append
strategies and ``on_schema_change`` policies, and a snapshot through the same
SCD2 merge. That is the whole trick, and it is why assertions, profiling,
the run log, blocking, rewind snapshots and contracts need no Python case.

``view`` and ``ephemeral`` are refused: both are stored SQL that some other
statement reads later, and a Python model's rows only exist once its
function has run. ``microbatch`` is refused too: it substitutes window
bounds into SQL text, which a function does not have.
"""

from __future__ import annotations

import ast
import hashlib
import io
import logging
import re
import sys
import threading
import time
import traceback
import types
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .models import PythonModelInfo, SQLModel, ValidationError

if TYPE_CHECKING:
    import duckdb

logger = logging.getLogger("havn.transform")


# ---------------------------------------------------------------------------
# What a Python model may say
# ---------------------------------------------------------------------------

# The @config keys that mean the same thing on a Python model, plus the
# directives SQL spells as their own lines (@description, @col, @assert,
# @grain, @owner, @depends_on) and the script timeouts.
PYTHON_CONFIG_KEYS = frozenset({
    "schema",
    "materialized",
    "unique_key",
    "incremental_strategy",
    "incremental_filter",
    "partition_by",
    "watermark",
    "on_schema_change",
    "tags",
    "strategy",
    "updated_at",
    "check_cols",
    "hard_deletes",
    "description",
    "columns",
    "assertions",
    "grain",
    "owner",
    "depends_on",
    "timeout",
    "idle_timeout",
})

# Keys that describe the model without changing what it builds. They are
# removed from the fingerprint, exactly as @description, @col, @owner and
# tags are left out of a SQL model's hash: retagging must not rebuild.
_METADATA_KEYS = frozenset({
    "tags", "description", "columns", "owner", "timeout", "idle_timeout",
})

PYTHON_MATERIALIZATIONS = ("table", "incremental", "snapshot")

# What the model function can ask for, by parameter name.
PYTHON_MODEL_PARAMS = ("db", "ref", "this", "is_incremental")

# Default timeouts: the same as an ingest script's.
DEFAULT_TIMEOUT_SECONDS = 7200

# A file that does not parse is still taken for a model when it plainly means
# to be one, so the syntax error is reported instead of the file vanishing
# from the DAG.
_LOOKS_LIKE_MODEL = re.compile(r"^\s*(?:@(?:\w+\.)?model\b|def\s+model\s*\()", re.MULTILINE)

_SQL_METHODS = frozenset({"sql", "execute", "query"})


class PythonModelError(RuntimeError):
    """A Python model could not be built.

    The message starts with the file and line in the user's code where it
    went wrong, then the user's own frames, so the run log and the console
    point at the line to fix rather than at havn's internals. ``output``
    holds whatever the function printed before it failed.
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


# ---------------------------------------------------------------------------
# Static reading
# ---------------------------------------------------------------------------


def _decorator_target(dec: ast.expr) -> tuple[bool, ast.Call | None]:
    """Whether ``dec`` is ``@model`` / ``@model(...)`` / ``@havn.model(...)``."""
    call = dec if isinstance(dec, ast.Call) else None
    func = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(func, ast.Name) and func.id == "model":
        return True, call
    if isinstance(func, ast.Attribute) and func.attr == "model":
        return True, call
    return False, None


def _find_model_function(
    tree: ast.Module,
) -> tuple[ast.FunctionDef | None, ast.Call | None, list[tuple[str, int | None]]]:
    """The model function, its ``@model(...)`` call (if any), and problems."""
    errors: list[tuple[str, int | None]] = []
    decorated: list[tuple[ast.FunctionDef, ast.Call | None]] = []
    plain: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                if _decorator_target(dec)[0]:
                    errors.append((
                        f"@model function '{node.name}' is async; a Python model "
                        "must be a plain function", node.lineno,
                    ))
            continue
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            is_model, call = _decorator_target(dec)
            if is_model:
                decorated.append((node, call))
                break
        else:
            if node.name == "model":
                plain = node
    if len(decorated) > 1:
        names = ", ".join(f"'{fn.name}' (line {fn.lineno})" for fn, _ in decorated)
        errors.append((
            f"more than one @model function: {names}. A file defines one model; "
            "split the others into their own files", decorated[1][0].lineno,
        ))
    if decorated:
        return decorated[0][0], decorated[0][1], errors
    return plain, None, errors


def _literal_config(
    call: ast.Call | None,
) -> tuple[dict[str, Any], list[tuple[str, int | None]]]:
    """``@model(...)`` keywords as Python values, read without running them."""
    config: dict[str, Any] = {}
    errors: list[tuple[str, int | None]] = []
    if call is None:
        return config, errors
    if call.args:
        errors.append((
            "@model takes keyword arguments only, e.g. @model(materialized=\"table\")",
            call.lineno,
        ))
    for kw in call.keywords:
        if kw.arg is None:
            errors.append((
                "@model(**...) cannot be read without running the file; "
                "write each setting out as a literal keyword", call.lineno,
            ))
            continue
        try:
            config[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, TypeError):
            errors.append((
                f"@model {kw.arg}= must be a literal value (a string, number, "
                "list or dict). havn reads it without running the file, so it "
                "cannot be a variable or an expression", kw.value.lineno,
            ))
    return config, errors


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]


def _code_fingerprint(tree: ast.Module, metadata_call_ids: set[int] | None = None) -> str:
    """sha256 of the code that runs, ignoring comments, layout and docstrings.

    ``ast.dump`` without attributes carries no line numbers, so moving code
    around, reformatting it or editing a comment leaves the fingerprint
    alone. Metadata-only keywords are removed from the ``@model(...)`` call
    (identified by ``id`` in the *original* tree, which is why the copy is
    made by re-walking in step).
    """
    import copy

    clone = copy.deepcopy(tree)
    if metadata_call_ids:
        originals = [n for n in ast.walk(tree)]
        copies = [n for n in ast.walk(clone)]
        for orig, dup in zip(originals, copies):
            if id(orig) in metadata_call_ids and isinstance(dup, ast.Call):
                dup.keywords = [k for k in dup.keywords if k.arg not in _METADATA_KEYS]
    _strip_docstrings(clone)
    return hashlib.sha256(ast.dump(clone).encode()).hexdigest()[:16]


def _local_imports(tree: ast.Module) -> list[str]:
    """Top-level names of every absolute import in the file, in order."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _resolve_helper(name: str, search_path: list[Path]) -> Path | None:
    for directory in search_path:
        candidate = directory / f"{name}.py"
        if candidate.is_file():
            return candidate
    return None


def _helper_fingerprints(
    tree: ast.Module, search_path: list[Path], transform_dir: Path
) -> tuple[str, list[str]]:
    """Fingerprint the local helper modules a model imports, transitively.

    A helper is a ``.py`` file next to the model or at the transform root
    that the model imports by name. Conventionally it starts with ``_`` so
    discovery never mistakes it for a model, though any non-model file
    works. Its fingerprint follows the same rules as the model's own.
    """
    from havn.textio import read_project_text

    found: dict[Path, str] = {}
    todo = [(n, list(search_path)) for n in _local_imports(tree)]
    while todo:
        name, where = todo.pop()
        path = _resolve_helper(name, where)
        if path is None or path in found:
            continue
        try:
            text = read_project_text(path)
            helper_tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, ValueError):
            # Unreadable or broken: hash the bytes so an edit still shows, and
            # let the import itself report the error at build time.
            try:
                found[path] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError:
                pass
            continue
        found[path] = _code_fingerprint(helper_tree)
        nested = [path.parent, *[d for d in search_path if d != path.parent]]
        todo.extend((n, nested) for n in _local_imports(helper_tree))
    if not found:
        return "", []

    def rel(p: Path) -> str:
        try:
            return p.relative_to(transform_dir).as_posix()
        except ValueError:
            return p.name

    entries = sorted(f"{rel(p)}:{fp}" for p, fp in found.items())
    digest = hashlib.sha256("|".join(entries).encode()).hexdigest()[:16]
    return digest, sorted(rel(p) for p in found)


def _ref_calls(
    tree: ast.Module,
) -> tuple[list[str], list[int]]:
    """Literal ``ref("schema.name")`` arguments, and lines of dynamic ones."""
    literal: list[str] = []
    dynamic: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "ref"):
            continue
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            literal.append(arg.value.strip().lower())
        else:
            dynamic.append(node.lineno)
    return literal, dynamic


def _sql_string_refs(tree: ast.Module, own: str) -> dict[str, int]:
    """Tables named in literal SQL handed to ``.sql()`` / ``.execute()``.

    ``db.sql("SELECT * FROM silver.orders")`` reads a model without going
    through ``ref``. Ordering still has to be right, so the reference becomes
    a dependency; validation then suggests ``ref`` (see
    :func:`python_validation_errors`), because only ``ref`` is redirected by
    defer and replaced by unit-test mocks.
    """
    from havn.engine.sql_analysis import extract_table_refs

    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _SQL_METHODS or not node.args:
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        for ref in extract_table_refs(arg.value, exclude=own):
            found.setdefault(ref, node.lineno)
    return found


def _as_list(value: Any, key: str, errors: list, line: int | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    errors.append((f"@model {key}= must be a string or a list of strings", line))
    return []


def _as_str(value: Any, key: str, errors: list, line: int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and key == "unique_key":
        if all(isinstance(v, str) for v in value):
            return ", ".join(v.strip() for v in value)
    if isinstance(value, str):
        return value
    errors.append((f"@model {key}= must be a string", line))
    return None


def _as_seconds(value: Any, key: str, errors: list, line: int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        errors.append((f"@model {key}= must be a number of seconds (0 or more)", line))
        return None
    return float(value)


def _assertion_specs(assertions: list[str]) -> list[tuple[str, str]]:
    """``"no_nulls(x), severity=warn"`` -> ``("no_nulls(x)", "warn")``.

    The same trailing qualifier ``@assert`` lines take, parsed by the same
    function, so the two spellings cannot drift apart.
    """
    from havn.engine.sql_analysis import parse_assertion_specs

    specs: list[tuple[str, str]] = []
    for text in assertions:
        parsed = parse_assertion_specs(f"@assert {text}")
        specs.extend(parsed or [(text, "error")])
    return specs


def looks_like_python_model(text: str) -> bool:
    """Cheap check used on files that do not parse."""
    return bool(_LOOKS_LIKE_MODEL.search(text))


def build_python_model(
    path: Path,
    text: str,
    transform_dir: Path,
) -> SQLModel | None:
    """The model a ``transform/**/*.py`` file defines, or None if it is not one.

    A ``.py`` file is a model when it has a function decorated with
    ``@model`` (or ``@havn.model``), or, failing that, a top-level ``def
    model``. Anything else is a helper module and is left alone, as are
    ``_``-prefixed files, which discovery never passes in.

    A file that is a model but has problems (a syntax error, a non-literal
    ``@model`` argument, an unknown key) still comes back as a model carrying
    ``python.errors``: it stays in the DAG, ``havn validate`` lists the
    problems, and building it fails with the first one.

    Raises:
        ValueError: the schema or model name is not a safe identifier, the
            same rule SQL discovery enforces.
    """
    from havn.engine.utils import validate_identifier

    rel = path.relative_to(transform_dir)
    folder_schema = rel.parent.name if rel.parent.name else "public"
    name = path.stem.lower()
    search_path = [path.parent]
    if transform_dir not in search_path:
        search_path.append(transform_dir)

    errors: list[tuple[str, int | None]] = []
    warnings: list[tuple[str, int | None]] = []

    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        if not looks_like_python_model(text):
            return None
        schema = folder_schema.lower()
        validate_identifier(schema, f"schema for {path.name}")
        validate_identifier(name, f"model name for {path.name}")
        info = PythonModelInfo(
            fingerprint=hashlib.sha256(text.encode()).hexdigest()[:16],
            search_path=search_path,
            errors=[(f"SyntaxError: {e.msg}", e.lineno)],
        )
        return SQLModel(
            path=path, name=name, schema=schema, full_name=f"{schema}.{name}",
            sql=text, query="", materialized="table", python=info,
        )

    fn, call, fn_errors = _find_model_function(tree)
    if fn is None and not fn_errors:
        return None
    errors.extend(fn_errors)

    config, config_errors = _literal_config(call)
    errors.extend(config_errors)
    line = call.lineno if call is not None else (fn.lineno if fn else None)

    for key in config:
        if key not in PYTHON_CONFIG_KEYS:
            import difflib

            close = difflib.get_close_matches(key, sorted(PYTHON_CONFIG_KEYS), n=1, cutoff=0.6)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            errors.append((f"Unknown @model key '{key}'.{hint}", line))

    raw_schema = config.get("schema")
    schema = (raw_schema if isinstance(raw_schema, str) and raw_schema else folder_schema).lower()
    validate_identifier(schema, f"schema for {path.name}")
    validate_identifier(name, f"model name for {path.name}")
    full_name = f"{schema}.{name}"

    materialized = config.get("materialized", "table")
    if materialized in ("view", "ephemeral"):
        errors.append((
            f"a Python model cannot be materialized as {materialized}: a "
            f"{materialized} is stored SQL that is read later, and a Python "
            "model's rows only exist once its function has run. Use table, "
            "incremental or snapshot", line,
        ))
        materialized = "table"
    elif materialized not in PYTHON_MATERIALIZATIONS:
        errors.append((
            f"Unknown materialization '{materialized}'. A Python model supports "
            f"{', '.join(PYTHON_MATERIALIZATIONS)}", line,
        ))
        materialized = "table"

    strategy = _as_str(config.get("incremental_strategy"), "incremental_strategy", errors, line)
    if strategy == "microbatch":
        errors.append((
            "incremental_strategy=microbatch is not supported for Python models: "
            "it substitutes {start} and {end} into SQL text. Use delete+insert, "
            "merge or append, and filter on is_incremental / this yourself", line,
        ))
        strategy = None

    params: list[str] = []
    takes_kwargs = False
    if fn is not None:
        args = fn.args
        params = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
        takes_kwargs = args.kwarg is not None
        if args.vararg is not None:
            errors.append((
                f"the model function cannot take *{args.vararg.arg}; havn passes "
                f"arguments by name ({', '.join(PYTHON_MODEL_PARAMS)})", fn.lineno,
            ))
        for p in params:
            if p not in PYTHON_MODEL_PARAMS:
                errors.append((
                    f"the model function asks for '{p}', which havn does not "
                    f"provide. It can take any of: {', '.join(PYTHON_MODEL_PARAMS)}",
                    fn.lineno,
                ))

    description = config.get("description")
    if description is not None and not isinstance(description, str):
        errors.append(("@model description= must be a string", line))
        description = None
    if description is None:
        # The function's docstring, else the module's: either is where a
        # Python author would naturally say what the model is.
        description = (ast.get_docstring(fn) if fn is not None else None) or ast.get_docstring(tree) or ""
    description = (description or "").strip()

    columns = config.get("columns") or {}
    if not (isinstance(columns, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in columns.items()
    )):
        errors.append(("@model columns= must be a dict of column name to description", line))
        columns = {}

    assertions = _as_list(config.get("assertions"), "assertions", errors, line)
    if isinstance(config.get("assertions"), str):
        # One string is one assertion, commas and all: `unique(a, b)`.
        assertions = [config["assertions"].strip()]
    grain = _as_list(config.get("grain"), "grain", errors, line)
    tags = _as_list(config.get("tags"), "tags", errors, line)
    explicit = [d.lower() for d in _as_list(config.get("depends_on"), "depends_on", errors, line)]
    owner = _as_str(config.get("owner"), "owner", errors, line) or ""

    literal_refs, dynamic_refs = _ref_calls(tree)
    for lineno in dynamic_refs:
        warnings.append((
            "ref() with an argument that is not a string literal cannot be seen "
            "without running the file; list the model it reads in "
            "@model(depends_on=[...]) or ref() will refuse it", lineno,
        ))
    sql_refs = _sql_string_refs(tree, own=full_name)

    depends: list[str] = []
    for dep in [*explicit, *literal_refs, *sql_refs]:
        if dep and dep != full_name and dep not in depends:
            depends.append(dep)
    for dep in literal_refs:
        if dep.count(".") != 1:
            errors.append((
                f"ref('{dep}') must name a model or table as schema.name", None,
            ))
    referenced_by_ref = set(literal_refs) | set(explicit)
    for dep, lineno in sql_refs.items():
        if dep not in referenced_by_ref:
            warnings.append((
                f"{dep} is read through literal SQL rather than ref(). It is "
                "still a dependency, but defer does not redirect it and unit "
                "test mocks do not replace it; ref(\"" + dep + "\") does both",
                lineno,
            ))

    metadata_calls = {id(call)} if call is not None else set()
    helper_hash, helpers = _helper_fingerprints(tree, search_path, transform_dir)

    info = PythonModelInfo(
        function=fn.name if fn is not None else "",
        params=params,
        takes_kwargs=takes_kwargs,
        fingerprint=_code_fingerprint(tree, metadata_calls),
        helper_hash=helper_hash,
        helpers=helpers,
        search_path=search_path,
        timeout=_as_seconds(config.get("timeout"), "timeout", errors, line),
        idle_timeout=_as_seconds(config.get("idle_timeout"), "idle_timeout", errors, line),
        errors=errors,
        warnings=warnings,
    )

    def opt(key: str) -> str | None:
        return _as_str(config.get(key), key, errors, line)

    return SQLModel(
        path=path,
        name=name,
        schema=schema,
        full_name=full_name,
        sql=text,
        query="",
        materialized=materialized,
        depends_on=depends,
        description=description,
        column_docs=dict(columns),
        assertions=[e for e, _ in _assertion_specs(assertions)],
        assertion_specs=_assertion_specs(assertions),
        unique_key=opt("unique_key"),
        incremental_strategy=strategy or "delete+insert",
        incremental_filter=opt("incremental_filter"),
        partition_by=opt("partition_by"),
        watermark=opt("watermark"),
        on_schema_change=opt("on_schema_change") or "append_new_columns",
        strategy=opt("strategy") or "check",
        updated_at=opt("updated_at"),
        check_cols=opt("check_cols"),
        hard_deletes=opt("hard_deletes") or "ignore",
        grain=grain,
        owner=owner,
        tags=tags,
        python=info,
    )


def _missing_imports(model: SQLModel) -> list[tuple[str, int | None]]:
    """Modules the file imports that are neither installed nor local helpers.

    Found with ``importlib.util.find_spec``, which locates a module without
    importing it, so validation still never runs the user's code. Only the
    first component of each import is checked: ``import a.b`` needs ``a``.
    """
    import importlib.util

    if model.python is None:
        return []
    try:
        tree = ast.parse(model.sql)
    except SyntaxError:
        return []
    problems: list[tuple[str, int | None]] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(a.name.split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [(node.module.split(".")[0], node.lineno)]
        else:
            continue
        for name, lineno in names:
            if name in seen:
                continue
            seen.add(name)
            if _resolve_helper(name, model.python.search_path) is not None:
                continue
            try:
                found = importlib.util.find_spec(name) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                problems.append((
                    f"imports '{name}', which is not installed in this Python "
                    "environment (and is not a helper module next to the model "
                    "or at the transform root)", lineno,
                ))
    return problems


def python_validation_errors(model: SQLModel) -> list[ValidationError]:
    """What is wrong with a Python model, found without running it.

    Discovery's findings (syntax, ``@model`` keys and values, the function's
    parameters, dynamic ``ref()`` calls) plus imports that would fail.
    """
    if model.python is None:
        return []
    out = [
        ValidationError(model=model.full_name, severity="error", message=msg, line=line)
        for msg, line in [*model.python.errors, *_missing_imports(model)]
    ]
    out.extend(
        ValidationError(model=model.full_name, severity="warning", message=msg, line=line)
        for msg, line in model.python.warnings
    )
    if not model.python.errors and not model.python.function:
        out.append(ValidationError(
            model=model.full_name, severity="error",
            message="no model function found: decorate one with @model or name it model",
        ))
    return out


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

# Serialises loading model modules, which briefly puts the model's directory
# and the transform root on sys.path so ``import _helpers`` resolves.
_LOAD_LOCK = threading.RLock()


def _under(path: str, roots: list[Path]) -> bool:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            p.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _load_module(model: SQLModel) -> types.ModuleType:
    """Execute the model file as a fresh module and return it.

    Fresh on every build, so an edit is picked up without a restart. Local
    helpers imported by the file are dropped from ``sys.modules`` afterwards
    for the same reason (and so two folders' ``_utils`` never shadow each
    other); the loaded module keeps its own references to them.
    """
    assert model.python is not None
    info = model.python
    roots = [p for p in info.search_path]
    module_name = f"_havn_model_{model.schema}__{model.name}"
    module = types.ModuleType(module_name)
    module.__file__ = str(model.path)
    code = compile(model.sql, str(model.path), "exec")

    with _LOAD_LOCK:
        added = [str(p) for p in roots if str(p) not in sys.path]
        sys.path[:0] = added
        before = set(sys.modules)
        dont_write = sys.dont_write_bytecode
        # No __pycache__ appearing in the user's transform/ folders.
        sys.dont_write_bytecode = True
        sys.modules[module_name] = module
        try:
            exec(code, module.__dict__)
        finally:
            sys.dont_write_bytecode = dont_write
            sys.modules.pop(module_name, None)
            for p in added:
                try:
                    sys.path.remove(p)
                except ValueError:
                    pass
            for name in set(sys.modules) - before:
                mod = sys.modules.get(name)
                file = getattr(mod, "__file__", None)
                if file and _under(file, roots):
                    sys.modules.pop(name, None)
    return module


def _quote_ref(name: str) -> str:
    from havn.engine.utils import validate_identifier

    parts = name.split(".")
    for part in parts:
        validate_identifier(part, f"ref name {name!r}")
    return ".".join(parts)


def make_ref(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    *,
    model_map: dict[str, SQLModel] | None = None,
    query_rewriter: Callable[[str], str] | None = None,
    ref_map: dict[str, str] | None = None,
) -> Callable[[str], Any]:
    """The ``ref`` a model function receives.

    ``ref("silver.orders")`` returns a lazy DuckDB relation over that model
    or table. It only accepts names the DAG already knows the model depends
    on (literal ``ref`` calls and ``depends_on``), so a dependency can never
    be hidden from the ordering. Through it:

    - an ephemeral upstream is inlined, exactly as in a SQL consumer;
    - a deferred run reads an unbuilt upstream from the defer target;
    - a unit test's mock replaces the upstream (``ref_map``).
    """
    allowed = {d.lower() for d in model.depends_on}
    aliases = model.python.ref_aliases if model.python is not None else {}

    def ref(name: str) -> Any:
        if not isinstance(name, str):
            raise TypeError(f"ref() takes a 'schema.name' string, not {type(name).__name__}")
        key = name.strip().lower()
        key = aliases.get(key, key)
        if key not in allowed:
            raise PythonModelError(
                f"ref({name!r}) is not a declared dependency of {model.full_name}. "
                "havn reads ref() calls without running the file, so the name "
                "has to be a string literal; for a name built at run time, "
                "list it in @model(depends_on=[...])."
            )
        if ref_map is not None:
            target = ref_map.get(key)
            if target is None:
                raise PythonModelError(f"no mock for {key} in this unit test")
            return conn.sql(f"SELECT * FROM {target}")
        sql = f"SELECT * FROM {_quote_ref(key)}"
        upstream = (model_map or {}).get(key)
        if upstream is not None and upstream.materialized == "ephemeral":
            from .inline import inline_ephemeral

            probe = SQLModel(
                path=model.path, name=model.name, schema=model.schema,
                full_name=model.full_name, sql=sql, query=sql,
                materialized="table", depends_on=[key],
            )
            sql = inline_ephemeral(probe, model_map or {})
        if query_rewriter is not None:
            sql = query_rewriter(sql)
        return conn.sql(sql)

    return ref


def staging_name(model: SQLModel) -> str:
    """The TEMP table a Python model's result is staged into."""
    from havn.engine.utils import validate_identifier

    validate_identifier(model.name, "staging table name")
    return f"_havn_py_{model.name}"


_SUPPORTED_RESULTS = (
    "a DuckDB relation (ref(...), db.sql(...)), a pandas or polars DataFrame, "
    "or a pyarrow Table"
)


def _stage_result(conn: duckdb.DuckDBPyConnection, model: SQLModel, result: Any, staged: str) -> None:
    """Write whatever the function returned into the TEMP table ``staged``.

    Everything goes through ``conn.register``, which DuckDB accepts for its
    own relations, pandas, pyarrow and (via arrow) polars. A relation from a
    different connection cannot be registered here and is copied through
    arrow instead.
    """
    import duckdb

    if result is None:
        raise PythonModelError(
            f"Python model {model.full_name}: the function returned None. "
            f"Return {_SUPPORTED_RESULTS}."
        )
    obj = result
    if isinstance(obj, duckdb.DuckDBPyRelation):
        pass
    elif type(obj).__module__.split(".")[0] == "polars" and hasattr(obj, "to_arrow"):
        obj = obj.to_arrow()
    elif type(obj).__module__.split(".")[0] in ("pandas", "pyarrow"):
        pass
    else:
        raise PythonModelError(
            f"Python model {model.full_name}: the function returned "
            f"{type(result).__name__}. Return {_SUPPORTED_RESULTS}."
        )

    source = f"{staged}_src"
    try:
        conn.register(source, obj)
    except duckdb.InvalidInputException as e:
        if isinstance(obj, duckdb.DuckDBPyRelation) and "another Connection" in str(e):
            obj = obj.to_arrow_table()
            conn.register(source, obj)
        else:
            raise
    try:
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {staged} AS SELECT * FROM {source}")
    finally:
        try:
            conn.unregister(source)
        except Exception:
            pass


def _format_failure(model: SQLModel, exc: BaseException) -> str:
    """The error as the user should read it: their file, their line, first.

    Frames from havn and the libraries it calls are dropped; what is left is
    the model file and its helpers. The headline names the deepest of those
    frames, which is the line to look at.
    """
    roots = list(model.python.search_path) if model.python else [model.path.parent]

    def show(filename: str) -> str:
        for root in roots[::-1]:
            try:
                return Path(filename).resolve().relative_to(root.resolve().parent).as_posix()
            except (ValueError, OSError):
                continue
        return filename

    frames = [f for f in traceback.extract_tb(exc.__traceback__) if _under(f.filename, roots)]
    if isinstance(exc, SyntaxError) and exc.filename and exc.lineno:
        # A helper that does not parse (the model file itself is parsed at
        # discovery). format_exception_only shows the line and the caret.
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        where = f"{show(exc.filename)}:{exc.lineno}"
        head = detail.splitlines()[-1]
        return f"Python model {model.full_name} failed at {where}: {head}\n{detail}"
    if isinstance(exc, PythonModelError):
        if not frames:
            return str(exc)
        detail = str(exc)
    else:
        # The bare class name: "CatalogException: ..." reads better than
        # "_duckdb.CatalogException: ...", and builtins print the same.
        detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
    if not frames:
        return f"Python model {model.full_name} failed: {detail}"
    last = frames[-1]
    lines = [f"Python model {model.full_name} failed at {show(last.filename)}:{last.lineno}: {detail}"]
    lines.append("Traceback (your code, most recent call last):")
    for f in frames:
        lines.append(f'  File "{show(f.filename)}", line {f.lineno}, in {f.name}')
        if f.line:
            lines.append(f"    {f.line.strip()}")
    return "\n".join(lines)


def _target_exists(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            "AND table_schema = ? AND table_name = ? AND table_type = 'BASE TABLE'",
            [model.schema, model.name],
        ).fetchone()
    except Exception:
        return False
    return bool(row and row[0])


def run_python_model(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    *,
    model_map: dict[str, SQLModel] | None = None,
    query_rewriter: Callable[[str], str] | None = None,
    ref_map: dict[str, str] | None = None,
    output: list[str] | None = None,
    is_incremental: bool | None = None,
) -> str:
    """Run the model's function and stage its result. Returns the TEMP table.

    Runs in a supervised thread with the script runner's hard and idle
    timeouts (``@model(timeout=..., idle_timeout=...)``); a DuckDB query in
    flight is interrupted on timeout. Whatever the function prints, to
    stdout or stderr, is appended to ``output`` either way, and carried on
    the :class:`PythonModelError` when it fails.

    ``is_incremental`` defaults to: the model is incremental and its target
    table exists in this warehouse, which is when the SQL writers would
    merge rather than create.
    """
    staged = staging_name(model)

    def stage(result: Any) -> str:
        _stage_result(conn, model, result, staged)
        return staged

    return _invoke(
        conn, model, stage,
        model_map=model_map, query_rewriter=query_rewriter, ref_map=ref_map,
        output=output, is_incremental=is_incremental,
    )


# A preview is an interactive request; it never gets the two hours a build may.
PREVIEW_TIMEOUT_SECONDS = 120


def preview_python_model(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    *,
    model_map: dict[str, SQLModel] | None = None,
    limit: int = 100,
) -> dict:
    """Run the function and return its first ``limit`` rows, writing nothing.

    The editor's Preview for a Python model. Nothing is staged: a relation
    is read through ``limit``, a DataFrame or Table through ``head`` /
    ``slice``, so this works on a read-only connection and leaves no TEMP
    table behind. ``is_incremental`` is False, as for any preview: it shows
    what a full build would produce.

    Returns ``{"columns", "rows", "truncated", "output"}`` with rows as
    plain Python values (the caller serialises them).
    """
    def head(result: Any) -> dict:
        import duckdb

        if result is None:
            raise PythonModelError(
                f"Python model {model.full_name}: the function returned None. "
                f"Return {_SUPPORTED_RESULTS}."
            )
        obj = result
        top = type(obj).__module__.split(".")[0]
        if top == "polars" and hasattr(obj, "to_arrow"):
            obj, top = obj.to_arrow(), "pyarrow"
        if isinstance(obj, duckdb.DuckDBPyRelation):
            rel = obj.limit(limit + 1)
            return {"columns": list(rel.columns), "rows": [list(r) for r in rel.fetchall()]}
        if top == "pandas" and hasattr(obj, "head"):
            frame = obj.head(limit + 1)
            return {
                "columns": [str(c) for c in frame.columns],
                "rows": [list(r) for r in frame.itertuples(index=False, name=None)],
            }
        if top == "pyarrow" and hasattr(obj, "slice"):
            table = obj.slice(0, limit + 1)
            names = list(table.column_names)
            return {"columns": names, "rows": [[row[n] for n in names] for row in table.to_pylist()]}
        raise PythonModelError(
            f"Python model {model.full_name}: the function returned "
            f"{type(result).__name__}. Return {_SUPPORTED_RESULTS}."
        )

    printed: list[str] = []
    data = _invoke(
        conn, model, head,
        model_map=model_map, output=printed, is_incremental=False,
        timeout_cap=PREVIEW_TIMEOUT_SECONDS,
    )
    truncated = len(data["rows"]) > limit
    return {
        "columns": data["columns"],
        "rows": data["rows"][:limit],
        "truncated": truncated,
        "output": "".join(printed),
    }


def _invoke(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    consume: Callable[[Any], Any],
    *,
    model_map: dict[str, SQLModel] | None = None,
    query_rewriter: Callable[[str], str] | None = None,
    ref_map: dict[str, str] | None = None,
    output: list[str] | None = None,
    is_incremental: bool | None = None,
    timeout_cap: float | None = None,
) -> Any:
    """Load the model file, call its function, hand the result to ``consume``.

    Everything from loading to ``consume`` runs in the supervised thread, so
    a lazy relation evaluated by ``consume`` is covered by the timeout too.
    Returns whatever ``consume`` returned.
    """
    from havn.engine.runner import _capture_thread_output, _run_supervised

    info = model.python
    if info is None:
        raise ValueError(f"{model.full_name} is not a Python model")
    if info.errors:
        msg, line = info.errors[0]
        where = f"{model.path.name}:{line}" if line else model.path.name
        raise PythonModelError(f"Python model {model.full_name} ({where}): {msg}")
    if not info.function:
        raise PythonModelError(
            f"Python model {model.full_name}: no model function found; "
            "decorate one with @model or name it model"
        )

    if is_incremental is None:
        is_incremental = model.materialized == "incremental" and _target_exists(conn, model)
    provided = {
        "db": conn,
        "ref": make_ref(
            conn, model, model_map=model_map, query_rewriter=query_rewriter, ref_map=ref_map,
        ),
        "this": model.full_name,
        "is_incremental": bool(is_incremental),
    }
    if info.takes_kwargs:
        kwargs = dict(provided)
    else:
        kwargs = {p: provided[p] for p in info.params if p in provided}

    stdout_buf, stderr_buf = io.StringIO(), io.StringIO()

    def target() -> Any:
        with _capture_thread_output(stdout_buf, stderr_buf):
            module = _load_module(model)
            fn = getattr(module, info.function, None)
            if not callable(fn):
                raise PythonModelError(
                    f"Python model {model.full_name}: '{info.function}' is not a function "
                    "once the file has run"
                )
            return consume(fn(**kwargs))

    timeout = info.timeout if info.timeout else DEFAULT_TIMEOUT_SECONDS
    if timeout_cap is not None:
        timeout = min(timeout, timeout_cap)
    started = time.perf_counter()
    sup = _run_supervised(
        conn, f"model:{model.full_name}", target,
        timeout=timeout,
        activity=lambda: (len(stdout_buf.getvalue()), len(stderr_buf.getvalue())),
        idle_timeout=info.idle_timeout,
    )
    printed = stdout_buf.getvalue() + stderr_buf.getvalue()
    if output is not None and printed:
        output.append(printed)

    if sup["reason"] is not None:
        elapsed = int(time.perf_counter() - started)
        if sup["reason"] == "idle":
            msg = (
                f"Python model {model.full_name} appears stuck: no output and no "
                f"DuckDB activity for {sup['idle_timeout']:g}s ({elapsed}s in all). "
                "Raise it with @model(idle_timeout=<seconds>), or 0 to turn the check off"
            )
        else:
            msg = (
                f"Python model {model.full_name} timed out after {timeout:g}s. "
                "Raise it with @model(timeout=<seconds>)"
            )
        if sup["orphaned"]:
            msg += (
                ". It could not be stopped and is still running in the background "
                "on this connection"
            )
        raise PythonModelError(msg, output=printed)
    if sup["error"] is not None:
        raise PythonModelError(_format_failure(model, sup["error"]), output=printed) from sup["error"]
    return sup["value"]


def notebook_cell(model: SQLModel) -> dict:
    """A notebook code cell that runs a Python model against live tables.

    The model's source, then a call of its function with a ``ref`` that
    reads the warehouse directly, so the result shows as the cell's output.
    Used where a SQL model gets a SQL cell holding its query (model to
    notebook, the debug notebook).
    """
    info = model.python
    fn = info.function if info is not None and info.function else "model"
    if info is not None and info.takes_kwargs:
        invoke = f"result = {fn}(**_args)"
    else:
        names = tuple(info.params) if info is not None else ()
        invoke = f"result = {fn}(**{{k: v for k, v in _args.items() if k in {names!r}}})"
    call = (
        f"_args = dict(db=db, ref=ref, this={model.full_name!r}, is_incremental=False)\n"
        + invoke
    )
    source = (
        model.sql.rstrip()
        + "\n\n\n# --- Run the model function against the warehouse ---\n"
        + "def ref(name):\n"
        + "    return db.sql(f\"SELECT * FROM {name}\")\n\n"
        + call
        + "\nresult\n"
    )
    return {"type": "code", "source": source, "outputs": []}


def staged_model(model: SQLModel, staged: str) -> SQLModel:
    """A SQL stand-in for ``model`` that selects from its staged result.

    Handed to the ordinary SQL writers, which then neither know nor care
    that the rows came from Python. Not a model anybody stores: the original
    keeps its hashes, its state row and its place in the DAG.
    """
    query = f"SELECT * FROM {staged}"
    return replace(model, query=query, sql=query, python=None)


def drop_staged(conn: duckdb.DuckDBPyConnection, staged: str) -> None:
    try:
        conn.execute(f"DROP TABLE IF EXISTS {staged}")
    except Exception as e:
        logger.debug("Could not drop staged Python result %s: %s", staged, e)

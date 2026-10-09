"""Model discovery, DAG building, and change detection."""

from __future__ import annotations

from havn.textio import read_project_text

import hashlib
import logging
import re
from dataclasses import replace
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from havn.engine.sql_analysis import (
    extract_table_refs,
    parse_assertion_specs,
    parse_assertions,
    parse_column_docs,
    parse_config,
    parse_depends,
    parse_description,
    parse_grain,
    parse_owner,
    parse_source_freshness,
    parse_sql,
    strip_config_comments,
)
from havn.engine.live.settings import parse_duration, parse_live_flag
from havn.engine.utils import validate_identifier

from .columns import save_model_columns
from .models import SQLModel

if TYPE_CHECKING:
    from havn.config import ProjectConfig
    from havn.engine.packages import PackageRoot

logger = logging.getLogger(__name__)


class DuplicateModelError(ValueError):
    """Two SQL files produce the same ``schema.name``.

    Raised rather than reported, because the project has no single answer for
    what that name means: ``build_dag`` keys models by full name, so one of
    the two files would be dropped and which one won depended on filename
    order. Discovery already raises for a model it cannot use (an invalid
    schema or model identifier), so this follows the same path.
    """


def discover_models(transform_dir: Path) -> list[SQLModel]:
    """Discover all SQL models in the transform directory.

    Convention: folder names map to schemas.
    transform/bronze/customers.sql -> schema=bronze, name=customers

    Raises:
        DuplicateModelError: two files resolve to the same ``schema.name``.
        ValueError: a file's schema or model name is not a safe identifier.
    """
    models = []
    if not transform_dir.exists():
        return models

    # full_name -> the file that claimed it first
    claimed: dict[str, Path] = {}

    def claim(full_name: str, path: Path) -> None:
        previous = claimed.get(full_name)
        if previous is not None:
            raise DuplicateModelError(
                f"Duplicate model '{full_name}': both "
                f"{previous} and {path} produce it. "
                "Rename one of the files, or point one at another schema "
                "with @config schema=."
            )
        claimed[full_name] = path

    # One sorted pass over both kinds, so a .sql and a .py claiming the same
    # name are reported the same way two .sql files are.
    files = sorted([*transform_dir.rglob("*.sql"), *_python_model_files(transform_dir)])
    for sql_file in files:
        if sql_file.suffix == ".py":
            from .python_models import build_python_model

            py_model = build_python_model(sql_file, read_project_text(sql_file), transform_dir)
            if py_model is None:
                continue  # a helper module, not a model
            claim(py_model.full_name, sql_file)
            models.append(py_model)
            continue

        sql = read_project_text(sql_file)
        config = parse_config(sql)
        depends = parse_depends(sql)
        description = parse_description(sql)
        column_docs = parse_column_docs(sql)
        assertions = parse_assertions(sql)
        assertion_specs = parse_assertion_specs(sql)
        grain = parse_grain(sql)
        owner = parse_owner(sql)
        source_freshness = parse_source_freshness(sql)
        query = strip_config_comments(sql)
        # Parsed once here and handed to the model below, so validation, the
        # deny-rule check and column lineage reuse this tree instead of
        # parsing the same SQL again.
        ast = parse_sql(query)
        folder_schema_tmp = sql_file.relative_to(transform_dir).parent.name or "public"
        own_schema_tmp = config.get("schema", folder_schema_tmp).lower()
        own_name_tmp = sql_file.stem.lower()
        auto_refs = extract_table_refs(
            query, exclude=f"{own_schema_tmp}.{own_name_tmp}", ast=ast
        )
        if depends:
            merged = list(depends)
            seen = set(depends)
            for ref in auto_refs:
                if ref not in seen:
                    merged.append(ref)
                    seen.add(ref)
            depends = merged
        else:
            depends = auto_refs

        # Schema from folder name (convention) or config override
        rel = sql_file.relative_to(transform_dir)
        folder_schema = rel.parent.name if rel.parent.name else "public"
        # Lowercased, because DuckDB identifiers are case-insensitive and every
        # reference is compared lowercased (extract_table_refs, parse_depends):
        # ``transform/silver/Customers.sql`` read as ``silver.Customers`` while
        # ``FROM silver.Customers`` was recorded as ``silver.customers``, so
        # the edge between them was lost and the order came out wrong.
        schema = config.get("schema", folder_schema).lower()
        name = sql_file.stem.lower()
        # Validate identifiers at discovery time to prevent SQL injection downstream
        validate_identifier(schema, f"schema for {sql_file.name}")
        validate_identifier(name, f"model name for {sql_file.name}")

        full_name = f"{schema}.{name}"
        claim(full_name, sql_file)

        materialized = config.get("materialized", "view")
        unique_key = config.get("unique_key")
        incremental_strategy = config.get("incremental_strategy", "delete+insert")
        incremental_filter = config.get("incremental_filter")
        partition_by = config.get("partition_by")
        watermark = config.get("watermark")
        on_schema_change = config.get("on_schema_change", "append_new_columns")
        tags = [t.strip() for t in config.get("tags", "").split(",") if t.strip()]
        # Snapshot (SCD2) settings. Read for every model so `validate_models`
        # can complain about them on a model that is not a snapshot; execution
        # only looks at them when materialized=snapshot.
        strategy = config.get("strategy", "check")
        updated_at = config.get("updated_at")
        check_cols = config.get("check_cols")
        hard_deletes = config.get("hard_deletes", "ignore")
        # Microbatch settings. `lookback` is kept as an int here so the model
        # carries a usable value; a non-numeric one is reported by
        # `validate_models` rather than crashing discovery.
        event_time = config.get("event_time")
        batch_size = config.get("batch_size")
        begin = config.get("begin")
        raw_lookback = config.get("lookback")
        try:
            lookback = int(raw_lookback) if raw_lookback is not None else 1
        except ValueError:
            lookback = 1
        # Live settings. A malformed value is kept off the model (not live, no
        # interval) and reported by `validate_models`, never a discovery crash.
        live = parse_live_flag(config.get("live"))
        try:
            live_interval = parse_duration(config.get("live_interval") or 0)
        except ValueError:
            live_interval = 0.0
        cdc_op = config.get("cdc_op") or None
        cdc_seq = config.get("cdc_seq") or None
        cdc_deletes = (config.get("cdc_deletes") or "hard").lower()

        model = SQLModel(
            path=sql_file,
            name=name,
            schema=schema,
            full_name=full_name,
            sql=sql,
            query=query,
            materialized=materialized,
            depends_on=depends,
            description=description,
            column_docs=column_docs,
            assertions=assertions,
            assertion_specs=assertion_specs,
            unique_key=unique_key,
            incremental_strategy=incremental_strategy,
            incremental_filter=incremental_filter,
            partition_by=partition_by,
            watermark=watermark,
            on_schema_change=on_schema_change,
            strategy=strategy,
            updated_at=updated_at,
            check_cols=check_cols,
            hard_deletes=hard_deletes,
            event_time=event_time,
            batch_size=batch_size,
            begin=begin,
            lookback=lookback,
            live=live,
            live_interval=live_interval,
            cdc_op=cdc_op,
            cdc_seq=cdc_seq,
            cdc_deletes=cdc_deletes,
            grain=grain,
            owner=owner,
            source_freshness=source_freshness,
            tags=tags,
        )
        if ast is not None:
            model.ast = ast
        models.append(model)

    return models


def _python_model_files(transform_dir: Path) -> list[Path]:
    """``.py`` files under ``transform_dir`` that may define a model.

    ``_``-prefixed files are helpers by convention and never read as models,
    and neither is anything in ``__pycache__`` or a hidden directory.
    Whether a remaining file *is* a model is decided by its content (see
    :func:`havn.engine.transform.python_models.build_python_model`).
    """
    out: list[Path] = []
    for path in transform_dir.rglob("*.py"):
        if path.name.startswith("_"):
            continue
        rel_parts = path.relative_to(transform_dir).parts[:-1]
        if any(p == "__pycache__" or p.startswith(".") for p in rel_parts):
            continue
        out.append(path)
    return out


def discover_package_models(root: PackageRoot) -> list[SQLModel]:
    """Discover one installed package's models, namespaced into the project.

    A package's models are written as if the package were the whole project:
    ``transform/silver/customers.sql`` says ``schema=silver`` and its siblings
    say ``FROM silver.customers``. Both halves are rewritten here, once, at
    discovery time:

    - the schema becomes ``<pkg>_<schema>`` (or whatever the package's
      ``havn_package.yml`` maps it to), so a package can never quietly take a
      name the project was already using;
    - references to the package's *own* models are rewritten to match, so the
      package author never writes the prefix and the project never has to
      care that it exists.

    References to anything else -- ``landing.*``, a table the package expects
    the host project to provide -- are left exactly as written.
    """
    from havn.engine.sql_rewrite import SQLRewriteError, find_table_refs, rewrite_table_refs

    raw = discover_models(root.transform_dir)
    if not raw:
        return []

    mapping: dict[str, str] = {}
    for m in raw:
        # Lowercased like every other model name (see discover_models).
        target_schema = root.schema_for(m.schema).lower()
        validate_identifier(target_schema, f"schema for package '{root.name}'")
        mapping[f"{m.schema}.{m.name}".lower()] = f"{target_schema}.{m.name}"

    models: list[SQLModel] = []
    for m in raw:
        if m.python is not None:
            # No SQL to rewrite. The package's own names are mapped at the
            # other end instead: ref("silver.customers") inside the package
            # resolves through ref_aliases to crm_silver.customers.
            target_schema = root.schema_for(m.schema).lower()
            aliases = {
                d.lower(): mapping[d.lower()] for d in m.depends_on if d.lower() in mapping
            }
            models.append(
                replace(
                    m,
                    schema=target_schema,
                    full_name=f"{target_schema}.{m.name}",
                    depends_on=[mapping.get(d.lower(), d) for d in m.depends_on],
                    package=root.name,
                    python=replace(m.python, ref_aliases=aliases),
                )
            )
            continue
        query = m.query
        try:
            refs = find_table_refs(query)
        except SQLRewriteError:
            # Unparseable SQL cannot be rewritten. Leave it alone: the model
            # will fail its own build with a real error, which is more useful
            # than a rewrite error standing in front of it.
            refs = []
        if any(ref in mapping for ref in refs):
            try:
                query = rewrite_table_refs(query, mapping)
            except SQLRewriteError as exc:
                raise ValueError(
                    f"Package '{root.name}': could not rewrite references in "
                    f"{m.path}: {exc}"
                ) from exc

        target_schema = root.schema_for(m.schema).lower()
        models.append(
            replace(
                m,
                schema=target_schema,
                full_name=f"{target_schema}.{m.name}",
                query=query,
                depends_on=[mapping.get(d.lower(), d) for d in m.depends_on],
                package=root.name,
            )
        )
    return models


def discover_all_models(
    project_dir: Path,
    config: ProjectConfig | None = None,
) -> list[SQLModel]:
    """Every model the project builds: its own, plus each installed package's.

    This is what every caller that runs or lists the DAG should use.
    :func:`discover_models` stays the single-directory primitive underneath
    it, for callers that genuinely mean one directory.

    Packages come from ``havn_packages.lock``, so a project without one pays
    a single ``stat`` and gets the output of ``discover_models``, except that
    models calling a macro from ``macros/`` carry its fingerprint in their
    content hash (see :func:`_apply_macro_hashes`).

    Raises:
        DuplicateModelError: a package model lands on a name the project (or
            an earlier package) already uses. That only happens once a
            package's manifest has overridden the ``<pkg>_`` schema prefix,
            and it is the same error a project would get from two of its own
            files claiming one name.
    """
    from havn.engine.packages import package_roots

    project_dir = Path(project_dir)
    models = discover_models(project_dir / "transform")

    roots = package_roots(project_dir)
    models = _add_package_models(config, models, roots)
    _apply_macro_hashes(models, [project_dir / "macros", *(r.macros_dir for r in roots)])
    return models


_PY_DEF_RE = re.compile(r"^\s*def\s+(\w+)\s*\(", re.MULTILINE)
_SQL_MACRO_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?MACRO\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?(\w+)",
    re.IGNORECASE,
)
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def _closure_fingerprints(
    units: dict[str, list[str]],
    refs: dict[str, set[str]],
    loose: str,
) -> dict[str, str]:
    """One fingerprint per name: its own source, the sources of every
    top-level name it reaches in the same file, and the file's loose code."""
    out: dict[str, str] = {}
    for name in units:
        seen: set[str] = set()
        todo = [name]
        while todo:
            n = todo.pop()
            if n in seen or n not in units:
                continue
            seen.add(n)
            todo.extend(refs.get(n, ()))
        body = "\x00".join(src for n in sorted(seen) for src in units[n])
        out[name] = hashlib.sha256((body + "\x01" + loose).encode()).hexdigest()[:16]
    return out


def _python_macro_fingerprints(text: str) -> dict[str, str] | None:
    """Per-function fingerprints for a macro ``.py`` file, or None if it does
    not parse. Each function covers itself (decorators included), the
    top-level helpers, constants and imports it references, transitively, and
    any loose top-level code; editing one macro leaves the others' alone."""
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    units: dict[str, list[str]] = {}
    refs: dict[str, set[str]] = {}
    loose: list[str] = []

    def segment(node: ast.AST) -> str:
        parts = [ast.get_source_segment(text, d) or "" for d in getattr(node, "decorator_list", [])]
        parts.append(ast.get_source_segment(text, node) or "")
        return "\n".join(parts)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound = [(a.asname or a.name).split(".")[0] for a in node.names]
        else:
            bound = []
        if not bound:
            loose.append(segment(node))
            continue
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        src = segment(node)
        for name in bound:
            units.setdefault(name, []).append(src)
            refs.setdefault(name, set()).update(used)
    fps = _closure_fingerprints(units, refs, "\n".join(loose))
    # Only functions can be macros; helpers and constants were only needed
    # to build the closures above.
    return {
        n.name: fps[n.name]
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _sql_macro_fingerprints(text: str) -> dict[str, str]:
    """Per-macro fingerprints for a macro ``.sql`` file: each ``CREATE MACRO``
    statement, plus the statements of other macros in the file it calls."""
    matches = list(_SQL_MACRO_RE.finditer(text))
    if not matches:
        return {}
    units: dict[str, list[str]] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        units.setdefault(m.group(1).lower(), []).append(text[m.start():end].strip())
    refs = {
        name: {c.lower() for src in srcs for c in _CALL_RE.findall(src)}
        for name, srcs in units.items()
    }
    return _closure_fingerprints(units, refs, text[: matches[0].start()].strip())


def _macro_definitions(macro_dirs: list[Path]) -> dict[str, list[str]]:
    """Map each macro name defined in ``macro_dirs`` to its fingerprints.

    Read statically (no import of user code): every top-level ``def`` in a
    ``.py`` file and every ``CREATE MACRO`` in a ``.sql`` file, skipping
    ``_``-prefixed files as macro registration does. Fingerprints are per
    function (see :func:`_python_macro_fingerprints`), so a model is only
    rebuilt when a function it calls, or something that function uses,
    changed. A ``.py`` file that does not parse falls back to one hash of the
    whole file for every ``def`` in it. A plain helper ``def`` that shares a
    name with something a model calls only costs that model an extra rebuild.
    """
    defs: dict[str, list[str]] = {}
    for macros_dir in macro_dirs:
        if not macros_dir.is_dir():
            continue
        for path in sorted([*macros_dir.glob("*.py"), *macros_dir.glob("*.sql")]):
            if path.name.startswith("_"):
                continue
            try:
                text = read_project_text(path)
            except OSError:
                continue
            if path.suffix == ".py":
                fps = _python_macro_fingerprints(text)
                if fps is None:
                    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
                    fps = {name: digest for name in _PY_DEF_RE.findall(text)}
            else:
                fps = _sql_macro_fingerprints(text)
            for name, fp in fps.items():
                defs.setdefault(name.lower(), []).append(fp)
    return defs


def _apply_macro_hashes(models: list[SQLModel], macro_dirs: list[Path]) -> None:
    """Fold the macros each model calls into its content hash.

    A macro edit changes what a model computes without touching its SQL, so
    change detection skipped it. Only models whose query calls a name some
    macro file defines are affected; every other model's hash is unchanged
    (``macro_hash`` stays "" and is not folded in), and the fingerprint only
    moves when one of those files' contents does.
    """
    defs = _macro_definitions(macro_dirs)
    if not defs:
        return
    for model in models:
        # A Python model has no query; its SQL, if any, is in string literals
        # in the source, which the same scan reaches (an unrelated Python call
        # that happens to share a macro's name only costs an extra rebuild).
        text = model.sql if model.is_python else model.query
        called = {c.lower() for c in _CALL_RE.findall(text)}
        digests = sorted({d for name in called & defs.keys() for d in defs[name]})
        if not digests:
            continue
        model.macro_hash = hashlib.sha256("".join(digests).encode()).hexdigest()[:16]
        model.refresh_content_hash()


def _add_package_models(
    config: ProjectConfig | None,
    models: list[SQLModel],
    roots: list[PackageRoot],
) -> list[SQLModel]:
    """Append each installed package's models to the project's own."""
    if config is not None:
        installed = {r.name for r in roots}
        for declared in getattr(config, "packages", []) or []:
            if declared.name not in installed:
                logger.warning(
                    "Package '%s' is declared in project.yml but not installed; "
                    "run 'havn packages install'",
                    declared.name,
                )
    if not roots:
        return models

    claimed: dict[str, Path] = {m.full_name: m.path for m in models}
    for root in roots:
        for model in discover_package_models(root):
            previous = claimed.get(model.full_name)
            if previous is not None:
                raise DuplicateModelError(
                    f"Duplicate model '{model.full_name}': both {previous} and "
                    f"{model.path} (package '{root.name}') produce it. "
                    f"Remove the schema override in {root.name}'s "
                    "havn_package.yml, or rename the project model."
                )
            claimed[model.full_name] = model.path
            models.append(model)
    return models


class CircularDependencyError(ValueError):
    """The model DAG contains a dependency cycle.

    Raised instead of letting graphlib's CycleError escape as a bare traceback
    through the CLI, the API, and the scheduler.
    """


def _format_cycle(err: CycleError, model_map: dict[str, SQLModel]) -> str:
    """Turn a graphlib CycleError into a message naming the model files."""
    cycle = err.args[1] if len(err.args) > 1 else []
    parts = []
    for name in cycle:
        model = model_map.get(name)
        parts.append(f"{name} ({model.path})" if model else name)
    chain = " -> ".join(parts) if parts else "unknown"
    return (
        "Circular dependency between models: "
        + chain
        + ". Break the cycle by removing one of the references "
        "(check @depends_on lines as well as FROM/JOIN clauses)."
    )


def build_dag(models: list[SQLModel]) -> list[SQLModel]:
    """Sort models in dependency order using topological sort."""
    model_map = {m.full_name: m for m in models}
    sorter: TopologicalSorter[str] = TopologicalSorter()

    for m in models:
        # Filter dependencies to only those that are known models
        # (landing.* tables won't be in the model list — that's fine)
        known_deps = [d for d in m.depends_on if d in model_map]
        sorter.add(m.full_name, *known_deps)

    try:
        ordered = list(sorter.static_order())
    except CycleError as e:
        raise CircularDependencyError(_format_cycle(e, model_map)) from e
    return [model_map[name] for name in ordered if name in model_map]


def _selected_ancestors(
    models: list[SQLModel],
    all_models: list[SQLModel],
) -> dict[str, list[str]]:
    """For each model in ``models``, the nearest ancestors also in ``models``.

    Walks the full DAG and passes through models that are not selected, so
    ``bronze.a -> silver.b -> gold.d`` with only a and d selected still says
    d depends on a. Without this, a subset run put a and d in one parallel
    tier and d read b's view over the old a.
    """
    full = {m.full_name: m for m in all_models}
    selected = {m.full_name for m in models}
    # unselected model -> the selected models reachable upwards from it
    through: dict[str, set[str]] = {}

    def reach(name: str, visiting: set[str]) -> set[str]:
        if name in through:
            return through[name]
        if name in visiting:  # a cycle; build_dag reports it elsewhere
            return set()
        visiting.add(name)
        found: set[str] = set()
        for dep in full[name].depends_on:
            if dep in selected:
                found.add(dep)
            elif dep in full:
                found |= reach(dep, visiting)
        visiting.discard(name)
        through[name] = found
        return found

    out: dict[str, list[str]] = {}
    for m in models:
        deps: set[str] = set()
        for dep in m.depends_on:
            if dep in selected:
                deps.add(dep)
            elif dep in full:
                deps |= reach(dep, set())
        deps.discard(m.full_name)
        out[m.full_name] = sorted(deps)
    return out


def build_dag_tiers(
    models: list[SQLModel],
    all_models: list[SQLModel] | None = None,
) -> list[list[SQLModel]]:
    """Build DAG and return models grouped by execution tier.

    Models within the same tier have no dependencies on each other
    and can execute in parallel.

    ``all_models`` is the whole project when ``models`` is a selection from
    it: two selected models connected only through unselected ones are then
    still ordered (see :func:`_selected_ancestors`).
    """
    model_map = {m.full_name: m for m in models}
    sorter: TopologicalSorter[str] = TopologicalSorter()

    if all_models is not None:
        edges = _selected_ancestors(models, all_models)
    else:
        edges = {
            m.full_name: [d for d in m.depends_on if d in model_map] for m in models
        }
    for m in models:
        sorter.add(m.full_name, *edges[m.full_name])

    try:
        sorter.prepare()
    except CycleError as e:
        raise CircularDependencyError(_format_cycle(e, model_map)) from e
    tiers: list[list[SQLModel]] = []

    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        tier = [model_map[name] for name in ready if name in model_map]
        if tier:
            tiers.append(tier)
        for name in ready:
            sorter.done(name)

    return tiers


def _compute_upstream_hash(model: SQLModel, model_map: dict[str, SQLModel]) -> str:
    """Compute a combined hash of all upstream model content and upstream hashes.

    Includes both content_hash and upstream_hash of each dependency so that
    changes propagate transitively through the full DAG (not just one level).
    Models must be processed in topological order so that upstream_hash is
    already set on dependencies before it is read here.
    """
    if not model.depends_on:
        return ""
    upstream_hashes = []
    for dep in sorted(model.depends_on):
        if dep in model_map:
            dep_model = model_map[dep]
            # Without the macro fingerprint (see SQLModel.definition_hash).
            # A model built by hand with content_hash set directly has no
            # macro_hash, and its content_hash is used as is.
            outside_code = dep_model.macro_hash or (
                dep_model.python is not None and dep_model.python.helper_hash
            )
            upstream_hashes.append(
                dep_model.definition_hash if outside_code else dep_model.content_hash
            )
            upstream_hashes.append(dep_model.upstream_hash)
    return hashlib.sha256("".join(upstream_hashes).encode()).hexdigest()[:16]


def _has_changed(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> bool:
    """Check if a model has changed since last run."""
    result = conn.execute(
        "SELECT content_hash, upstream_hash FROM _havn.model_state WHERE model_path = ?",
        [model.full_name],
    ).fetchone()
    if result is None:
        return True
    old_content_hash, old_upstream_hash = result
    return old_content_hash != model.content_hash or old_upstream_hash != model.upstream_hash


def _needs_build(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> bool:
    """Whether a transform run should build ``model`` rather than skip it.

    Wider than ``_has_changed``, which only says whether the definition
    moved and backs "edited since build" in the UI and the
    ``state:modified`` selector. A model whose last build an error
    assertion rejected has an unchanged definition but must still be
    rebuilt and re-checked, or a rerun would wave it through.
    """
    # Snapshot and incremental models (microbatch included) exist to pick up
    # new source data, and their SQL does not change between runs -- so
    # judging them by their definition skipped them forever: a scheduled run
    # recorded no new history and ingested no new windows. They are built to
    # be re-run cheaply, so they always run.
    if model.materialized == "snapshot":
        return True
    if model.materialized == "incremental" and _rerun_safe(model):
        return True
    return _has_changed(conn, model) or _is_blocked(conn, model) or _inputs_newer(conn, model)


# Run types that load raw data into the warehouse outside the model DAG.
_SOURCE_LOAD_RUN_TYPES = ("ingest", "import", "connector_sync", "seed")


def _inputs_newer(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    """Whether a table model's inputs hold newer data than its last build.

    A table's SQL does not change when its inputs get new rows, so judged by
    its definition alone it stayed on old data until ``--force``:

    - an input model rebuilt in an earlier, narrower run (``havn transform
      silver.x`` refreshes silver.x but not the gold table reading it);
    - a raw source (``landing.*``) reloaded by an ingest, import, connector
      sync or seed.

    A dependency with a ``model_state`` row is an input model and counts when
    it was built after this one. Anything else is a raw source and counts
    when a successful load was logged after this model's build. That half is
    coarse on purpose: the run log says *that* raw data arrived, not which
    table, and fingerprinting every source on every run would mean a full
    scan each time. Data written into ``landing`` outside havn (another tool,
    an ad-hoc query) still needs ``--force``.

    Within one run, ``_parent_built`` already covers parents rebuilt in that
    run. Views read their inputs live and never need this, and a plain
    ``append`` incremental would duplicate rows if re-run, so only tables
    are considered.
    """
    if model.materialized != "table" or not model.depends_on:
        return False
    deps = sorted(model.depends_on)
    try:
        row = conn.execute(
            "SELECT last_run_at FROM _havn.model_state WHERE model_path = ?",
            [model.full_name],
        ).fetchone()
        if row is None or row[0] is None:
            return False  # never built: _has_changed already builds it
        built_at = row[0]
        placeholders = ", ".join("?" for _ in deps)
        # Views and ephemerals hold no data of their own: a view reads its
        # inputs live and is only rebuilt when its SQL changes, an ephemeral
        # is recorded on every run without being built. Their timestamps say
        # nothing about their data, so they are treated like the raw sources
        # they read through to. (A table reached through a view that was
        # rebuilt in a narrower run is not seen this way; --force covers it.)
        dep_rows = conn.execute(
            f"SELECT model_path, last_run_at FROM _havn.model_state "
            f"WHERE model_path IN ({placeholders}) AND materialized_as NOT IN ('ephemeral', 'view')",
            deps,
        ).fetchall()
        if any(ts is not None and ts > built_at for _, ts in dep_rows):
            return True
        if len(dep_rows) == len(deps):
            return False  # every input is a built model, and none is newer
        types = ", ".join("?" for _ in _SOURCE_LOAD_RUN_TYPES)
        hit = conn.execute(
            f"""
            SELECT 1 FROM _havn.run_log
            WHERE status = 'success' AND run_type IN ({types})
              AND started_at + to_milliseconds(CAST(COALESCE(duration_ms, 0) AS BIGINT)) > ?
            LIMIT 1
            """,
            [*_SOURCE_LOAD_RUN_TYPES, built_at],
        ).fetchone()
    except duckdb.CatalogException:
        return False
    return hit is not None


def _rerun_safe(model: SQLModel) -> bool:
    """Whether running an incremental model again without new data is a no-op.

    merge and delete+insert replace by key, microbatch reprocesses only open
    windows, and a filtered append reads only rows newer than the target. A
    plain append with no filter re-inserts everything it selects, so running
    it every time would duplicate rows: it keeps the changed-or-blocked rule.
    """
    strategy = (model.incremental_strategy or "delete+insert").lower()
    if strategy == "append":
        return bool(model.incremental_filter)
    return True


def _invalidate_state(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
) -> None:
    """Make the next run rebuild ``model`` and check its assertions again.

    ``_update_state`` runs as soon as the build succeeds, before assertions
    are evaluated, so a model whose severity=error assertion then fails is
    left recorded as up to date. The next run would skip it as unchanged,
    nothing would block its descendants any more, and they would build on
    the data the assertion had just rejected. Marking it blocked makes
    ``_has_changed`` rebuild it; a clean build clears the mark.
    """
    conn.execute("DELETE FROM _havn.model_blocked WHERE model_path = ?", [model.full_name])
    conn.execute("INSERT INTO _havn.model_blocked (model_path) VALUES (?)", [model.full_name])


def _clear_block(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> None:
    """Accept ``model``'s current build: its checks ran and passed.

    Only a passing check clears a block. Building alone does not: the job
    runner and the stream pipeline build without (or without acting on)
    assertions, and clearing on build let a scheduled job reopen the gate.
    """
    try:
        conn.execute("DELETE FROM _havn.model_blocked WHERE model_path = ?", [model.full_name])
    except duckdb.CatalogException:
        pass


def blocked_models(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Every model whose last build an error assertion rejected."""
    try:
        return {r[0] for r in conn.execute("SELECT model_path FROM _havn.model_blocked").fetchall()}
    except duckdb.CatalogException:
        return set()


def _is_blocked(conn: duckdb.DuckDBPyConnection, model: SQLModel) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM _havn.model_blocked WHERE model_path = ? LIMIT 1",
            [model.full_name],
        ).fetchone()
    except duckdb.CatalogException:
        # A warehouse bootstrapped before the table existed has nothing blocked.
        return False
    return row is not None


def _update_state(
    conn: duckdb.DuckDBPyConnection,
    model: SQLModel,
    duration_ms: int,
    row_count: int,
) -> None:
    """Update the model state after a successful run.

    INSERT OR REPLACE on DuckDB is atomic against the model_path PK;
    DuckLake strips the PK at table creation so we fall back to
    DELETE-then-INSERT inside an explicit transaction.
    """
    from havn.engine.database import _is_ducklake_connection

    params = [
        model.full_name,
        model.content_hash,
        model.upstream_hash,
        model.materialized,
        duration_ms,
        row_count,
    ]
    if _is_ducklake_connection(conn):
        # A live refresh builds inside one outer transaction (data, consumed
        # watermarks and state commit together), so join it when one is
        # open instead of failing on a nested BEGIN.
        from havn.engine.utils import begin_transaction

        owns_tx = begin_transaction(conn)
        try:
            conn.execute(
                "DELETE FROM _havn.model_state WHERE model_path = ?",
                [model.full_name],
            )
            conn.execute(
                """
                INSERT INTO _havn.model_state
                    (model_path, content_hash, upstream_hash, materialized_as, last_run_at, run_duration_ms, row_count)
                VALUES (?, ?, ?, ?, current_timestamp, ?, ?)
                """,
                params,
            )
            if owns_tx:
                conn.execute("COMMIT")
        except Exception:
            if owns_tx:
                conn.execute("ROLLBACK")
            raise
    else:
        conn.execute(
            """
            INSERT OR REPLACE INTO _havn.model_state
                (model_path, content_hash, upstream_hash, materialized_as, last_run_at, run_duration_ms, row_count)
            VALUES (?, ?, ?, ?, current_timestamp, ?, ?)
            """,
            params,
        )
    save_model_columns(conn, model)

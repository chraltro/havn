"""Graph selectors over the model DAG.

One grammar, one implementation, used by ``havn transform``, ``havn ls``,
``POST /api/transform``, orchestration jobs and ``havn diff``. Before this
module the tree had three separate downstream-closure implementations and a
``schema.*`` prefix hack that silently matched nothing for ``gold.fct_*``.

Grammar (one *selector* per list entry)::

    schema.name          exactly that model
    name                 the model with that bare name, in any schema
    gold.fct_*           fnmatch wildcards anywhere in the name
    *.customers          ... including in the schema half
    *                    every model
    +x                   x and everything it depends on, transitively
    x+                   x and everything that depends on it, transitively
    +x+                  both
    2+x / x+2            the same, bounded to N hops (dbt's "n-plus")
    @x                   x, its descendants, and every ancestor of those
    tag:daily            models carrying @config tags=daily
    path:transform/gold/ models under a path prefix (or a path glob)
    package:crm          models that came from the installed package ``crm``
    package:             the project's own models, excluding every package
    config.materialized:incremental   any @config key on the model
    state:modified       models whose SQL or upstream changed since the last run
    state:modified+      ... plus their downstream
    tag:daily,gold.*     comma means intersection

``exclude`` is a second selection subtracted from the first, so it takes the
same grammar.
"""

from __future__ import annotations

import fnmatch
import re
from collections import deque
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from havn.engine.transform.models import SQLModel

# Depth sentinel for a bare ``+`` (no hop limit). A plain int keeps the
# traversal arithmetic free of None checks.
UNLIMITED = 1 << 30

# Selector methods that take a ``method:value`` form. ``config`` is special:
# it is written ``config.<key>:<value>``.
SELECTOR_METHODS = frozenset(
    {"tag", "path", "config", "state", "fqn", "name", "package"}
)


@dataclass
class SelectionResult:
    """What a set of selectors resolved to.

    Attributes:
        selected: Matched model full names, in DAG (dependency) order.
        matched: Selector string -> the full names it contributed, sorted.
            A selector that matched nothing maps to an empty list.
        warnings: Human-readable notes for selectors that matched nothing or
            that could not be evaluated. Callers decide whether to print or
            raise on these.
    """

    selected: list[str] = field(default_factory=list)
    matched: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.selected)

    def __iter__(self):
        return iter(self.selected)

    def __contains__(self, name: object) -> bool:
        return name in self.selected


@dataclass
class _Atom:
    """One comma-separated piece of a selector, already stripped of operators."""

    core: str
    up: int | None = None        # hops of upstream, None = not requested
    down: int | None = None      # hops of downstream, None = not requested
    at_sign: bool = False        # ``@x``


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_LEADING_PLUS = re.compile(r"^(\d*)\+")
_TRAILING_PLUS = re.compile(r"\+(\d*)$")


def strip_graph_operators(target: str) -> tuple[bool, bool, str]:
    """Split ``target`` into (include_upstream, include_downstream, bare_name).

    The boolean form kept for callers that only need to know whether a target
    names a script (``ingest/x.py``) rather than a model. The legacy
    ``+downstream:`` prefix is still accepted.
    """
    atom = _parse_atom(target)
    return (atom.up is not None, atom.down is not None, atom.core)


def _parse_atom(atom: str) -> _Atom:
    """Parse one selector piece into its core plus its graph operators."""
    s = atom.strip()
    if s.startswith("+downstream:"):  # legacy spelling, predates ``x+``
        return _Atom(core=s[len("+downstream:") :], down=UNLIMITED)
    if s.startswith("@"):
        return _Atom(core=s[1:], at_sign=True)

    up: int | None = None
    down: int | None = None
    m = _LEADING_PLUS.match(s)
    if m:
        up = int(m.group(1)) if m.group(1) else UNLIMITED
        s = s[m.end() :]
    m = _TRAILING_PLUS.search(s)
    if m:
        down = int(m.group(1)) if m.group(1) else UNLIMITED
        s = s[: m.start()]
    return _Atom(core=s, up=up, down=down)


def _split_method(core: str) -> tuple[str, str] | None:
    """Split ``tag:daily`` into ("tag", "daily"), or None for a plain name.

    ``gold.fct_orders`` has no colon and is a name. ``config.materialized:x``
    keeps the dotted key in the method half.
    """
    if ":" not in core:
        return None
    method, _, value = core.partition(":")
    head = method.split(".", 1)[0]
    if head not in SELECTOR_METHODS:
        return None
    return (method, value)


# ---------------------------------------------------------------------------
# Graph traversal (the one downstream-closure implementation)
# ---------------------------------------------------------------------------


class _Graph:
    """Both edge directions of the model DAG, built once per selection.

    Every traversal below goes through here, so the parent and child maps are
    built once instead of once per selector per model.
    """

    def __init__(self, models: list[SQLModel]) -> None:
        self.parents: dict[str, list[str]] = {}
        self.children: dict[str, set[str]] = {m.full_name: set() for m in models}
        for m in models:
            deps = [d for d in (m.depends_on or []) if d in self.children]
            self.parents[m.full_name] = deps
        for name, deps in self.parents.items():
            for dep in deps:
                self.children[dep].add(name)

    def _walk(self, start: str, edges: dict[str, Any], depth: int) -> set[str]:
        """Breadth-first closure from ``start``, ``depth`` hops deep.

        The start node is included. Edges pointing at something that is not a
        model (a landing table, say) are already filtered out of the maps.
        """
        if start not in edges:
            return {start}
        seen = {start}
        frontier: deque[tuple[str, int]] = deque([(start, 0)])
        while frontier:
            node, dist = frontier.popleft()
            if dist >= depth:
                continue
            for nxt in edges.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append((nxt, dist + 1))
        return seen

    def ancestors(self, name: str, depth: int = UNLIMITED) -> set[str]:
        """``name`` plus everything it depends on, up to ``depth`` hops."""
        return self._walk(name, self.parents, depth)

    def descendants(self, name: str, depth: int = UNLIMITED) -> set[str]:
        """``name`` plus everything that depends on it, up to ``depth`` hops."""
        return self._walk(name, self.children, depth)


def ancestors(
    name: str, models: list[SQLModel], depth: int = UNLIMITED, *, include_self: bool = True
) -> set[str]:
    """Everything ``name`` depends on, up to ``depth`` hops."""
    found = _Graph(models).ancestors(name, depth)
    return found if include_self else found - {name}


def descendants(
    name: str, models: list[SQLModel], depth: int = UNLIMITED, *, include_self: bool = True
) -> set[str]:
    """Everything that depends on ``name``, up to ``depth`` hops.

    The single downstream-closure implementation in the tree; ``havn diff``
    and orchestration jobs both route through it.
    """
    found = _Graph(models).descendants(name, depth)
    return found if include_self else found - {name}


# ---------------------------------------------------------------------------
# Core matching
# ---------------------------------------------------------------------------


def _rel_path(model: SQLModel, project_dir: Path | None) -> str:
    """The model's file path relative to the project, in posix form."""
    path = Path(model.path)
    if project_dir is not None:
        try:
            path = path.relative_to(Path(project_dir))
        except ValueError:
            pass
    return path.as_posix()


def _config_value(model: SQLModel, key: str) -> str | list[str] | None:
    """Read a ``@config`` key off the model, normalized to text."""
    value: Any = getattr(model, key, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return str(value)


def _match_core(
    core: str,
    models: list[SQLModel],
    conn: Any,
    project_dir: Path | None,
    changed_cache: dict[str, bool],
) -> tuple[set[str], str]:
    """Match one selector core against ``models``.

    Returns the matched full names plus a warning string ("" when fine).
    """
    if not core:
        return (set(), "")

    parts = _split_method(core)
    if parts is None:
        # A plain (possibly wildcard) model name. A dotted pattern is matched
        # against ``schema.name``; a bare one against either, so both
        # ``customers`` and ``bronze.customers`` find the same model.
        out = set()
        dotted = "." in core
        for m in models:
            if fnmatch.fnmatchcase(m.full_name, core) or (
                not dotted and fnmatch.fnmatchcase(m.name, core)
            ):
                out.add(m.full_name)
        return (out, "")

    method, value = parts

    if method == "tag":
        return (
            {
                m.full_name
                for m in models
                if any(fnmatch.fnmatchcase(t, value) for t in getattr(m, "tags", []) or [])
            },
            "",
        )

    if method == "path":
        prefix = value.strip().rstrip("/")
        out = set()
        for m in models:
            rel = _rel_path(m, project_dir)
            if rel == prefix or rel.startswith(prefix + "/") or fnmatch.fnmatchcase(rel, value):
                out.add(m.full_name)
        return (out, "")

    if method == "package":
        # ``package:crm`` selects a package's models; ``package:*`` every
        # package model; ``package:`` (empty) the project's own models, which
        # is the only way to say "mine, not theirs".
        if not value:
            return ({m.full_name for m in models if not getattr(m, "package", None)}, "")
        return (
            {
                m.full_name
                for m in models
                if getattr(m, "package", None)
                and fnmatch.fnmatchcase(m.package or "", value)
            },
            "",
        )

    if method in ("fqn", "name"):
        attr = "full_name" if method == "fqn" else "name"
        return (
            {m.full_name for m in models if fnmatch.fnmatchcase(getattr(m, attr), value)},
            "",
        )

    if method.startswith("config"):
        _, _, key = method.partition(".")
        if not key:
            return (set(), "config: needs a key, e.g. config.materialized:table")
        out = set()
        for m in models:
            got = _config_value(m, key)
            if got is None:
                continue
            if isinstance(got, list):
                if any(fnmatch.fnmatchcase(g, value) for g in got):
                    out.add(m.full_name)
            elif fnmatch.fnmatchcase(got, value):
                out.add(m.full_name)
        return (out, "")

    if method == "state":
        if value != "modified":
            return (set(), f"Unknown state selector 'state:{value}'. Supported: state:modified.")
        if conn is None:
            return (set(), "state:modified needs a warehouse connection; nothing selected.")
        return ({m.full_name for m in models if changed_cache.get(m.full_name, False)}, "")

    return (set(), f"Unknown selector method '{method}'.")


def _changed_models(conn: Any, models: list[SQLModel]) -> dict[str, bool]:
    """Which models ``_has_changed`` reports as modified, keyed by full name.

    Upstream hashes are computed over the whole list first, exactly as a
    transform run does, so a model whose parent changed is reported modified
    rather than compared against an empty upstream hash.
    """
    from havn.engine.transform.discovery import (
        _compute_upstream_hash,
        _has_changed,
        build_dag,
    )

    try:
        ordered = build_dag(models)
    except Exception:
        ordered = models
    model_map = {m.full_name: m for m in ordered}
    for model in ordered:
        model.upstream_hash = _compute_upstream_hash(model, model_map)
    out: dict[str, bool] = {}
    for model in ordered:
        try:
            out[model.full_name] = _has_changed(conn, model)
        except Exception:
            # No _havn.model_state yet: nothing has ever been built, so
            # everything counts as modified.
            out[model.full_name] = True
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _topological(names: set[str], models: list[SQLModel]) -> list[str]:
    """Order ``names`` so every model follows the dependencies it selects."""
    model_map = {m.full_name: m for m in models}
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for name in names:
        if name in model_map:
            deps = [d for d in (model_map[name].depends_on or []) if d in names]
            sorter.add(name, *deps)
    return [n for n in sorter.static_order() if n in names and n in model_map]


def _expand_atom(
    atom: _Atom,
    core_matches: set[str],
    graph: _Graph,
    resolve: str,
) -> set[str]:
    """Apply an atom's graph operators to the models its core matched."""
    selected: set[str] = set()
    for name in core_matches:
        up = atom.up
        if up is None and atom.down is None and not atom.at_sign and resolve == "upstream":
            # Legacy job behavior: a bare target means "+target".
            up = UNLIMITED
        if up is not None:
            selected |= graph.ancestors(name, up)
        else:
            selected.add(name)
        if atom.down is not None:
            desc = graph.descendants(name, atom.down)
            if resolve == "upstream":
                for d in desc:
                    selected |= graph.ancestors(d)
            else:
                selected |= desc
        if atom.at_sign:
            # @x is x, its descendants, and every ancestor of those, so the
            # selection is buildable from scratch in one run.
            for d in graph.descendants(name):
                selected |= graph.ancestors(d)
    return selected


def select_models(
    selectors: list[str] | str | None,
    models: list[SQLModel],
    *,
    conn: Any = None,
    project_dir: Path | None = None,
    resolve: str = "none",
    exclude: list[str] | str | None = None,
) -> SelectionResult:
    """Resolve graph selectors against a list of models.

    Args:
        selectors: Selector strings. Empty, None or ``["all"]`` selects every
            model (``havn transform`` with no arguments).
        models: The models to select from, usually ``discover_models()`` or
            ``build_dag()`` output.
        conn: Warehouse connection, needed only by ``state:`` selectors.
        project_dir: Project root, needed only by ``path:`` selectors to make
            model paths relative.
        resolve: ``"none"`` (the default) runs selectors literally.
            ``"upstream"`` is the legacy orchestration-job mode where a target
            with no ``+`` markers means ``+target`` and a downstream expansion
            also pulls in the upstream of everything it reached.
        exclude: A second set of selectors, subtracted from the first.

    Returns:
        A :class:`SelectionResult`. Selectors that matched nothing are
        reported in ``warnings`` rather than raising, so a job or a CLI run
        can decide for itself how loud to be.
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    if isinstance(exclude, str):
        exclude = [exclude]

    result = SelectionResult()
    if not models:
        if selectors:
            result.warnings.append("No SQL models found.")
        return result

    if not selectors or list(selectors) == ["all"]:
        chosen = {m.full_name for m in models}
    else:
        # Deduplicate while preserving order, so a repeated --select is a no-op.
        seen: set[str] = set()
        ordered_selectors = [s for s in selectors if not (s in seen or seen.add(s))]

        changed_cache: dict[str, bool] = {}
        if conn is not None and any("state:" in s for s in ordered_selectors):
            changed_cache = _changed_models(conn, models)

        graph = _Graph(models)
        chosen = set()
        for selector in ordered_selectors:
            # Commas intersect: each piece is resolved in full, operators and
            # all, and only models in every piece survive.
            pieces = [p for p in selector.split(",") if p.strip()]
            per_piece: list[set[str]] = []
            for piece in pieces:
                atom = _parse_atom(piece)
                core_matches, warning = _match_core(
                    atom.core, models, conn, project_dir, changed_cache
                )
                if warning:
                    result.warnings.append(warning)
                per_piece.append(_expand_atom(atom, core_matches, graph, resolve))
            hit = set.intersection(*per_piece) if per_piece else set()
            result.matched[selector] = sorted(hit)
            if not hit:
                result.warnings.append(f"Selector '{selector}' matched no models.")
            chosen |= hit

    if exclude:
        excluded = select_models(
            exclude, models, conn=conn, project_dir=project_dir, resolve=resolve
        )
        result.warnings.extend(excluded.warnings)
        chosen -= set(excluded.selected)

    result.selected = _topological(chosen, models)
    return result

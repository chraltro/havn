"""Rename a column across the models that read it.

A column rename is not a text substitution. The same word names different
things in different models, a downstream model may only filter or join on the
column without ever projecting it, and a model that re-exports the column
under the same name passes the rename on to its own children. Getting any of
that wrong ships SQL that still parses and no longer means the same thing.

So this module does three separate jobs, and keeps them separate:

1. :func:`find_column_references` builds the index: every place the column is
   written, with a byte range in the file it lives in, plus a list of the
   places it could not see through. Nothing is guessed. A downstream model
   that reaches the column through ``SELECT *`` is a blocker, not a silent
   skip, because a star yields no ``Column`` node to rewrite.
2. :func:`plan_rename` turns the index into a list of splices, refusing on a
   bad new name, a collision, or an unresolved blocker.
3. :func:`apply_rename` performs the splices, re-reading every file and
   checking the old text is still there before it writes anything, and
   restoring every file it touched if any write fails.

Offsets
-------

``strip_config_comments`` blanks directive lines in place: line *numbers*
survive, the characters do not, so a query offset is not a file offset.
:class:`_OffsetMap` bridges the two by shifting per line, which is exact
because every line that is not a directive is copied verbatim.

Every offset a :class:`RenameSite` or a :class:`FileEdit` carries is a **file**
offset with an **exclusive** end, so ``text[start:end]`` is the identifier.
That differs from :class:`~havn.engine.sql_analysis.ColumnRef`, whose ``end``
is inclusive and whose offsets point into the stripped query.
"""

from __future__ import annotations

import os
import re
import stat
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from sqlglot import exp

from havn.engine.selectors import descendants
from havn.engine.sql_analysis import (
    extract_column_references,
    parse_sql,
    relation_aliases,
    strip_config_comments,
)

# Reasons a rename refuses to touch a model. Stable strings: the API and the
# CLI both render them, and tests assert on them.
STAR = "select_star"
COLUMNS_EXPR = "columns_expression"
UNION_BY_NAME = "union_by_name"
OPAQUE_RELATION = "opaque_relation"
UNRESOLVED = "unresolved_reference"
YAML_MENTION = "yaml_mention"
NO_DEFINITION = "no_definition"
UNPARSED = "unparsed"
POSITION = "position_mismatch"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Directories whose YAML files name columns in plain text.
_YAML_DIRS = ("metrics", "contracts")


class RenameError(ValueError):
    """A rename cannot be planned or applied."""


@dataclass(frozen=True)
class RenameSite:
    """One place the column is written, as a range in a project file.

    ``start`` and ``end`` are 0-based character offsets into the file, with
    ``end`` exclusive, so ``file_text[start:end]`` is the identifier as
    written, quotes included when it is quoted.
    """

    model: str
    """Model ``full_name``, or "" for a YAML site that belongs to no model."""

    path: str
    """Project-relative path, posix separators."""

    line: int
    col: int
    start: int
    end: int
    clause: str
    """select, where, join, group, order, having, qualify, window, or yaml."""

    kind: str
    """definition, reference, alias or yaml. See the module docstring."""

    resolved: bool
    """False when the site is a candidate the index could not attribute."""

    text: str = ""
    """The identifier exactly as it appears in the file."""

    needs_alias: bool = False
    """The site defines an output column by projecting an upstream column
    unaliased. Renaming it means adding ``AS <new>`` rather than rewriting the
    identifier, which would silently point at a column that does not exist."""


@dataclass(frozen=True)
class Blocker:
    """Something the index saw but refuses to rename around."""

    reason: str
    model: str
    path: str
    message: str
    line: int | None = None


@dataclass
class ReferenceReport:
    """Everything :func:`find_column_references` found.

    Iterates over its sites, so a caller that only wants the list can treat it
    as one.
    """

    target: str
    column: str
    sites: list[RenameSite] = field(default_factory=list)
    blocked: list[Blocker] = field(default_factory=list)

    def __iter__(self) -> Iterator[RenameSite]:
        return iter(self.sites)

    def __len__(self) -> int:
        return len(self.sites)

    def __getitem__(self, index: int) -> RenameSite:
        return self.sites[index]

    @property
    def models(self) -> list[str]:
        """Distinct models with at least one site, in first-seen order."""
        seen: list[str] = []
        for site in self.sites:
            if site.model and site.model not in seen:
                seen.append(site.model)
        return seen


@dataclass(frozen=True)
class FileEdit:
    """One splice: replace ``old_text`` at ``[start, end)`` with ``new_text``."""

    path: str
    start: int
    end: int
    old_text: str
    new_text: str
    kind: str = "reference"
    model: str = ""
    line: int = 0


# ---------------------------------------------------------------------------
# Offsets
# ---------------------------------------------------------------------------


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


class _OffsetMap:
    """Translate an offset in a model's stripped query into a file offset.

    Both texts have the same number of lines, and every line that survived
    stripping is byte-identical, so the translation is a per-line shift. When
    the line counts disagree the map reports itself unusable rather than
    producing an offset that points at the wrong character.
    """

    def __init__(self, file_text: str, query: str) -> None:
        self._query_starts = _line_starts(query)
        self._file_starts = _line_starts(file_text)
        self.usable = len(self._query_starts) == len(self._file_starts)

    def to_file(self, offset: int) -> int:
        index = bisect_right(self._query_starts, offset) - 1
        if index < 0:
            index = 0
        return self._file_starts[index] + (offset - self._query_starts[index])


# ---------------------------------------------------------------------------
# Reading a model's shape
# ---------------------------------------------------------------------------


def _output_selects(parsed: exp.Expression) -> list[exp.Select]:
    """The SELECTs whose projections form the query's output columns.

    A set operation has one per branch: each branch names the output, so a
    rename has to reach all of them.
    """
    if isinstance(parsed, exp.Select):
        return [parsed]
    if isinstance(parsed, exp.Union):
        found: list[exp.Select] = []
        for side in (parsed.this, parsed.expression):
            if side is not None:
                found.extend(_output_selects(side))
        return found
    select = parsed.find(exp.Select)
    return [select] if select is not None else []


def _has_star(select: exp.Select) -> bool:
    """True when the projection expands a star, with or without EXCLUDE."""
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            return True
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return True
    return False


def _identifier_span(node: exp.Expression | None) -> tuple[int, int, int, int] | None:
    """``(line, col, start, end_exclusive)`` for an identifier node."""
    meta = getattr(node, "meta", None) or {}
    if "start" not in meta or "end" not in meta:
        return None
    return (
        int(meta.get("line", 0)),
        int(meta.get("col", 0)),
        int(meta["start"]),
        int(meta["end"]) + 1,
    )


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _defining_projections(
    select: exp.Select, column: str
) -> list[tuple[exp.Expression, bool]]:
    """Projections whose **output** is named ``column``.

    Returns ``(node, is_alias)``: ``node`` is the identifier that gives the
    output its name, which is the alias when there is one and the column
    identifier otherwise. The expression behind an alias is not inspected;
    ``COUNT(*) AS customer_id`` defines the column just as much as a plain
    projection does.
    """
    found: list[tuple[exp.Expression, bool]] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Alias):
            if _unquote(projection.alias).lower() == column:
                found.append((projection.args.get("alias"), True))
            continue
        if not isinstance(projection, exp.Column) or isinstance(
            projection.this, exp.Star
        ):
            continue
        if _unquote(projection.name).lower() == column:
            found.append((projection.this, False))
    return found


def _source_projections(
    select: exp.Select, column: str, relations: set[str], aliases: dict[str, str]
) -> list[tuple[exp.Expression, str, bool]]:
    """Projections that **read** ``column`` out of one of ``relations``.

    Returns ``(node, output_name, is_alias)``: ``node`` is the identifier that
    names the output. ``relations`` holds lower-cased relation names; an empty
    set means "any relation".
    """
    found: list[tuple[exp.Expression, str, bool]] = []
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if not isinstance(inner, exp.Column) or isinstance(inner.this, exp.Star):
            continue
        if _unquote(inner.name).lower() != column:
            continue
        if relations:
            qualifier = _unquote(inner.table or "").lower()
            source = aliases.get(qualifier, qualifier)
            if source not in relations:
                continue
        if isinstance(projection, exp.Alias):
            found.append(
                (projection.args.get("alias"), _unquote(projection.alias).lower(), True)
            )
        else:
            found.append((inner.this, column, False))
    return found


def _classify_projections(
    select: exp.Select, column: str, exposed: set[str], aliases: dict[str, str]
) -> tuple[bool, list[exp.Expression], list[tuple[exp.Expression, str]]]:
    """Split one SELECT's projections of ``column`` three ways.

    Returns ``(re_exported, same_name_aliases, renamed_aliases)``:

    - ``re_exported`` is True when the SELECT outputs the column under its own
      name, so whatever reads this relation sees the rename too.
    - ``same_name_aliases`` are ``x AS x`` alias identifiers, which have to be
      renamed alongside the reference for the name to keep travelling.
    - ``renamed_aliases`` are ``x AS other`` identifiers: the name stops here,
      so they are reported and never edited.
    """
    re_exported = False
    same_name: list[exp.Expression] = []
    renamed: list[tuple[exp.Expression, str]] = []
    for node, out_name, is_alias in _source_projections(
        select, column, exposed, aliases
    ):
        if not is_alias:
            re_exported = True
        elif out_name == column:
            same_name.append(node)
            re_exported = True
        else:
            renamed.append((node, out_name))
    return re_exported, same_name, renamed


def _combined_aliases(parsed: exp.Expression) -> dict[str, str]:
    """Local relation name to the thing it refers to, tables and CTEs alike."""
    tables, ctes, _ = relation_aliases(parsed)
    combined = dict(tables)
    combined.update(ctes)
    return combined


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def _model_path(model: Any, project_dir: Path | None) -> str:
    path = Path(model.path)
    if project_dir is not None:
        try:
            path = path.relative_to(Path(project_dir))
        except ValueError:
            pass
    return path.as_posix()


def _site_from_span(
    model: Any,
    project_dir: Path | None,
    offsets: _OffsetMap,
    span: tuple[int, int, int, int],
    *,
    column: str,
    kind: str,
    clause: str,
    resolved: bool = True,
    needs_alias: bool = False,
) -> RenameSite | None:
    """Turn a query span into a file site, or None when the text disagrees.

    The check is the whole point: an offset that does not land on the
    identifier it claims to is a bug that would corrupt a file, so it is
    dropped here and reported as a blocker by the caller.
    """
    line, col, q_start, q_end = span
    if not offsets.usable:
        return None
    start = offsets.to_file(q_start)
    end = offsets.to_file(q_end - 1) + 1
    text = model.sql[start:end]
    if _unquote(text).lower() != column:
        return None
    return RenameSite(
        model=model.full_name,
        path=_model_path(model, project_dir),
        line=line,
        col=col,
        start=start,
        end=end,
        clause=clause,
        kind=kind,
        resolved=resolved,
        text=text,
        needs_alias=needs_alias,
    )


def _select_relations(select: exp.Select, aliases: dict[str, str]) -> set[str]:
    """The relations a SELECT reads directly, resolved through its aliases.

    Only its own FROM and JOINs: a nested subquery is a scope of its own and
    its sources do not feed this SELECT's star.
    """
    found: set[str] = set()
    # sqlglot 30 keys the FROM clause as ``from_``; older releases used
    # ``from``. Reading both keeps this working either way.
    for key in ("from_", "from", "joins"):
        node = select.args.get(key)
        if node is None:
            continue
        for part in node if isinstance(node, list) else [node]:
            for table in part.find_all(exp.Table):
                name = _unquote(table.name or "").lower()
                db = _unquote(table.db or "").lower()
                fqn = f"{db}.{name}" if db else name
                found.add(aliases.get(fqn, aliases.get(name, fqn)))
    return found


def _star_blocker(
    model: Any,
    path: str,
    select: exp.Select,
    exposed: set[str],
    aliases: dict[str, str],
    where: str,
) -> Blocker | None:
    """A blocker when ``select`` stars over a relation that carries the column.

    A star elsewhere in the model is not this rename's business, so the check
    is scoped to the relations that actually hold the renamed column.
    """
    if not _has_star(select):
        return None
    if not (_select_relations(select, aliases) & exposed):
        return None
    return Blocker(
        STAR,
        model.full_name,
        path,
        f"{where} expands SELECT * over a relation that carries the column, "
        "so there is no identifier to rename",
    )


def _scan_blockers(
    model: Any, project_dir: Path | None, parsed: exp.Expression
) -> list[Blocker]:
    """Model-wide constructs a reference index cannot see a column through."""
    path = _model_path(model, project_dir)
    found: list[Blocker] = []
    if any(parsed.find_all(exp.Columns)):
        found.append(
            Blocker(
                COLUMNS_EXPR,
                model.full_name,
                path,
                "uses COLUMNS(...), which expands to columns that are not written out",
            )
        )
    for union in parsed.find_all(exp.Union):
        if union.args.get("by_name"):
            found.append(
                Blocker(
                    UNION_BY_NAME,
                    model.full_name,
                    path,
                    "uses UNION BY NAME, which matches columns by name at run time",
                )
            )
            break
    for table in parsed.find_all(exp.Table):
        inner = table.this
        if isinstance(inner, exp.Func):
            found.append(
                Blocker(
                    OPAQUE_RELATION,
                    model.full_name,
                    path,
                    f"reads from {inner.sql(dialect='duckdb')}, whose SQL is not visible",
                )
            )
            break
    return found


def _yaml_sites(
    project_dir: Path | None, column: str
) -> tuple[list[RenameSite], list[Blocker]]:
    """Plain-text mentions of ``column`` in metrics/ and contracts/ YAML.

    A YAML file names columns in expressions havn never parses, so a match
    here is a word match, nothing stronger. It is reported as an editable site
    and as a blocker at the same time: precise enough to splice, too weak to
    apply without someone looking at it.
    """
    if project_dir is None:
        return [], []
    pattern = re.compile(rf"\b{re.escape(column)}\b", re.IGNORECASE)
    sites: list[RenameSite] = []
    blocked: list[Blocker] = []
    root = Path(project_dir)
    for folder in _YAML_DIRS:
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix not in (".yml", ".yaml") or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            starts = _line_starts(text)
            rel = path.relative_to(root).as_posix()
            hits = 0
            for match in pattern.finditer(text):
                line = bisect_right(starts, match.start())
                sites.append(
                    RenameSite(
                        model="",
                        path=rel,
                        line=line,
                        col=match.start() - starts[line - 1] + 1,
                        start=match.start(),
                        end=match.end(),
                        clause="yaml",
                        kind="yaml",
                        resolved=False,
                        text=match.group(0),
                    )
                )
                hits += 1
            if hits:
                blocked.append(
                    Blocker(
                        YAML_MENTION,
                        "",
                        rel,
                        f"names the column in {hits} place{'' if hits == 1 else 's'}; "
                        "YAML is matched as text, so check each one",
                    )
                )
    return sites, blocked


def find_column_references(
    models: list[Any],
    target_model: str,
    column: str,
    *,
    schemas: dict[str, list[tuple[str, str]]] | None = None,
    project_dir: Path | str | None = None,
) -> ReferenceReport:
    """Index every written mention of ``target_model``'s ``column``.

    The walk starts at the column's defining site in ``target_model`` and
    follows the downstream closure. A downstream model that projects the
    column unaliased re-exports it under the same name, so its own children
    are walked too; a model that re-aliases it ends the chain there and the
    alias is reported so the reader can see where the name stops travelling.
    CTEs inside a model are followed the same way.

    Args:
        models: Every model in the project.
        target_model: ``schema.name`` of the model whose column is renamed.
        column: The column name, case-insensitive.
        schemas: ``{"schema.table": [(column, type), ...]}`` from the bind pass
            or ``_havn.model_columns``. Used to attribute an unqualified
            column when the model reads more than one relation.
        project_dir: Project root, for project-relative paths and for the
            YAML scan.

    Returns:
        A :class:`ReferenceReport`. It iterates over its sites, and carries
        the blockers separately.

    Raises:
        RenameError: ``target_model`` is not one of ``models``.
    """
    column = _unquote(column).lower()
    target_key = target_model.lower()
    by_name = {m.full_name.lower(): m for m in models}
    target = by_name.get(target_key)
    if target is None:
        raise RenameError(f"Unknown model: {target_model}")

    report = ReferenceReport(target=target.full_name, column=column)
    root = Path(project_dir) if project_dir is not None else None

    # The definition, in the target itself.
    exports_own_name = _index_definition(report, target, root, column)

    # Downstream, one hop at a time, carrying the column only where it keeps
    # its name.
    closure = descendants(target.full_name, models, include_self=False)
    children: dict[str, list[Any]] = {}
    for model in models:
        for dep in model.depends_on or []:
            children.setdefault(dep.lower(), []).append(model)

    if exports_own_name:
        producing = {target.full_name.lower()}
        visited: set[tuple[str, str]] = set()
        queue = [target.full_name.lower()]
        while queue:
            producer = queue.pop(0)
            for child in children.get(producer, []):
                if child.full_name not in closure:
                    continue
                key = (child.full_name.lower(), producer)
                if key in visited:
                    continue
                visited.add(key)
                if _index_downstream(report, child, root, column, producing, schemas):
                    if child.full_name.lower() not in producing:
                        producing.add(child.full_name.lower())
                        queue.append(child.full_name.lower())

    yaml_sites, yaml_blocked = _yaml_sites(root, column)
    report.sites.extend(yaml_sites)
    report.blocked.extend(yaml_blocked)
    report.sites.sort(key=lambda s: (s.path, s.start))
    return report


def _index_definition(
    report: ReferenceReport, target: Any, root: Path | None, column: str
) -> bool:
    """Record the defining site in the target model. True if it was found."""
    path = _model_path(target, root)
    parsed = getattr(target, "ast", None) or parse_sql(target.query)
    if parsed is None:
        report.blocked.append(
            Blocker(UNPARSED, target.full_name, path, "does not parse")
        )
        return False

    offsets = _OffsetMap(target.sql, target.query)
    found = False
    for select in _output_selects(parsed):
        projections = _defining_projections(select, column)
        if not projections and _has_star(select):
            report.blocked.append(
                Blocker(
                    STAR,
                    target.full_name,
                    path,
                    f"projects SELECT * , so {column} has no defining identifier",
                )
            )
            continue
        for node, is_alias in projections:
            span = _identifier_span(node)
            if span is None:
                continue
            site = _site_from_span(
                target,
                root,
                offsets,
                span,
                column=column,
                kind="definition",
                clause="select",
                needs_alias=not is_alias,
            )
            if site is None:
                report.blocked.append(
                    Blocker(
                        POSITION,
                        target.full_name,
                        path,
                        f"the definition of {column} could not be located in the file",
                        line=span[0],
                    )
                )
                continue
            report.sites.append(site)
            found = True

    if not found and not any(b.model == target.full_name for b in report.blocked):
        report.blocked.append(
            Blocker(
                NO_DEFINITION,
                target.full_name,
                path,
                f"does not output a column named {column}",
            )
        )
    return found


def _index_downstream(
    report: ReferenceReport,
    model: Any,
    root: Path | None,
    column: str,
    producing: set[str],
    schemas: dict[str, list[tuple[str, str]]] | None,
) -> bool:
    """Record every mention in one downstream model.

    Returns True when the model re-exports the column under the same name, so
    the caller knows to carry the rename on to the model's own children.
    """
    path = _model_path(model, root)
    parsed = getattr(model, "ast", None) or parse_sql(model.query)
    if parsed is None:
        report.blocked.append(Blocker(UNPARSED, model.full_name, path, "does not parse"))
        return False

    report.blocked.extend(_scan_blockers(model, root, parsed))
    offsets = _OffsetMap(model.sql, model.query)
    aliases = _combined_aliases(parsed)

    # Relations inside this model that carry the column under its own name:
    # the upstream models already renamed, plus every CTE that re-exports it.
    # CTEs are walked in definition order, which is the order the column can
    # travel through them.
    exposed = set(producing)
    alias_sites: list[tuple[exp.Expression, str]] = []
    same_name_aliases: list[exp.Expression] = []
    for cte in parsed.find_all(exp.CTE):
        select = cte.this if isinstance(cte.this, exp.Select) else cte.find(exp.Select)
        if select is None:
            continue
        name = (cte.alias or "").lower()
        star = _star_blocker(model, path, select, exposed, aliases, f"CTE {name}")
        if star is not None:
            report.blocked.append(star)
            continue
        re_exported, same_name, renamed = _classify_projections(
            select, column, exposed, aliases
        )
        alias_sites.extend(renamed)
        same_name_aliases.extend(same_name)
        if re_exported and name:
            exposed.add(name)

    re_export = False
    for select in _output_selects(parsed):
        star = _star_blocker(model, path, select, exposed, aliases, "the output")
        if star is not None:
            report.blocked.append(star)
            continue
        exported, same_name, renamed = _classify_projections(
            select, column, exposed, aliases
        )
        alias_sites.extend(renamed)
        same_name_aliases.extend(same_name)
        re_export = re_export or exported

    for node, out_name in alias_sites:
        span = _identifier_span(node)
        if span is None:
            continue
        site = _site_from_span(
            model,
            root,
            offsets,
            span,
            column=out_name,
            kind="alias",
            clause="select",
        )
        if site is not None:
            report.sites.append(site)

    # ``x AS x`` keeps the name on purpose. Renaming only the reference would
    # pin the old name to the model's output, so the alias is renamed with it
    # and the column keeps travelling.
    for node in same_name_aliases:
        span = _identifier_span(node)
        if span is None:
            continue
        site = _site_from_span(
            model,
            root,
            offsets,
            span,
            column=column,
            kind="definition",
            clause="select",
        )
        if site is not None:
            report.sites.append(site)

    refs = extract_column_references(
        model.query,
        model.depends_on,
        schema=schemas,
        ast=parsed,
    )
    for ref in refs:
        if ref.column != column:
            continue
        resolved = ref.table in exposed
        if not resolved:
            if ref.table:
                continue
            report.blocked.append(
                Blocker(
                    UNRESOLVED,
                    model.full_name,
                    path,
                    f"an unqualified {column} on line {ref.line} could not be "
                    "attributed to one relation",
                    line=ref.line,
                )
            )
        site = _site_from_span(
            model,
            root,
            offsets,
            (ref.line, ref.col, ref.start, ref.end + 1),
            column=column,
            kind="reference",
            clause=ref.clause,
            resolved=resolved,
        )
        if site is None:
            report.blocked.append(
                Blocker(
                    POSITION,
                    model.full_name,
                    path,
                    f"a mention of {column} on line {ref.line} could not be located "
                    "in the file",
                    line=ref.line,
                )
            )
            continue
        report.sites.append(site)

    return re_export


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _rename_text(old_text: str, new: str) -> str:
    """Keep the identifier's quoting when replacing it."""
    if len(old_text) >= 2 and old_text[0] == '"' and old_text[-1] == '"':
        return f'"{new}"'
    return new


def plan_rename(
    sites: ReferenceReport | list[RenameSite],
    old: str,
    new: str,
    *,
    force: bool = False,
    schemas: dict[str, list[tuple[str, str]]] | None = None,
    blocked: list[Blocker] | None = None,
    target_model: str | None = None,
) -> list[FileEdit]:
    """Turn an index into splices, grouped per file, highest offset first.

    Descending order is what makes applying them trivial: every edit is at an
    offset the earlier edits have not moved.

    ``alias`` sites are reported but never edited. A downstream model that
    re-aliases the column keeps its own output name, so rewriting the alias
    would rename a second, different column nobody asked about.

    Args:
        sites: A :class:`ReferenceReport`, or a bare list of sites plus
            ``blocked`` and ``target_model``.
        old: The current column name, for the sanity check against each site.
        new: The new column name.
        force: Plan even though blockers were reported.
        schemas: Used for the collision check against the target's columns.
        blocked: Blockers, when ``sites`` is a bare list.
        target_model: The target, when ``sites`` is a bare list.

    Raises:
        RenameError: ``new`` is not an identifier, ``new`` already names a
            column of the target model, or blockers were reported without
            ``force``.
    """
    if isinstance(sites, ReferenceReport):
        site_list = sites.sites
        blockers = sites.blocked
        target = sites.target
    else:
        site_list = list(sites)
        blockers = list(blocked or [])
        target = target_model or next(
            (s.model for s in site_list if s.kind == "definition"), ""
        )

    old = _unquote(old).lower()
    new_clean = _unquote(new)
    if not _IDENTIFIER.match(new_clean):
        raise RenameError(
            f"{new!r} is not a valid column name: letters, digits and "
            "underscores only, not starting with a digit"
        )
    if new_clean.lower() == old:
        raise RenameError(f"{new_clean!r} is already the column's name")

    if schemas and target:
        existing = {
            name.lower() for name, _type in schemas.get(target, []) or []
        }
        if new_clean.lower() in existing:
            raise RenameError(
                f"{target} already has a column named {new_clean!r}"
            )

    if blockers and not force:
        reasons = "; ".join(f"{b.path}: {b.message}" for b in blockers[:5])
        raise RenameError(
            f"{len(blockers)} blocker(s) stand in the way of renaming "
            f"{old} to {new_clean}: {reasons}"
        )

    edits: list[FileEdit] = []
    for site in site_list:
        if site.kind == "alias":
            continue
        if not site.resolved and site.kind != "yaml":
            continue
        replacement = _rename_text(site.text, new_clean)
        if site.needs_alias:
            replacement = f"{site.text} AS {new_clean}"
        edits.append(
            FileEdit(
                path=site.path,
                start=site.start,
                end=site.end,
                old_text=site.text,
                new_text=replacement,
                kind=site.kind,
                model=site.model,
                line=site.line,
            )
        )

    edits.sort(key=lambda e: (e.path, -e.start))
    return edits


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _splice(content: str, edits: list[FileEdit], path: str) -> str:
    """Apply one file's edits, checking the old text is still where it was."""
    for edit in sorted(edits, key=lambda e: -e.start):
        found = content[edit.start : edit.end]
        if found != edit.old_text:
            raise RenameError(
                f"{path}: expected {edit.old_text!r} at offset {edit.start}, "
                f"found {found!r}. The file changed since the plan was made."
            )
        content = content[: edit.start] + edit.new_text + content[edit.end :]
    return content


def apply_rename(
    project_dir: Path | str,
    edits: list[FileEdit],
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Apply ``edits`` to the project, all of them or none.

    Every file is read back and checked against the plan before anything is
    written, and each file is written to a temporary neighbour and renamed
    into place. If a write fails part way through, the files already written
    are restored from the copies held in memory, so the project is never left
    half renamed.

    Args:
        project_dir: The project root the edits' paths are relative to.
        edits: What :func:`plan_rename` produced.
        dry_run: Compute the new contents and write nothing.

    Returns:
        ``{path: new content}`` for every file the plan touches.

    Raises:
        RenameError: a file is missing, is not writable, no longer matches the
            plan, or a write failed. Nothing is left changed in any case.
    """
    root = Path(project_dir)
    grouped: dict[str, list[FileEdit]] = {}
    for edit in edits:
        grouped.setdefault(edit.path, []).append(edit)

    originals: dict[str, str] = {}
    updated: dict[str, str] = {}
    for path, file_edits in sorted(grouped.items()):
        full = root / path
        if not full.is_file():
            raise RenameError(f"{path}: file not found")
        try:
            content = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise RenameError(f"{path}: cannot read ({e})") from e
        originals[path] = content
        updated[path] = _splice(content, file_edits, path)

    if dry_run:
        return updated

    write_files_atomically(root, updated, originals=originals)
    return updated


def write_files_atomically(
    project_dir: Path | str,
    contents: dict[str, str],
    *,
    originals: dict[str, str] | None = None,
) -> None:
    """Write several files, or leave every one of them as it was.

    Each file goes to a temporary neighbour and is renamed into place, and the
    previous contents are held in memory so a failure part way through can put
    back what was already written. A file whose owner-write bit is clear is
    refused instead of replaced: ``os.replace`` only needs the *directory* to
    be writable, so without the check a read-only file would be overwritten
    without a word.

    Args:
        project_dir: Root the paths are relative to.
        contents: ``{path: new content}``.
        originals: Previous contents, read here when not supplied.

    Raises:
        RenameError: nothing was left changed.
    """
    root = Path(project_dir)
    previous = dict(originals or {})
    written: list[str] = []
    try:
        for path in sorted(contents):
            full = root / path
            if path not in previous and full.is_file():
                previous[path] = full.read_text(encoding="utf-8")
            mode = full.stat().st_mode if full.exists() else stat.S_IWUSR
            if not mode & stat.S_IWUSR:
                raise RenameError(f"{path}: file is read-only")
            full.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(full, contents[path])
            written.append(path)
    except Exception as e:
        for path in written:
            try:
                if path in previous:
                    _atomic_write(root / path, previous[path])
                else:  # pragma: no cover - a file that did not exist before
                    (root / path).unlink(missing_ok=True)
            except OSError:  # pragma: no cover - restoring is best effort
                pass
        if isinstance(e, RenameError):
            raise
        raise RenameError(f"Write failed, all files restored: {e}") from e


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` through a temporary neighbour."""
    tmp = path.with_name(f".{path.name}.havn-rename")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def schemas_from_models(models: list[Any]) -> dict[str, list[tuple[str, str]]]:
    """A best-effort schema map built from the models' own projections.

    Only used when neither the bind pass nor ``_havn.model_columns`` has
    anything to say: it knows the output column *names* of every model that
    does not project a star, with no types, which is enough to disambiguate an
    unqualified reference and to catch a collision.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    for model in models:
        parsed = getattr(model, "ast", None) or parse_sql(
            model.query or strip_config_comments(model.sql)
        )
        if parsed is None:
            continue
        names: list[tuple[str, str]] = []
        for select in _output_selects(parsed):
            if _has_star(select):
                names = []
                break
            for projection in select.expressions:
                name = _unquote(projection.alias_or_name or "")
                if name and name != "*":
                    names.append((name.lower(), ""))
        if names:
            found[model.full_name] = names
    return found

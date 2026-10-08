"""Enumerate the CTEs in a SQL buffer and build a preview query for each.

The preview is assembled by slicing the original text rather than generating
SQL from the parsed tree. A sqlglot round trip reformats everything and can
drift on DuckDB-specific syntax; the point of previewing a CTE is to run what
the author actually wrote.

sqlglot gives each CTE's alias ``Identifier`` a ``meta`` with ``line`` and a
``start`` byte offset. From that offset the CTE body is the next balanced
parenthesis group, found by a scan that knows about string literals and
comments, so the slice is ``name AS ( ... )`` verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass


class CteParseError(ValueError):
    """The buffer's CTEs could not be turned into previews."""


@dataclass
class CteSlice:
    """One CTE, located in the buffer and ready to preview."""

    name: str
    start_line: int
    end_line: int
    preview_sql: str


def _match_paren(text: str, open_index: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``open_index``.

    Skips over single- and double-quoted strings, dollar-quoted strings,
    line comments and block comments, so a parenthesis inside a literal does
    not throw the count off.
    """
    depth = 0
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or ch == '"':
            quote = ch
            i += 1
            while i < n:
                if text[i] == quote:
                    if i + 1 < n and text[i + 1] == quote:
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if ch == "-" and text.startswith("--", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline + 1
            continue
        if ch == "/" and text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise CteParseError("Unbalanced parentheses in the CTE list.")


def _line_of(text: str, index: int) -> int:
    """1-based line number of a byte offset."""
    return text.count("\n", 0, index) + 1


def _with_node(parsed):
    """The ``WITH`` node of a parsed statement, under either arg spelling."""
    args = getattr(parsed, "args", {}) or {}
    return args.get("with") or args.get("with_")


def enumerate_ctes(
    sql: str, line: int | None = None
) -> tuple[list[CteSlice], int | None]:
    """Find the CTEs in ``sql`` and build a preview query for each.

    Args:
        sql: The buffer text, directives already blanked out.
        line: A 1-based line the caller cares about, typically the cursor.

    Returns:
        ``(slices, active)`` where ``active`` indexes into ``slices`` for the
        CTE containing ``line``, or None.

    Raises:
        CteParseError: the SQL does not parse, or uses ``WITH RECURSIVE``.
            A recursive CTE cannot be previewed on its own: its definition
            refers to itself through the enclosing WITH, so lifting it out
            produces SQL that does not run.
    """
    import sqlglot
    from sqlglot import exp

    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except Exception as e:
        raise CteParseError(f"Could not parse SQL: {e}")
    if parsed is None:
        raise CteParseError("Could not parse SQL.")

    with_node = _with_node(parsed)
    if with_node is None:
        return [], None
    if with_node.args.get("recursive"):
        raise CteParseError(
            "Recursive CTEs cannot be previewed on their own: the definition "
            "refers to itself through the enclosing WITH."
        )

    spans: list[tuple[str, int, int]] = []  # (name, start offset, end offset)
    for cte in with_node.expressions:
        if not isinstance(cte, exp.CTE):
            continue
        alias = cte.args.get("alias")
        identifier = alias if isinstance(alias, exp.Identifier) else None
        if identifier is None and alias is not None:
            identifier = alias.find(exp.Identifier)
        meta = getattr(identifier, "meta", None) if identifier is not None else None
        start = meta.get("start") if meta else None
        if start is None:
            raise CteParseError(
                "Could not locate the CTE definitions in the source text."
            )
        open_index = sql.find("(", start)
        if open_index < 0:
            raise CteParseError(f"CTE '{cte.alias}' has no body.")
        close_index = _match_paren(sql, open_index)
        spans.append((cte.alias, start, close_index + 1))

    if not spans:
        return [], None

    slices: list[CteSlice] = []
    for index, (name, start, end) in enumerate(spans):
        body = ",\n".join(sql[s:e] for _, s, e in spans[: index + 1])
        preview = f"WITH {body}\nSELECT * FROM {name}"
        slices.append(
            CteSlice(
                name=name,
                start_line=_line_of(sql, start),
                end_line=_line_of(sql, end - 1),
                preview_sql=preview,
            )
        )

    active = None
    if line is not None:
        for index, item in enumerate(slices):
            if item.start_line <= line <= item.end_line:
                active = index
                break
    return slices, active

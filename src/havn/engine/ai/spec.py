"""The structured query spec a question compiles to.

A :class:`QuerySpec` is everything ``havn ask`` lets a language model decide:
which metric(s), which declared dimensions, which time grain, which dimension
filters, which time range, the ordering and a row limit. Nothing in it is SQL.
:func:`validate_spec` checks it against ``metrics/*.yml`` and
:func:`compile_spec` turns it into one SELECT with the semantic layer's own
compiler, so a spec answered through ``havn ask`` returns exactly what
``havn metrics query`` would for the same options.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from havn.engine.semantic import (
    FILTER_OPS,
    TIME_GRAINS,
    DimensionFilter,
    MetricDef,
    SemanticError,
    compile_metric,
)

MAX_METRICS = 5

_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$"
)


class SpecError(ValueError):
    """A spec does not fit the metric catalog. ``errors`` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class OrderTerm:
    field: str
    direction: str = "desc"

    def to_dict(self) -> dict:
        return {"field": self.field, "direction": self.direction}


@dataclass
class QuerySpec:
    metrics: list[str]
    dimensions: list[str] = field(default_factory=list)
    grain: str | None = None
    filters: list[DimensionFilter] = field(default_factory=list)
    start: str | None = None
    end: str | None = None
    order_by: list[OrderTerm] = field(default_factory=list)
    limit: int | None = None

    def to_dict(self) -> dict:
        return {
            "metrics": list(self.metrics),
            "dimensions": list(self.dimensions),
            "grain": self.grain,
            "filters": [f.to_dict() for f in self.filters],
            "start": self.start,
            "end": self.end,
            "order_by": [o.to_dict() for o in self.order_by],
            "limit": self.limit,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "QuerySpec":
        """Lenient parse: models (and people) spell a few keys differently.

        Shape problems raise SpecError; whether the names exist is
        :func:`validate_spec`'s job.
        """
        if not isinstance(raw, dict):
            raise SpecError(["spec must be a JSON object"])
        errors: list[str] = []

        metrics = raw.get("metrics")
        if metrics is None and raw.get("metric"):
            metrics = [raw.get("metric")]
        metrics = _str_list(metrics, "metrics", errors)

        dims = raw.get("dimensions")
        if dims is None:
            dims = raw.get("group_by") or raw.get("by")
        dims = _str_list(dims, "dimensions", errors)

        grain = raw.get("grain", raw.get("time_grain"))
        grain = str(grain).strip().lower() if grain not in (None, "", "none", "null") else None

        filters: list[DimensionFilter] = []
        for f in raw.get("filters") or []:
            try:
                filters.append(DimensionFilter.from_any(f))
            except SemanticError as e:
                errors.append(str(e))

        order: list[OrderTerm] = []
        raw_order = raw.get("order_by") or []
        if isinstance(raw_order, (str, dict)):
            raw_order = [raw_order]
        for term in raw_order:
            if isinstance(term, str):
                name, _, direction = term.strip().partition(" ")
                order.append(OrderTerm(name.strip(), (direction or "desc").strip().lower()))
            elif isinstance(term, dict) and term.get("field"):
                order.append(
                    OrderTerm(
                        str(term["field"]).strip(),
                        str(term.get("direction") or "desc").strip().lower(),
                    )
                )
            else:
                errors.append(f"order_by entry not understood: {term!r}")

        limit = raw.get("limit")
        if limit in ("", None):
            limit = None
        else:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                errors.append(f"limit must be an integer, got {limit!r}")
                limit = None

        start = _opt_str(raw.get("start"))
        end = _opt_str(raw.get("end"))
        if errors:
            raise SpecError(errors)
        return cls(
            metrics=metrics,
            dimensions=dims,
            grain=grain,
            filters=filters,
            start=start,
            end=end,
            order_by=order,
            limit=limit,
        )


def _opt_str(value: Any) -> str | None:
    if value in (None, "", "null", "none"):
        return None
    return str(value).strip()


def _str_list(value: Any, label: str, errors: list[str]) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        errors.append(f"{label} must be a list of names")
        return []
    out: list[str] = []
    for v in value:
        name = str(v).strip()
        if name and name not in out:
            out.append(name)
    return out


def _resolve_names(spec: QuerySpec, metrics: dict[str, MetricDef]) -> None:
    """Match names case-insensitively against the catalog, in place."""
    lower = {name.lower(): name for name in metrics}
    spec.metrics = [lower.get(m.lower(), m) for m in spec.metrics]
    dims_known: dict[str, str] = {}
    for name in spec.metrics:
        m = metrics.get(name)
        if m is None:
            continue
        for d in m.dimensions + ([m.time_dimension] if m.time_dimension else []):
            dims_known.setdefault(d.lower(), d)
    spec.dimensions = [dims_known.get(d.lower(), d) for d in spec.dimensions]
    for f in spec.filters:
        f.dimension = dims_known.get(f.dimension.lower(), f.dimension)


def validate_spec(
    spec: QuerySpec,
    metrics: dict[str, MetricDef],
    *,
    max_rows: int = 50_000,
) -> list[str]:
    """Every way ``spec`` does not fit the catalog; empty when it compiles."""
    _resolve_names(spec, metrics)
    errors: list[str] = []
    if not spec.metrics:
        errors.append("no metric chosen")
    if len(spec.metrics) > MAX_METRICS:
        errors.append(f"at most {MAX_METRICS} metrics per question")
    chosen: list[MetricDef] = []
    for name in spec.metrics:
        m = metrics.get(name)
        if m is None:
            errors.append(f"unknown metric {name!r}")
        else:
            chosen.append(m)

    for d in spec.dimensions:
        for m in chosen:
            if d not in m.dimensions:
                declared = ", ".join(m.dimensions) or "none"
                errors.append(
                    f"dimension {d!r} is not declared on metric {m.name!r} (declared: {declared})"
                )

    if spec.grain is not None and spec.grain not in TIME_GRAINS:
        errors.append(f"invalid grain {spec.grain!r} (use one of: {', '.join(TIME_GRAINS)})")
    if spec.grain or spec.start or spec.end:
        for m in chosen:
            if not m.time_dimension:
                errors.append(
                    f"metric {m.name!r} has no time_dimension, so grain/start/end cannot apply"
                )
    for label, value in (("start", spec.start), ("end", spec.end)):
        if value is not None and not _DATE_RE.match(value):
            errors.append(f"{label} must be an ISO date like 2024-01-31, got {value!r}")
    if spec.start and spec.end and _DATE_RE.match(spec.start) and _DATE_RE.match(spec.end):
        if spec.start >= spec.end:
            errors.append(f"start {spec.start} must be before end {spec.end}")

    for f in spec.filters:
        if f.op not in FILTER_OPS:
            errors.append(f"invalid filter operator {f.op!r} (use one of: {', '.join(FILTER_OPS)})")
        for m in chosen:
            allowed = m.dimensions + ([m.time_dimension] if m.time_dimension else [])
            if f.dimension not in allowed:
                errors.append(
                    f"cannot filter metric {m.name!r} on {f.dimension!r} "
                    f"(filterable: {', '.join(allowed) or 'none'})"
                )

    orderable = set(spec.metrics) | set(spec.dimensions) | ({spec.grain} if spec.grain else set())
    for term in spec.order_by:
        if term.field not in orderable:
            errors.append(
                f"cannot order by {term.field!r} (use one of: {', '.join(sorted(orderable))})"
            )
        if term.direction not in ("asc", "desc"):
            errors.append(f"order direction must be asc or desc, got {term.direction!r}")

    if spec.limit is not None and not (1 <= spec.limit <= max_rows):
        errors.append(f"limit must be between 1 and {max_rows}")

    if not errors:
        # Last word: the compiler itself (catches anything the checks above
        # do not model, e.g. a filter value that is too long).
        try:
            compile_spec(spec, metrics)
        except SemanticError as e:
            errors.append(str(e))
    return list(dict.fromkeys(errors))


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def compile_spec(spec: QuerySpec, metrics: dict[str, MetricDef]) -> str:
    """One SELECT for the spec. Raises SemanticError on anything invalid.

    A single metric compiles exactly as ``havn metrics query`` would. Several
    metrics are compiled one by one with the same dimensions, grain, filters
    and range, then joined on those keys with FULL OUTER JOIN, so a region
    that has revenue but no refunds still shows up.
    """
    if not spec.metrics:
        raise SemanticError("no metric chosen")
    order = [(t.field, t.direction != "asc") for t in spec.order_by] or None
    chosen = []
    for name in spec.metrics:
        if name not in metrics:
            raise SemanticError(f"unknown metric {name!r}")
        chosen.append(metrics[name])

    if len(chosen) == 1:
        return compile_metric(
            chosen[0],
            dimensions=spec.dimensions,
            grain=spec.grain,
            start=spec.start,
            end=spec.end,
            limit=spec.limit,
            where=spec.filters,
            order_by=order,
        )

    keys = ([spec.grain] if spec.grain else []) + list(dict.fromkeys(spec.dimensions))
    ctes = []
    for m in chosen:
        inner = compile_metric(
            m,
            dimensions=spec.dimensions,
            grain=spec.grain,
            start=spec.start,
            end=spec.end,
            where=spec.filters,
        )
        indented = "\n".join("    " + line for line in inner.splitlines())
        ctes.append(f"{_q('m_' + m.name)} AS (\n{indented}\n)")

    select_cols = [_q(k) for k in keys] + [_q(m.name) for m in chosen]
    from_clause = _q("m_" + chosen[0].name)
    for m in chosen[1:]:
        if keys:
            using = ", ".join(_q(k) for k in keys)
            from_clause += f"\nFULL OUTER JOIN {_q('m_' + m.name)} USING ({using})"
        else:
            from_clause += f"\nCROSS JOIN {_q('m_' + m.name)}"

    lines = [
        "WITH " + ",\n".join(ctes),
        "SELECT " + ", ".join(select_cols),
        "FROM " + from_clause,
    ]
    if order:
        allowed = set(keys) | {m.name for m in chosen}
        terms = []
        for field_name, descending in order:
            if field_name not in allowed:
                raise SemanticError(f"cannot order by {field_name!r}")
            terms.append(f"{_q(field_name)} {'DESC' if descending else 'ASC'} NULLS LAST")
        lines.append("ORDER BY " + ", ".join(terms))
    elif keys:
        lines.append("ORDER BY " + ", ".join(str(i + 1) for i in range(len(keys))))
    if spec.limit is not None:
        if spec.limit <= 0:
            raise SemanticError("limit must be a positive integer")
        lines.append(f"LIMIT {int(spec.limit)}")
    return "\n".join(lines)

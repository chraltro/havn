"""Semantic layer: declarative metric definitions compiled to SQL.

Metrics are defined once in YAML files under ``metrics/`` in the project
root and can then be queried consistently from the CLI (``havn metrics``),
the API (``/api/semantic/*``), and the MCP server — no copy-pasted SQL.

.. code-block:: yaml

    # metrics/revenue.yml
    metrics:
      - name: revenue
        description: Total order revenue
        model: gold.orders            # table or view to aggregate over
        measure: SUM(amount)          # SQL aggregate expression
        dimensions: [region, status]  # columns allowed in group-by
        time_dimension: order_date    # column used for time-grain bucketing
        filters:
          - status != 'cancelled'     # always-applied WHERE predicates

``compile_metric`` turns a definition plus query-time options (group-by
dimensions, time grain, time range, limit) into a single read-only SELECT.
All identifiers are validated and literal values are escaped, so the
caller-supplied parts cannot inject SQL; the compiled statement should
still be run through ``havn.engine.sql_safety.validate_read_only_query``
as defense in depth (the API and MCP surfaces do).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from havn.engine.utils import validate_identifier

TIME_GRAINS = ("hour", "day", "week", "month", "quarter", "year")

_MAX_LITERAL_LEN = 64


class SemanticError(ValueError):
    """A metric definition or metric query is invalid."""


@dataclass
class MetricDef:
    """A single declared metric."""

    name: str
    model: str                      # schema-qualified table/view, e.g. "gold.orders"
    measure: str                    # SQL aggregate expression, e.g. "SUM(amount)"
    description: str = ""
    dimensions: list[str] = field(default_factory=list)
    time_dimension: str | None = None
    filters: list[str] = field(default_factory=list)
    source_path: str = ""           # file the metric was defined in (for error messages)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "measure": self.measure,
            "description": self.description,
            "dimensions": list(self.dimensions),
            "time_dimension": self.time_dimension,
            "filters": list(self.filters),
            "source_path": self.source_path,
        }


def _check_expression(expr: str, label: str) -> str:
    """Reject expressions that could break out of the compiled statement.

    Measures and filters are authored by the project owner (same trust level
    as transform SQL), but they are embedded into a generated statement, so
    statement separators and comment markers are still rejected — they can
    only ever be authoring mistakes.
    """
    if not expr or not expr.strip():
        raise SemanticError(f"{label} must not be empty")
    if any(tok in expr for tok in (";", "--", "/*")):
        raise SemanticError(f"{label} must not contain ';' or comment markers: {expr!r}")
    return expr.strip()


def _check_model(model: str) -> str:
    parts = model.split(".")
    if len(parts) not in (1, 2):
        raise SemanticError(f"model must be 'table' or 'schema.table', got {model!r}")
    for part in parts:
        try:
            validate_identifier(part, "model")
        except ValueError as e:
            raise SemanticError(str(e))
    return model


def _parse_metric(raw: dict, source_path: str) -> MetricDef:
    if not isinstance(raw, dict):
        raise SemanticError(f"metric entry must be a mapping, got {type(raw).__name__}")
    name = str(raw.get("name", "")).strip()
    try:
        validate_identifier(name, "metric name")
    except ValueError as e:
        raise SemanticError(str(e))

    model = _check_model(str(raw.get("model", "")).strip())
    measure = _check_expression(str(raw.get("measure", "")), f"metric {name!r}: measure")

    dimensions = raw.get("dimensions") or []
    if not isinstance(dimensions, list):
        raise SemanticError(f"metric {name!r}: dimensions must be a list")
    dims: list[str] = []
    for d in dimensions:
        try:
            dims.append(validate_identifier(str(d), "dimension"))
        except ValueError as e:
            raise SemanticError(f"metric {name!r}: {e}")

    time_dimension = raw.get("time_dimension")
    if time_dimension is not None:
        try:
            time_dimension = validate_identifier(str(time_dimension), "time_dimension")
        except ValueError as e:
            raise SemanticError(f"metric {name!r}: {e}")

    filters_raw = raw.get("filters") or []
    if not isinstance(filters_raw, list):
        raise SemanticError(f"metric {name!r}: filters must be a list")
    filters = [
        _check_expression(str(f), f"metric {name!r}: filter") for f in filters_raw
    ]

    return MetricDef(
        name=name,
        model=model,
        measure=measure,
        description=str(raw.get("description", "")),
        dimensions=dims,
        time_dimension=time_dimension,
        filters=filters,
        source_path=source_path,
    )


def load_metrics(project_dir: Path) -> tuple[dict[str, MetricDef], list[str]]:
    """Load all metric definitions from ``<project>/metrics/*.yml``.

    Returns ``(metrics, errors)``. Invalid files or entries are skipped and
    reported as error strings rather than aborting the whole load, so one
    broken definition doesn't take down the listing for everyone.
    """
    metrics_dir = Path(project_dir) / "metrics"
    metrics: dict[str, MetricDef] = {}
    errors: list[str] = []
    if not metrics_dir.is_dir():
        return metrics, errors

    files = sorted(
        p for p in metrics_dir.iterdir()
        if p.suffix in (".yml", ".yaml") and p.is_file()
    )
    for path in files:
        rel = path.name
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except Exception as e:
            errors.append(f"{rel}: invalid YAML ({e})")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{rel}: top level must be a mapping with a 'metrics' key")
            continue
        for entry in raw.get("metrics", []) or []:
            try:
                metric = _parse_metric(entry, source_path=rel)
            except SemanticError as e:
                errors.append(f"{rel}: {e}")
                continue
            if metric.name in metrics:
                errors.append(
                    f"{rel}: duplicate metric {metric.name!r} "
                    f"(first defined in {metrics[metric.name].source_path})"
                )
                continue
            metrics[metric.name] = metric
    return metrics, errors


def get_metric(project_dir: Path, name: str) -> MetricDef:
    """Look up a single metric by name, raising SemanticError if unknown."""
    metrics, errors = load_metrics(project_dir)
    if name in metrics:
        return metrics[name]
    for err in errors:
        # Surface a load error for the requested metric instead of "unknown".
        if f"'{name}'" in err or f'"{name}"' in err:
            raise SemanticError(f"metric {name!r} failed to load: {err}")
    available = ", ".join(sorted(metrics)) or "none defined"
    raise SemanticError(f"unknown metric {name!r} (available: {available})")


def _sql_literal(value: str, label: str) -> str:
    """Quote a user-supplied value as a SQL string literal."""
    value = str(value)
    if len(value) > _MAX_LITERAL_LEN:
        raise SemanticError(f"{label} value too long (max {_MAX_LITERAL_LEN} chars)")
    if "\x00" in value:
        raise SemanticError(f"{label} value contains NUL")
    return "'" + value.replace("'", "''") + "'"


def compile_metric(
    metric: MetricDef,
    *,
    dimensions: list[str] | None = None,
    grain: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
) -> str:
    """Compile a metric plus query-time options into a SELECT statement.

    Args:
        metric: The metric definition.
        dimensions: Group-by columns; must be a subset of the metric's
            declared dimensions.
        grain: Time bucket (one of :data:`TIME_GRAINS`); requires the metric
            to declare a ``time_dimension``.
        start, end: Inclusive lower / exclusive upper bound on the time
            dimension (any string DuckDB can cast to TIMESTAMP).
        limit: Optional row cap appended as LIMIT.
    """
    dimensions = list(dimensions or [])
    for d in dimensions:
        if d not in metric.dimensions:
            declared = ", ".join(metric.dimensions) or "none declared"
            raise SemanticError(
                f"dimension {d!r} is not declared for metric {metric.name!r} "
                f"(declared: {declared})"
            )
    # De-duplicate while preserving order, so "--by region --by region"
    # doesn't produce an ambiguous duplicate column.
    dimensions = list(dict.fromkeys(dimensions))

    if grain is not None and grain not in TIME_GRAINS:
        raise SemanticError(f"invalid grain {grain!r} (use one of: {', '.join(TIME_GRAINS)})")
    if (grain or start or end) and not metric.time_dimension:
        raise SemanticError(
            f"metric {metric.name!r} has no time_dimension; "
            "grain/start/end are not supported"
        )

    select_parts: list[str] = []
    if grain:
        select_parts.append(
            f"DATE_TRUNC('{grain}', {metric.time_dimension}) AS {grain}"
        )
    select_parts.extend(dimensions)
    select_parts.append(f"{metric.measure} AS {metric.name}")
    n_group = len(select_parts) - 1

    where_parts = [f"({f})" for f in metric.filters]
    if start is not None:
        where_parts.append(
            f"{metric.time_dimension} >= CAST({_sql_literal(start, 'start')} AS TIMESTAMP)"
        )
    if end is not None:
        where_parts.append(
            f"{metric.time_dimension} < CAST({_sql_literal(end, 'end')} AS TIMESTAMP)"
        )

    lines = [
        "SELECT",
        "    " + ",\n    ".join(select_parts),
        f"FROM {metric.model}",
    ]
    if where_parts:
        lines.append("WHERE " + "\n  AND ".join(where_parts))
    if n_group:
        positions = ", ".join(str(i + 1) for i in range(n_group))
        lines.append(f"GROUP BY {positions}")
        lines.append(f"ORDER BY {positions}")
    if limit is not None:
        if not isinstance(limit, int) or limit <= 0:
            raise SemanticError(f"limit must be a positive integer, got {limit!r}")
        lines.append(f"LIMIT {limit}")
    return "\n".join(lines)

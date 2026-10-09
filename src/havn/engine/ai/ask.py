"""Answer a question from the semantic layer.

The flow, for every surface (CLI, ``/api/ask``, the ``ask`` MCP tool):

1. Load ``metrics/*.yml`` and build the catalog (metadata only).
2. Ask the model for a JSON decision: a :class:`QuerySpec` over that catalog,
   "unanswerable" (with the closest metrics and, where the models allow it, a
   metric definition to add), or a clarifying question.
3. Validate the spec. An invalid spec goes back to the model once with the
   errors; a spec that is still invalid is reported, never guessed at.
4. Compile it with the semantic layer and run it through the governed read
   path (:func:`havn.engine.read_path.run_read_query`) as the asking user.
5. Attach what the number rests on: the spec, the SQL, the metric
   definitions, the lineage of each model to its sources and the freshness of
   every model in that chain.

Exploratory SQL is a separate, explicit step: only when ``ai.exploratory_sql``
is on and the caller asks for it, and its answer is labelled unverified. It
still runs through the governed read path.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from havn.engine.ai.catalog import (
    build_catalog,
    fetch_column_types,
    fetch_dimension_values,
    freshness_for,
    lineage_for,
    rank_metrics,
)
from havn.engine.ai.config import AIConfig
from havn.engine.ai.providers import LLMProvider, ProviderError
from havn.engine.ai.spec import QuerySpec, SpecError, compile_spec, validate_spec
from havn.engine.semantic import MetricDef, SemanticError, load_metrics

logger = logging.getLogger("havn.ai")

MAX_HISTORY = 10
MAX_QUESTION_LEN = 2000
_SPEC_ATTEMPTS = 2
_SUMMARY_ROWS = 50


@dataclass
class AskTurn:
    """One earlier exchange, sent back so a follow-up can refine it."""

    question: str
    spec: dict | None = None

    @classmethod
    def from_any(cls, raw: Any) -> "AskTurn | None":
        if isinstance(raw, AskTurn):
            return raw
        if not isinstance(raw, dict) or not str(raw.get("question") or "").strip():
            return None
        spec = raw.get("spec")
        return cls(question=str(raw["question"])[:MAX_QUESTION_LEN], spec=spec if isinstance(spec, dict) else None)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_FILTER_VALUE_SCHEMA = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
    ]
}

_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {"type": "array", "items": {"type": "string"}},
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "grain": {"type": ["string", "null"]},
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string"},
                    "op": {"type": "string"},
                    "value": _FILTER_VALUE_SCHEMA,
                },
                "required": ["dimension", "op", "value"],
                "additionalProperties": False,
            },
        },
        "start": {"type": ["string", "null"]},
        "end": {"type": ["string", "null"]},
        "order_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                },
                "required": ["field", "direction"],
                "additionalProperties": False,
            },
        },
        "limit": {"type": ["integer", "null"]},
    },
    "required": ["metrics", "dimensions", "grain", "filters", "start", "end", "order_by", "limit"],
    "additionalProperties": False,
}

_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "model": {"type": "string"},
        "measure": {"type": "string"},
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "time_dimension": {"type": ["string", "null"]},
        "filters": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "description", "model", "measure", "dimensions", "time_dimension", "filters"],
    "additionalProperties": False,
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["query", "unanswerable", "clarify"]},
        "spec": {"anyOf": [_SPEC_SCHEMA, {"type": "null"}]},
        "explanation": {"type": "string"},
        "closest_metrics": {"type": "array", "items": {"type": "string"}},
        "suggested_metric": {"anyOf": [_SUGGESTION_SCHEMA, {"type": "null"}]},
        "clarification": {"type": ["string", "null"]},
    },
    "required": ["kind", "spec", "explanation", "closest_metrics", "suggested_metric", "clarification"],
    "additionalProperties": False,
}

_EXPLORATORY_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "explanation": {"type": "string"}},
    "required": ["sql", "explanation"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are the query planner behind "havn ask", a question box over a company's \
semantic layer. You turn a business question into a structured query over the \
metrics in the catalog below. You never write SQL and you never invent metrics, \
dimensions or columns: every name you use must appear in the catalog.

Reply with one JSON object with these keys:
- "kind": "query" when the catalog can answer the question, "unanswerable" when \
no metric measures what was asked, "clarify" when the question fits more than \
one metric and you cannot tell which.
- "spec" (for "query", else null): {"metrics": [...], "dimensions": [...], \
"grain": one of hour/day/week/month/quarter/year or null, "filters": \
[{"dimension": ..., "op": one of = != > >= < <= in "not in", "value": ...}], \
"start": ISO date or null (inclusive), "end": ISO date or null (exclusive), \
"order_by": [{"field": a chosen metric, dimension or the grain, "direction": \
"asc"|"desc"}], "limit": integer or null}.
- "explanation": one or two sentences saying what the query measures, in the \
user's terms. For "unanswerable", say plainly what is missing.
- "closest_metrics": for "unanswerable" or "clarify", the catalog metrics that \
come nearest; otherwise [].
- "suggested_metric": for "unanswerable" only, and only when one of the listed \
models has the columns to measure it: {"name", "description", "model", \
"measure" (a SQL aggregate over that model's columns, e.g. SUM(amount)), \
"dimensions", "time_dimension", "filters"}. Otherwise null.
- "clarification": for "clarify", one short question back to the user; else null.

Rules:
- A dimension or filter must be declared on every metric you choose.
- Several metrics in one spec share the same dimensions, grain, filters and range.
- "by month", "monthly", "per week" set the grain. "top 5 X by Y" orders by Y \
descending with limit 5. A single number needs no dimensions.
- Today is {today}. Turn relative ranges ("last month", "this year", "Q1 2024", \
"since March") into start/end dates; end is exclusive.
- Filter values: when a dimension lists its values, use one of them exactly \
(match "Norway" to "NO" if that is how the data spells it). Otherwise use the \
user's wording.
- A follow-up ("now by month", "only Norway", "and refunds?") refines the \
previous spec: keep what it did not mention, change what it did.
- Do not stretch a metric to mean something else. If the question asks for a \
measure the catalog does not have, answer "unanswerable".

Catalog:
{catalog}
"""

EXPLORATORY_PROMPT = """\
The question below could not be answered from the semantic layer's metrics. \
Write one read-only DuckDB SELECT that answers it, using only the tables and \
columns listed. Reply with JSON: {"sql": "...", "explanation": "..."}. \
The answer will be shown to the user as unverified exploratory SQL.

Tables:
{tables}
"""

SUMMARY_PROMPT = """\
Summarise this query result in two or three plain sentences for a business \
reader. Mention the biggest values and any obvious trend. Do not speculate \
beyond the rows. Reply with JSON: {"summary": "..."}.
"""


def _render_prompt(catalog: dict) -> str:
    # str.replace rather than str.format: the prompt is full of literal braces.
    return SYSTEM_PROMPT.replace("{today}", catalog["today"]).replace(
        "{catalog}", json.dumps(catalog, indent=1, default=str)
    )


def build_messages(question: str, history: list[AskTurn]) -> list[dict[str, str]]:
    """The conversation sent to the model: earlier turns, then the question."""
    messages: list[dict[str, str]] = []
    for turn in history[-MAX_HISTORY:]:
        messages.append({"role": "user", "content": turn.question})
        if turn.spec:
            messages.append({
                "role": "assistant",
                "content": json.dumps({"kind": "query", "spec": turn.spec}),
            })
        else:
            messages.append({
                "role": "assistant",
                "content": json.dumps({"kind": "unanswerable", "spec": None}),
            })
    messages.append({"role": "user", "content": question})
    return messages


# ---------------------------------------------------------------------------
# Chart choice
# ---------------------------------------------------------------------------


def suggest_chart(spec: QuerySpec | None, columns: list[str], rows: list[list]) -> dict:
    """Pick a chart for a result from its shape. ``type`` "table" means none."""
    if not rows:
        return {"type": "table"}
    if spec is None:
        return {"type": "table"}
    metric_cols = [m for m in spec.metrics if m in columns]
    if not metric_cols:
        return {"type": "table"}
    if not spec.grain and not spec.dimensions and len(rows) == 1:
        return {"type": "number", "y": metric_cols}
    if spec.grain and spec.grain in columns:
        series = None
        if len(spec.dimensions) == 1:
            dim = spec.dimensions[0]
            distinct = {r[columns.index(dim)] for r in rows}
            if len(distinct) <= 12:
                series = dim
        if len(spec.dimensions) <= 1:
            return {"type": "line", "x": spec.grain, "y": metric_cols, "series": series}
        return {"type": "table"}
    if spec.dimensions and len(rows) <= 50:
        series = spec.dimensions[1] if len(spec.dimensions) == 2 else None
        if len(spec.dimensions) <= 2:
            return {"type": "bar", "x": spec.dimensions[0], "y": metric_cols, "series": series}
    return {"type": "table"}


# ---------------------------------------------------------------------------
# Suggested metric
# ---------------------------------------------------------------------------


def _check_suggestion(
    raw: Any,
    metrics: dict[str, MetricDef],
    model_columns: dict[str, list[tuple[str, str]]],
    project_dir: Path,
) -> dict | None:
    """Validate a suggested metric definition and render it as YAML."""
    from havn.engine.semantic import _parse_metric

    if not isinstance(raw, dict) or not raw.get("name") or not raw.get("model"):
        return None
    definition = {
        "name": str(raw.get("name", "")).strip(),
        "description": str(raw.get("description") or "").strip(),
        "model": str(raw.get("model", "")).strip().lower(),
        "measure": str(raw.get("measure") or "").strip(),
        "dimensions": [str(d) for d in raw.get("dimensions") or []],
    }
    if raw.get("time_dimension"):
        definition["time_dimension"] = str(raw["time_dimension"])
    if raw.get("filters"):
        definition["filters"] = [str(f) for f in raw["filters"]]

    problems: list[str] = []
    try:
        _parse_metric(definition, source_path="suggestion")
    except SemanticError as e:
        problems.append(str(e))
    if definition["name"] in metrics:
        problems.append(f"a metric named {definition['name']!r} already exists")
    cols = {c.lower() for c, _ in model_columns.get(definition["model"], [])}
    if not cols:
        problems.append(f"model {definition['model']!r} is not in the warehouse catalog")
    else:
        for d in definition["dimensions"] + (
            [definition["time_dimension"]] if definition.get("time_dimension") else []
        ):
            if d.lower() not in cols:
                problems.append(f"column {d!r} does not exist on {definition['model']}")
    if problems:
        return {"definition": definition, "errors": problems, "yaml": None, "path": None}

    path = f"metrics/{definition['name']}.yml"
    if (project_dir / path).exists():
        path = f"metrics/{definition['name']}_suggested.yml"
    return {
        "definition": definition,
        "errors": [],
        "yaml": yaml.safe_dump({"metrics": [definition]}, sort_keys=False, allow_unicode=True),
        "path": path,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@dataclass
class AskContext:
    """Everything one question needs: where, as whom, and through which model."""

    project_dir: Path
    conn: Any  # a read cursor on the warehouse, or None when there is none yet
    user: dict
    provider: LLMProvider
    ai: AIConfig
    project_config: Any = None
    models: list = field(default_factory=list)

    def read(self, sql: str, limit: int | None = None):
        """Run SQL through the governed read path as the asking user."""
        from havn.engine.read_path import run_read_query

        if self.conn is None:
            raise RuntimeError("No warehouse database found. Run a pipeline first.")
        return run_read_query(self.conn, sql, user=self.user, limit=limit, task_label="ask")


@dataclass
class CatalogInfo:
    catalog: dict
    column_types: dict
    shared_values: bool = False


def prepare_catalog(ctx: AskContext, metrics: dict[str, MetricDef]) -> CatalogInfo:
    """Build the catalog for ``ctx``: metadata only, unless values are opted in."""
    tables = sorted({m.model.lower() for m in metrics.values()} | {
        m.full_name for m in ctx.models if m.materialized != "ephemeral"
    })
    column_types = fetch_column_types(lambda sql: ctx.read(sql), tables) if ctx.conn is not None else {}
    dim_values: dict = {}
    if ctx.ai.share_dimension_values and ctx.conn is not None and metrics:
        dim_values = fetch_dimension_values(lambda sql: ctx.read(sql), metrics, column_types)
    catalog = build_catalog(metrics, ctx.models, column_types=column_types, dimension_values=dim_values)
    return CatalogInfo(catalog=catalog, column_types=column_types, shared_values=bool(dim_values))


@dataclass
class Plan:
    """The model's decision for one question, after validation."""

    kind: str
    decision: dict
    spec: QuerySpec | None
    errors: list[str]
    attempts: int


def plan(
    question: str,
    ctx: AskContext,
    metrics: dict[str, MetricDef],
    catalog: dict,
    history: list[AskTurn] | None = None,
) -> Plan:
    """Ask the model for a decision and validate it against the catalog.

    An invalid spec is sent back once with the errors. A spec that is still
    invalid comes back with ``spec=None`` and the errors, and :func:`ask`
    reports it as unanswerable rather than guessing. Raises ProviderError.
    """
    system = _render_prompt(catalog)
    messages = build_messages(question, list(history or []))
    decision: dict = {}
    spec: QuerySpec | None = None
    errors: list[str] = []
    attempts = 0
    for attempts in range(1, _SPEC_ATTEMPTS + 1):
        decision = ctx.provider.complete_json(system=system, messages=messages, schema=DECISION_SCHEMA)
        if not isinstance(decision, dict):
            decision = {}
        kind = str(decision.get("kind") or "").lower()
        if kind != "query":
            break
        try:
            spec = QuerySpec.from_dict(decision.get("spec") or {})
            errors = validate_spec(spec, metrics, max_rows=ctx.ai.max_rows)
        except SpecError as e:
            errors = e.errors
        if not errors:
            break
        spec = None
        messages = messages + [
            {"role": "assistant", "content": json.dumps(decision, default=str)},
            {
                "role": "user",
                "content": "That spec does not fit the catalog: "
                + "; ".join(errors)
                + ". Fix it using only catalog names, or answer \"unanswerable\" "
                "if the catalog cannot answer the question.",
            },
        ]
    kind = str(decision.get("kind") or "").lower()
    if kind not in ("query", "unanswerable", "clarify"):
        kind = "unanswerable"
    return Plan(kind=kind, decision=decision, spec=spec, errors=errors, attempts=attempts)


def _freshness_hours(project_config) -> float:
    try:
        return float(project_config.alerts.freshness_hours)
    except Exception:
        return 24.0


def ask(
    question: str,
    ctx: AskContext,
    *,
    history: list | None = None,
    exploratory: bool = False,
    summarize: bool = False,
) -> dict:
    """Answer ``question``. Returns the answer as a JSON-serialisable dict.

    ``status`` is one of ``answered``, ``unanswerable``, ``clarify``,
    ``exploratory`` (unverified SQL fallback) or ``error``.
    """
    started = time.perf_counter()
    question = (question or "").strip()
    turns = [t for t in (AskTurn.from_any(h) for h in (history or [])) if t is not None]
    out: dict[str, Any] = {
        "question": question,
        "status": "error",
        "verified": False,
        "provider": ctx.provider.describe(),
        "data_sent_to_model": {"catalog": True, "dimension_values": False, "result_rows": False},
        "warnings": [],
        "spec": None,
        "sql": None,
        "result": None,
        "chart": {"type": "table"},
        "metrics": [],
        "models": [],
        "lineage": [],
        "freshness": [],
        "explanation": "",
        "closest_metrics": [],
        "suggested_metric": None,
        "clarification": None,
        "attempts": 0,
    }
    if not question:
        out["error"] = "Ask a question."
        return out
    if len(question) > MAX_QUESTION_LEN:
        out["error"] = f"Questions are limited to {MAX_QUESTION_LEN} characters."
        return out

    metrics, load_errors = load_metrics(ctx.project_dir)
    for err in load_errors:
        out["warnings"].append(f"metric definition error: {err}")

    catalog_info = prepare_catalog(ctx, metrics)
    column_types = catalog_info.column_types
    out["data_sent_to_model"]["dimension_values"] = catalog_info.shared_values

    try:
        planned = plan(question, ctx, metrics, catalog_info.catalog, turns)
    except ProviderError as e:
        out["error"] = f"The language model could not be used: {e}"
        return out
    decision, spec, spec_errors = planned.decision, planned.spec, planned.errors
    out["attempts"] = planned.attempts

    out["explanation"] = str(decision.get("explanation") or "")
    kind = planned.kind

    if kind == "query" and spec is not None:
        _answer_from_spec(out, spec, metrics, ctx, column_types)
        if summarize and out["status"] == "answered":
            _summarize(out, ctx)
        out["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return out

    if kind == "clarify":
        out["status"] = "clarify"
        out["clarification"] = str(decision.get("clarification") or out["explanation"] or "Which metric do you mean?")
        out["closest_metrics"] = _closest(question, decision, metrics)
        out["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return out

    # Unanswerable: the model said so, or kept producing specs that do not
    # fit. Either way, say it plainly and do not guess.
    out["status"] = "unanswerable"
    if kind == "query" and spec_errors:
        out["explanation"] = (
            "The question could not be mapped onto the defined metrics: "
            + "; ".join(spec_errors)
        )
    elif not metrics:
        out["explanation"] = out["explanation"] or (
            "No metrics are defined yet (metrics/*.yml), so there is nothing to answer from."
        )
    elif not out["explanation"]:
        out["explanation"] = "None of the defined metrics measures what this question asks for."
    out["closest_metrics"] = _closest(question, decision, metrics)
    out["suggested_metric"] = _check_suggestion(
        decision.get("suggested_metric"), metrics, column_types, ctx.project_dir
    )
    out["exploratory_available"] = bool(ctx.ai.exploratory_sql)
    if exploratory:
        if not ctx.ai.exploratory_sql:
            out["warnings"].append(
                "Exploratory SQL is off for this project (set ai.exploratory_sql: true to allow it)."
            )
        else:
            _exploratory(out, question, ctx, column_types)
    out["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return out


def _closest(question: str, decision: dict, metrics: dict[str, MetricDef]) -> list[dict]:
    named = []
    for name in decision.get("closest_metrics") or []:
        name = str(name)
        if name in metrics and name not in named:
            named.append(name)
    out = [{"name": n, "description": metrics[n].description} for n in named[:5]]
    if len(out) < 3:
        for entry in rank_metrics(question, metrics, top=5):
            if entry["name"] not in named and len(out) < 3:
                out.append({"name": entry["name"], "description": entry["description"]})
    return out


def _answer_from_spec(
    out: dict,
    spec: QuerySpec,
    metrics: dict[str, MetricDef],
    ctx: AskContext,
    column_types: dict,
) -> None:
    from havn.engine.read_path import (
        MaskedColumnAccessError,
        QueryTimeoutError,
        ReadOnlyQueryError,
    )

    sql = compile_spec(spec, metrics)
    out["spec"] = spec.to_dict()
    out["sql"] = sql
    used = [metrics[name] for name in spec.metrics]
    out["metrics"] = [m.to_dict() for m in used]
    model_names = list(dict.fromkeys(m.model.lower() for m in used))
    out["models"] = model_names

    lineage = [lineage_for(name, ctx.models) for name in model_names]
    out["lineage"] = lineage
    chain_models = list(dict.fromkeys(
        node["name"] for lin in lineage for node in lin["nodes"] if node["kind"] == "model"
    ))
    if ctx.conn is not None:
        out["freshness"] = freshness_for(ctx.conn, chain_models, _freshness_hours(ctx.project_config))
        for f in out["freshness"]:
            if f.get("is_stale"):
                out["warnings"].append(
                    f"{f['model']} was last built {f['hours_since_run']}h ago, "
                    f"past the {_freshness_hours(ctx.project_config):g}h freshness window."
                )
            elif f.get("never_built") and f["model"] in model_names:
                out["warnings"].append(f"{f['model']} has no recorded build.")

    if ctx.conn is None:
        out["status"] = "error"
        out["error"] = "No warehouse database found. Run a pipeline first."
        return
    try:
        result = ctx.read(sql, limit=ctx.ai.max_rows)
    except ReadOnlyQueryError as e:
        out["error"] = f"The compiled query was rejected: {e}"
        return
    except MaskedColumnAccessError as e:
        out["error"] = str(e)
        out["status"] = "error"
        return
    except QueryTimeoutError as e:
        out["error"] = str(e)
        return
    except Exception as e:
        out["error"] = f"The query failed: {e}"
        return
    out["status"] = "answered"
    out["verified"] = True
    out["result"] = {
        "columns": result.columns,
        "column_types": result.column_types,
        "rows": _numeric_rows(result.column_types, result.rows),
        "row_count": result.row_count,
        "truncated": result.truncated,
        "masked": result.masked,
    }
    out["chart"] = suggest_chart(spec, result.columns, result.rows)


def _numeric_rows(column_types: list[str], rows: list[list]) -> list[list]:
    """DECIMAL and HUGEINT arrive as strings from the read path; make them numbers.

    The read path serialises them as text so no precision is lost in JSON. An
    answer is for reading and charting, where a float is what every consumer
    expects, so metric values come back as numbers.
    """
    convert = []
    for i, t in enumerate(column_types):
        t = t.upper()
        if t.startswith("DECIMAL") or t.startswith("NUMERIC"):
            convert.append((i, float))
        elif t in ("HUGEINT", "UHUGEINT"):
            convert.append((i, int))
    if not convert:
        return rows
    out = []
    for row in rows:
        row = list(row)
        for i, fn in convert:
            if isinstance(row[i], str):
                try:
                    row[i] = fn(row[i])
                except ValueError:
                    pass
        out.append(row)
    return out


def _summarize(out: dict, ctx: AskContext) -> None:
    if not ctx.ai.summarize_results:
        out["warnings"].append(
            "Result summaries are off for this project: they send result rows to the "
            "model (set ai.summarize_results: true to allow it)."
        )
        return
    result = out.get("result") or {}
    payload = {
        "question": out["question"],
        "columns": result.get("columns", []),
        "rows": (result.get("rows") or [])[:_SUMMARY_ROWS],
    }
    try:
        answer = ctx.provider.complete_json(
            system=SUMMARY_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        )
    except ProviderError as e:
        out["warnings"].append(f"Could not summarise the result: {e}")
        return
    out["summary"] = str(answer.get("summary") or "")
    out["data_sent_to_model"]["result_rows"] = True


def _exploratory(out: dict, question: str, ctx: AskContext, column_types: dict) -> None:
    from havn.engine.sql_safety import ReadOnlyQueryError, validate_read_only_query

    tables = "\n".join(
        f"- {name}: " + ", ".join(f"{c} {t}" for c, t in cols)
        for name, cols in sorted(column_types.items())
    ) or "(no tables found)"
    try:
        answer = ctx.provider.complete_json(
            system=EXPLORATORY_PROMPT.replace("{tables}", tables),
            messages=[{"role": "user", "content": question}],
            schema=_EXPLORATORY_SCHEMA,
        )
    except ProviderError as e:
        out["warnings"].append(f"Exploratory SQL failed: {e}")
        return
    sql = str(answer.get("sql") or "").strip()
    exp: dict[str, Any] = {
        "sql": sql,
        "explanation": str(answer.get("explanation") or ""),
        "verified": False,
        "result": None,
        "error": None,
    }
    out["exploratory"] = exp
    if not sql:
        exp["error"] = "The model returned no SQL."
        return
    try:
        validate_read_only_query(sql)
        result = ctx.read(sql, limit=ctx.ai.max_rows)
    except ReadOnlyQueryError as e:
        exp["error"] = f"Rejected by the read-only check: {e}"
        return
    except Exception as e:
        exp["error"] = f"The query failed: {e}"
        return
    exp["result"] = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }
    out["status"] = "exploratory"
    out["sql"] = sql

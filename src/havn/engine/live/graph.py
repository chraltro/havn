"""Which models are live, what each one consumes, and in what order to refresh.

A live model's *tracked inputs* are the sources whose watermark it consumes.
They are found by walking its dependencies:

- a raw table (anything that is not a model, typically ``landing.*``) is a
  tracked input in its own right;
- a live incremental model is a tracked input: it publishes its own
  watermark after each refresh;
- a view or an ephemeral model holds no data of its own, so it is looked
  through: its dependencies are walked instead;
- any other model (a table, a snapshot, an incremental that is not live)
  changes only on batch runs and is *not* tracked. The live model still
  reads it, it just does not refresh because of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from graphlib import TopologicalSorter

from havn.engine.transform.models import SQLModel, ValidationError

# {watermark} or {watermark:schema.table}
WATERMARK_RE = re.compile(
    r"\{watermark(?::([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*))?\}"
)

LIVE_MATERIALIZATIONS = ("incremental", "view")


def is_live_incremental(model: SQLModel) -> bool:
    return model.live and model.materialized == "incremental"


def _transparent(model: SQLModel) -> bool:
    return model.materialized in ("view", "ephemeral")


def resolve_dep(
    dep: str, model_map: dict[str, SQLModel], _seen: frozenset[str] = frozenset()
) -> list[str]:
    """The tracked sources reached through one dependency (may be empty)."""
    model = model_map.get(dep)
    if model is None:
        return [dep]
    if is_live_incremental(model):
        return [dep]
    if _transparent(model) and dep not in _seen:
        out: list[str] = []
        for inner in model.depends_on:
            for src in resolve_dep(inner, model_map, _seen | {dep}):
                if src not in out:
                    out.append(src)
        return out
    return []


def tracked_inputs(model: SQLModel, model_map: dict[str, SQLModel]) -> dict[str, list[str]]:
    """``{dependency as written: [tracked sources behind it]}``."""
    return {dep: resolve_dep(dep, model_map) for dep in model.depends_on}


def tracked_sources(model: SQLModel, model_map: dict[str, SQLModel]) -> list[str]:
    out: list[str] = []
    for sources in tracked_inputs(model, model_map).values():
        for s in sources:
            if s not in out:
                out.append(s)
    return out


def untracked_model_inputs(model: SQLModel, model_map: dict[str, SQLModel]) -> list[str]:
    """Model dependencies (looking through views) that only change on batch runs."""
    out: list[str] = []

    def walk(dep: str, seen: frozenset[str]) -> None:
        m = model_map.get(dep)
        if m is None or is_live_incremental(m) or dep in seen:
            return
        if _transparent(m):
            for inner in m.depends_on:
                walk(inner, seen | {dep})
            return
        if dep not in out:
            out.append(dep)

    for dep in model.depends_on:
        walk(dep, frozenset())
    return out


class WatermarkError(ValueError):
    """A ``{watermark}`` placeholder that does not name exactly one input."""


def resolve_placeholder_source(
    model: SQLModel, model_map: dict[str, SQLModel], named: str | None
) -> str:
    """The tracked source a ``{watermark}`` / ``{watermark:x}`` refers to."""
    inputs = tracked_inputs(model, model_map)
    if named:
        named = named.lower()
        if named in inputs:
            sources = inputs[named]
        elif any(named in srcs for srcs in inputs.values()):
            sources = [named]
        else:
            raise WatermarkError(
                f"{{watermark:{named}}} does not name an input of {model.full_name}. "
                f"Inputs: {', '.join(sorted(inputs)) or '(none)'}"
            )
        if len(sources) != 1:
            raise WatermarkError(
                f"{{watermark:{named}}} is ambiguous: {named} reads "
                + (", ".join(sources) if sources else "no live source")
                + ". Name one of its sources instead."
            )
        return sources[0]
    sources = tracked_sources(model, model_map)
    if len(sources) != 1:
        raise WatermarkError(
            f"{{watermark}} needs exactly one live input, but {model.full_name} has "
            + (f"{len(sources)} ({', '.join(sources)})" if sources else "none")
            + ". Write {watermark:schema.table} to say which."
        )
    return sources[0]


def placeholder_text(model: SQLModel) -> str:
    """Everything a watermark placeholder may appear in."""
    return f"{model.query}\n{model.incremental_filter or ''}"


@dataclass
class LiveGraph:
    """The live part of the project's DAG."""

    models: dict[str, SQLModel]
    order: list[str] = field(default_factory=list)       # live models, topological
    sources: dict[str, list[str]] = field(default_factory=dict)  # live model -> tracked sources
    consumers: dict[str, list[str]] = field(default_factory=dict)  # source -> live models

    @classmethod
    def build(cls, models: list[SQLModel]) -> "LiveGraph":
        model_map = {m.full_name: m for m in models}
        live = [m for m in models if m.live and m.materialized in LIVE_MATERIALIZATIONS]
        names = {m.full_name for m in live}
        # The project's own topological order, filtered: a live view between
        # two live tables (bronze -> silver view -> gold) has to exist before
        # gold builds, and every edge of the full DAG says so.
        sorter: TopologicalSorter = TopologicalSorter()
        for m in models:
            sorter.add(m.full_name, *[d for d in m.depends_on if d in model_map])
        try:
            order = [n for n in sorter.static_order() if n in names]
        except Exception:
            order = sorted(names)
        graph = cls(models=model_map, order=order)
        for name in order:
            m = model_map[name]
            srcs = tracked_sources(m, model_map) if m.materialized == "incremental" else []
            graph.sources[name] = srcs
            for s in srcs:
                graph.consumers.setdefault(s, []).append(name)
        return graph

    @property
    def refreshable(self) -> list[str]:
        """Live incremental models, in refresh order (views need no refresh)."""
        return [n for n in self.order if self.models[n].materialized == "incremental"]

    def live_upstreams(self, name: str) -> list[str]:
        """Live incremental models this one consumes directly."""
        return [s for s in self.sources.get(name, []) if s in self.models and s in self.sources]

    def downstream_of(self, source: str) -> list[str]:
        """Every live model that ends up refreshing when ``source`` advances."""
        hit: set[str] = set()
        frontier = [source]
        while frontier:
            current = frontier.pop()
            for consumer in self.consumers.get(current, []):
                if consumer not in hit:
                    hit.add(consumer)
                    frontier.append(consumer)
        return [n for n in self.order if n in hit]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_live(models: list[SQLModel], configs: dict[str, dict[str, str]]) -> list[ValidationError]:
    """Pre-flight every rule the live runner and the CDC apply path enforce.

    ``configs`` is each model's raw ``@config`` dict, for the checks that need
    to see what was written rather than what discovery made of it.
    """
    from havn.engine.sql_analysis import CDC_DELETE_POLICIES

    from .settings import is_valid_live_flag, parse_duration

    model_map = {m.full_name: m for m in models}
    errors: list[ValidationError] = []

    def err(model: SQLModel, msg: str, severity: str = "error") -> None:
        errors.append(ValidationError(model=model.full_name, severity=severity, message=msg))

    for model in models:
        config = configs.get(model.full_name, {})
        raw_live = config.get("live")
        if raw_live is not None and not is_valid_live_flag(raw_live):
            err(model, f"@config live={raw_live} is not true or false.")
        if "live_interval" in config:
            try:
                parse_duration(config["live_interval"])
            except ValueError as e:
                err(model, f"@config live_interval: {e}")
            if not model.live:
                err(model, "@config live_interval only applies to a live model (live=true); it is ignored.",
                    "warning")

        _validate_cdc(model, config, err, CDC_DELETE_POLICIES)

        placeholders = WATERMARK_RE.findall(placeholder_text(model))
        if placeholders and not model.live:
            err(model, "{watermark} placeholders only mean something on a live model; "
                "add live=true to @config.")

        if not model.live:
            continue

        if model.materialized not in LIVE_MATERIALIZATIONS:
            err(model,
                f"Live models must be materialized=incremental (or a view, which is always "
                f"live); this one is '{model.materialized}'. A live table would be rebuilt "
                "in full on every source commit. Make it incremental with a unique_key and "
                "an incremental_filter on {watermark}, or a view.")
            continue
        if model.materialized == "view":
            continue
        if model.incremental_strategy == "microbatch":
            err(model,
                "Live models cannot use incremental_strategy=microbatch: microbatch cuts "
                "time windows for backfills, a live model consumes source watermarks. Use "
                "merge, delete+insert or append with an incremental_filter on {watermark}.")
            continue
        if model.incremental_strategy == "append" and not model.incremental_filter and not model.watermark:
            err(model,
                "A live append model needs an incremental_filter (e.g. WHERE _havn_seq > "
                "{watermark}); without one every refresh would append the whole input again.")
        text = placeholder_text(model)
        if not placeholders and "{this}" not in text and not model.watermark:
            err(model,
                "Live model never reads incrementally: no {watermark}, {this} or watermark= "
                "in its SQL or incremental_filter, so every refresh re-reads all of its input.",
                "warning")
        for named in placeholders:
            try:
                resolve_placeholder_source(model, model_map, named or None)
            except WatermarkError as e:
                err(model, str(e))
        sources = tracked_sources(model, model_map)
        if not sources:
            err(model,
                "Live model has no live input: everything it reads is a batch-built model, "
                "so nothing will ever trigger a refresh.", "warning")
        for dep in untracked_model_inputs(model, model_map):
            err(model,
                f"Reads {dep}, which is not live: it changes only on batch runs and this model "
                "does not refresh when it does.", "warning")
    return errors


def _validate_cdc(model, config, err, policies) -> None:
    has_op, has_seq = bool(model.cdc_op), bool(model.cdc_seq)
    if not (has_op or has_seq or "cdc_deletes" in config):
        return
    if has_op != has_seq:
        err(model, "cdc_op and cdc_seq go together: cdc_op names the operation column, "
            "cdc_seq the column that orders changes to one key (an LSN).")
    if model.cdc_deletes not in policies:
        err(model, f"Unknown cdc_deletes '{model.cdc_deletes}'. Supported: "
            f"{', '.join(sorted(policies))}.")
    if "cdc_deletes" in config and not has_op:
        err(model, "cdc_deletes only applies together with cdc_op and cdc_seq.", "warning")
    if not has_op:
        return
    if model.materialized != "incremental" or model.incremental_strategy not in ("merge", "delete+insert"):
        err(model, "CDC apply (cdc_op/cdc_seq) needs materialized=incremental with "
            "incremental_strategy=merge or delete+insert.")
    if not model.unique_key:
        err(model, "CDC apply needs @config unique_key=<column>: the key whose versions "
            "cdc_seq orders.")
    from havn.engine.utils import validate_identifier

    for key in ("cdc_op", "cdc_seq"):
        value = getattr(model, key)
        if not value:
            continue
        try:
            validate_identifier(value, key)
        except ValueError as e:
            err(model, str(e))

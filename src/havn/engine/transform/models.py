"""Data classes for the SQL transformation engine."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlglot import exp


class _Unparsed:
    """Marker for "this model's SQL has not been parsed yet"."""


_UNPARSED = _Unparsed()


# A comment, a quoted span (string literal or quoted identifier), or a run of
# whitespace. Comments come first so an apostrophe in `-- customer's` does not
# open a "string" that runs on to the next quote.
_HASH_TOKEN_RE = re.compile(
    r"""(--[^\n]*|/\*.*?\*/)|('(?:[^']|'')*'?|"(?:[^"]|"")*"?)|(\s+)""",
    re.DOTALL,
)


def _normalize_ws(match: re.Match[str]) -> str:
    comment, quoted, _ws = match.groups()
    if quoted is not None:
        return quoted
    if comment is not None:
        return re.sub(r"\s+", " ", comment)
    return " "


def _hash_content(content: str) -> str:
    """Hash SQL content for change detection.

    Whitespace is collapsed everywhere except inside quoted spans:
    ``'a  b'`` and ``'a b'`` are different values, and collapsing them made
    that edit invisible. For any query whose literals hold no run of
    whitespace (nor a tab or newline) the result is byte-identical to the old
    collapse-everything rule, so existing stored hashes stay valid.
    """
    normalized = _HASH_TOKEN_RE.sub(_normalize_ws, content.strip())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _drop_leading_blank_lines(sql: str) -> str:
    """Drop whitespace-only lines from the front of ``sql``.

    ``strip_config_comments`` blanks directive lines in place instead of
    deleting them, to keep the line map intact, so a model's query now starts
    with as many blank lines as its header had. Everything else in the hash is
    whitespace-normalized, but a leading blank line does change the hash, and
    without this every already-built model in every project would rebuild once
    on upgrade for no reason.
    """
    lines = sql.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


@dataclass
class AssertionResult:
    """Result of a data quality assertion."""

    expression: str
    passed: bool
    detail: str = ""
    severity: str = "error"  # "error" halts downstream; "warn" continues
    owner: str = ""


@dataclass
class ProfileResult:
    """Auto-computed profile stats for a model after execution."""

    row_count: int = 0
    column_count: int = 0
    null_percentages: dict[str, float] = field(default_factory=dict)
    distinct_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class SQLModel:
    """A single SQL transformation model."""

    path: Path
    name: str  # e.g. "customers"
    schema: str  # e.g. "bronze"
    full_name: str  # e.g. "bronze.customers"
    sql: str  # raw SQL content
    query: str  # SQL without config comments
    materialized: str  # "view", "table", "incremental", "ephemeral" or "snapshot"
    depends_on: list[str] = field(default_factory=list)
    description: str = ""
    column_docs: dict[str, str] = field(default_factory=dict)
    content_hash: str = ""
    upstream_hash: str = ""
    assertions: list[str] = field(default_factory=list)
    # (expression, severity) pairs parsed from @assert lines. Mirrors
    # ``assertions`` in length/order so callers can index into either.
    assertion_specs: list[tuple[str, str]] = field(default_factory=list)
    unique_key: str | None = None  # For incremental models
    incremental_strategy: str = "delete+insert"  # "delete+insert", "append", or "merge"
    incremental_filter: str | None = None  # e.g. "WHERE updated_at > (SELECT MAX(updated_at) FROM {this})"
    partition_by: str | None = None  # e.g. "event_date" — enables partition-based pruning
    # What an incremental model does when its query's columns no longer line
    # up with the target table: "append_new_columns" (default), "ignore",
    # "fail" or "sync_all_columns".
    on_schema_change: str = "append_new_columns"
    watermark: str | None = None  # @watermark column for incremental models — auto-generates incremental_filter
    # --- Snapshot (SCD2) settings, read only when materialized="snapshot" ---
    # How a changed row is recognised: "check" hashes the tracked columns,
    # "timestamp" trusts ``updated_at``. Same names and values as dbt.
    strategy: str = "check"
    updated_at: str | None = None  # timestamp strategy: the source's change clock
    # "all" (or None) tracks every non-key column; otherwise a comma list.
    check_cols: str | None = None
    # What happens to a key that disappeared from the source:
    # "ignore" leaves history alone, "invalidate" closes the current row,
    # "new_record" closes it and appends a tombstone row.
    hard_deletes: str = "ignore"
    # --- Microbatch settings, read only with incremental_strategy=microbatch ---
    event_time: str | None = None  # the column each batch window is cut on
    batch_size: str | None = None  # "hour", "day", "month" or "year"
    begin: str | None = None  # the first window's date or timestamp, UTC
    # How many already-done windows to reprocess on each run, for late arrivals.
    lookback: int = 1
    # --- Live models (see havn.engine.live) ---
    # @config live=true: refreshed by the live runner whenever an input
    # advances. Deliberately left out of content_hash, like tags: turning a
    # model live changes who triggers its build, not what the build writes.
    live: bool = False
    # @config live_interval=10s: the least time between two live refreshes of
    # this model, in seconds. 0 means "as often as the runner cycles".
    live_interval: float = 0.0
    # --- CDC apply, for incremental merge / delete+insert models ---
    # Column in the query output holding the change operation (I/U/D, or
    # insert/update/delete), and the column that orders changes to one key
    # (an LSN or another monotonically increasing sequence).
    cdc_op: str | None = None
    cdc_seq: str | None = None
    # "hard" removes a deleted key's row (and remembers the delete in a
    # tombstone table so a replayed older event cannot resurrect it); "soft"
    # keeps the row with _havn_deleted = true so downstream models see it.
    cdc_deletes: str = "hard"
    grain: list[str] = field(default_factory=list)  # @grain columns; auto-asserts uniqueness post-build
    owner: str = ""  # @owner label for alert routing
    # @config tags=daily,finance -- labels for `tag:` selectors. Deliberately
    # left out of content_hash below: retagging a model must not rebuild it.
    tags: list[str] = field(default_factory=list)
    source_freshness: list[dict] = field(default_factory=list)  # @source_freshness specs
    # Name of the havn package this model came from, or None for a project
    # model. Set by package discovery, never by a @config directive, and
    # deliberately out of content_hash: it is provenance, not build semantics.
    package: str | None = None
    # Fingerprint of the macro files defining a function this model calls, or
    # "" when it calls none. Set by ``discover_all_models`` (which knows the
    # project's macros/), then folded into content_hash by
    # :meth:`refresh_content_hash` (but not into ``definition_hash``, which is
    # what descendants see): editing a macro has to rebuild the models
    # that call it, and only those, so every other model keeps its hash.
    macro_hash: str = ""

    def __post_init__(self) -> None:
        self.refresh_content_hash()

        # Plain attributes, deliberately not dataclass fields: the AST must
        # stay out of __eq__/__repr__ and out of the content hash above.
        self._ast_cache: object = _UNPARSED
        self._parse_error: str = ""

    def refresh_content_hash(self) -> None:
        """Recompute ``content_hash`` from the model's current fields."""
        # Hash everything that changes build semantics — not just the query —
        # so editing e.g. @config unique_key or incremental_strategy triggers
        # a rebuild. Only non-default values are appended, keeping hashes of
        # models without these settings stable across havn upgrades.
        parts = [f"{self.materialized}:{_drop_leading_blank_lines(self.query)}"]
        if self.unique_key:
            parts.append(f"unique_key={self.unique_key}")
        if self.incremental_strategy != "delete+insert":
            parts.append(f"incremental_strategy={self.incremental_strategy}")
        if self.incremental_filter:
            parts.append(f"incremental_filter={self.incremental_filter}")
        if self.partition_by:
            parts.append(f"partition_by={self.partition_by}")
        if self.watermark:
            parts.append(f"watermark={self.watermark}")
        if self.on_schema_change != "append_new_columns":
            parts.append(f"on_schema_change={self.on_schema_change}")
        # Snapshot settings change what a run writes into history, so a model
        # that switches from check to timestamp (or narrows check_cols) has to
        # be recognised as modified. Only non-defaults are folded in, so every
        # model that is not a snapshot keeps the hash it already had.
        if self.strategy != "check":
            parts.append(f"strategy={self.strategy}")
        if self.updated_at:
            parts.append(f"updated_at={self.updated_at}")
        if self.check_cols:
            parts.append(f"check_cols={self.check_cols}")
        if self.hard_deletes != "ignore":
            parts.append(f"hard_deletes={self.hard_deletes}")
        # Microbatch settings decide which rows a run even looks at, so a
        # changed batch_size or begin has to read as a modified model.
        if self.event_time:
            parts.append(f"event_time={self.event_time}")
        if self.batch_size:
            parts.append(f"batch_size={self.batch_size}")
        if self.begin:
            parts.append(f"begin={self.begin}")
        if self.lookback != 1:
            parts.append(f"lookback={self.lookback}")
        # CDC apply settings decide which version of a key survives and
        # whether a delete removes the row, so changing them is a rebuild.
        if self.cdc_op:
            parts.append(f"cdc_op={self.cdc_op}")
        if self.cdc_seq:
            parts.append(f"cdc_seq={self.cdc_seq}")
        if self.cdc_deletes != "hard":
            parts.append(f"cdc_deletes={self.cdc_deletes}")
        # Assertions and @grain are stripped out of `query` by
        # strip_config_comments, so without folding them in here, adding an
        # @assert to a model that is already built leaves content_hash
        # unchanged -- the model is skipped and the new assertion never runs.
        if self.assertion_specs:
            parts.append(
                "assert=" + ";".join(f"{e}@{s}" for e, s in self.assertion_specs)
            )
        elif self.assertions:
            parts.append("assert=" + ";".join(self.assertions))
        if self.grain:
            parts.append("grain=" + ",".join(self.grain))
        # The definition alone, without macros: this is what descendants fold
        # into their upstream hash. A macro edit rebuilds its callers (their
        # content_hash moves), and their descendants follow in the same run
        # through _parent_built; folding the fingerprint in transitively made
        # every model downstream of any caller look modified on its own.
        self.definition_hash = _hash_content("|".join(parts))
        if self.macro_hash:
            parts.append(f"macros={self.macro_hash}")
            self.content_hash = _hash_content("|".join(parts))
        else:
            self.content_hash = self.definition_hash

    @property
    def ast(self) -> exp.Expression | None:
        """The parsed ``query``, or None when it does not parse.

        Parsed once and kept. Discovery, validation, the deny-rule check and
        column lineage all want the same tree, and each used to call
        ``sqlglot.parse_one`` on the same SQL again -- four parses per model
        per pass, which dominates the cost of a full-project check.

        Assigning to it seeds the cache, which discovery does with the AST it
        already parsed to extract table references. ``query`` is never
        rewritten after construction, so the cache cannot go stale.
        """
        if isinstance(self._ast_cache, _Unparsed):
            from havn.engine.sql_analysis import parse_sql_with_error

            parsed, error = parse_sql_with_error(self.query)
            self._ast_cache = parsed
            self._parse_error = error
        return self._ast_cache  # type: ignore[return-value]

    @ast.setter
    def ast(self, value: exp.Expression) -> None:
        """Seed the cache with a tree the caller already parsed.

        Only successful parses are seeded; a model whose SQL did not parse is
        left alone so the failure message is captured on first access.
        """
        self._ast_cache = value
        self._parse_error = ""

    @property
    def parse_error(self) -> str:
        """Why ``query`` failed to parse, or "" when it parsed."""
        if isinstance(self._ast_cache, _Unparsed):
            _ = self.ast
        return self._parse_error


@dataclass
class ModelResult:
    """Full result from executing a single model."""

    status: str  # "built", "skipped", "error"
    duration_ms: int = 0
    row_count: int = 0
    error: str | None = None
    assertions: list[AssertionResult] = field(default_factory=list)
    profile: ProfileResult | None = None
    # Human-readable schema-evolution actions applied by an incremental run,
    # e.g. ["added column region VARCHAR", "dropped column legacy_id"]. Empty
    # for every other materialization and for runs that changed nothing.
    schema_changes: list[str] = field(default_factory=list)


@dataclass
class ValidationError:
    """A single validation error found during compile-time check."""

    model: str
    severity: str  # "error" or "warning"
    message: str
    line: int | None = None

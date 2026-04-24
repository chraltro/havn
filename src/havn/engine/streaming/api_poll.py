"""API poll consumer — scheduled HTTP polling for REST sources without webhooks.

Reads from a REST endpoint on a configurable interval, extracts rows via a
simple JSONPath, persists them to ``landing.{connector_name}``, and tracks
the high-watermark in ``_havn.cdc_state`` so each poll is incremental.

Config keys (all optional except those marked required):
    url                   (str, required) — API endpoint URL
    method                (str) — HTTP verb, default "GET"
    headers               (str|dict) — extra HTTP headers as JSON
    auth_header           (str) — value for the Authorization header
    json_path             (str) — dot-delimited path into the response JSON,
                                   e.g. "data" or "results.items"; default "$"
                                   (root)
    timeout               (int) — request timeout in seconds, default 30
    watermark_field       (str) — field name whose max becomes the next
                                   watermark (omit for full-refresh each poll)
    watermark_param       (str) — query-param name sent on incremental polls;
                                   defaults to watermark_field when omitted
    pagination_key        (str) — response key holding the next-page URL
    max_pages_per_poll    (int) — page-fetch cap per poll cycle, default 10
    poll_interval_seconds (int) — seconds between polls (default 60); used by
                                   run() when no explicit interval given
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs
from urllib.request import Request, urlopen

import duckdb

from havn.engine.cdc import ensure_cdc_table, get_watermark, update_watermark
from havn.engine.utils import validate_identifier

logger = logging.getLogger("havn.streaming.api_poll")

_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 1


@dataclass
class PollResult:
    rows_inserted: int
    new_watermark: str | None
    duration_ms: int
    error: str | None = None


@dataclass
class _PageResult:
    rows: list[dict]
    next_url: str | None


def _extract_json_path(obj: object, path: str) -> list[dict]:
    """Walk a dot-delimited path into the response object.

    ``path`` of ``"$"`` or ``""`` returns the root.  The path is never
    evaluated as code — only dict-key lookups are performed, so arbitrary
    user-supplied paths cannot inject behaviour beyond traversal.
    """
    if path in ("$", "", None):
        if isinstance(obj, list):
            return obj
        return [obj] if isinstance(obj, dict) else []

    parts = path.lstrip("$.").split(".")
    current: object = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return []
    if isinstance(current, list):
        return current
    if isinstance(current, dict):
        return [current]
    return []


def _append_watermark_param(url: str, param: str, value: str) -> str:
    """Return *url* with the watermark query parameter added or replaced."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs[param] = [value]
    new_query = urlencode({k: v[0] for k, v in qs.items()})
    return urlunparse(parsed._replace(query=new_query))


def _build_headers(config: dict) -> dict:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    extra = config.get("headers")
    if extra:
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError:
                extra = {}
        if isinstance(extra, dict):
            headers.update(extra)
    auth = config.get("auth_header")
    if auth:
        headers["Authorization"] = auth
    return headers


def _fetch_once(url: str, method: str, headers: dict, timeout: int) -> object:
    """Make one HTTP request with exponential backoff on transient errors.

    Raises the final exception when all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = Request(url, method=method, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw)
        except HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                wait = _RETRY_BASE_SECONDS * (2 ** attempt)
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        pass
                logger.warning(
                    "HTTP %s from %s, retry %d/%d in %ds",
                    exc.code, url, attempt + 1, _MAX_RETRIES, wait,
                )
                time.sleep(wait)
                last_exc = exc
            else:
                raise
        except (URLError, TimeoutError, OSError) as exc:
            wait = _RETRY_BASE_SECONDS * (2 ** attempt)
            logger.warning(
                "Network error fetching %s: %s, retry %d/%d in %ds",
                url, exc, attempt + 1, _MAX_RETRIES, wait,
            )
            time.sleep(wait)
            last_exc = exc

    raise RuntimeError(
        f"Failed after {_MAX_RETRIES} retries: {last_exc}"
    ) from last_exc


def _fetch_pages(
    start_url: str,
    method: str,
    headers: dict,
    timeout: int,
    json_path: str,
    pagination_key: str | None,
    max_pages: int,
) -> list[dict]:
    """Fetch one or more pages and return all extracted rows."""
    all_rows: list[dict] = []
    url = start_url
    for page_num in range(max_pages):
        data = _fetch_once(url, method, headers, timeout)
        rows = _extract_json_path(data, json_path)
        all_rows.extend(rows)

        if not pagination_key:
            break
        if not isinstance(data, dict):
            break
        next_url = data.get(pagination_key)
        if not next_url or not isinstance(next_url, str):
            break
        url = next_url

        if page_num == max_pages - 1:
            logger.warning(
                "Reached max_pages_per_poll (%d); additional pages skipped", max_pages
            )

    return all_rows


def _max_field_value(rows: list[dict], field_name: str) -> str | None:
    """Return the max stringified value of *field_name* across *rows*.

    String comparison is used so the caller is responsible for choosing a field
    whose natural sort matches lexicographic order (ISO timestamps work; raw
    integers with varying widths do not — callers should zero-pad or use ISO).
    """
    values = [str(r[field_name]) for r in rows if field_name in r]
    return max(values) if values else None


def _upsert_rows(
    conn: duckdb.DuckDBPyConnection,
    schema: str,
    table: str,
    rows: list[dict],
) -> int:
    """Insert *rows* into ``schema.table``, creating the table on first run.

    Writes rows to a temp JSON file and uses DuckDB's ``read_json_auto`` to
    infer schema and load data.  Returns the number of rows inserted.
    """
    import os
    import tempfile

    if not rows:
        return 0

    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    target = f'"{schema}"."{table}"'

    exists = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()[0] > 0

    # Write to a temp file; DuckDB read_json_auto requires a file path.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        safe_path = tmp_path.replace("\\", "/").replace("'", "''")
        if not exists:
            conn.execute(
                f"CREATE TABLE {target} AS SELECT * FROM read_json_auto('{safe_path}')"
            )
        else:
            conn.execute(
                f"INSERT INTO {target} SELECT * FROM read_json_auto('{safe_path}')"
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return len(rows)


class APIPollConsumer:
    """Poll a REST API endpoint on a schedule and load rows into the warehouse.

    Parameters
    ----------
    connector_name:
        Logical name for the connector; becomes the landing table name and the
        CDC state key.  Must satisfy ``validate_identifier``.
    config:
        Dict of polling parameters (see module docstring).
    project_dir:
        Root of the havn project (used to locate ``warehouse.duckdb``).
    """

    def __init__(
        self,
        connector_name: str,
        config: dict,
        project_dir: Path,
    ) -> None:
        validate_identifier(connector_name, "connector name")
        self.connector_name = connector_name
        self.config = config
        self.project_dir = Path(project_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_conn(self) -> duckdb.DuckDBPyConnection:
        from havn.config import load_project
        from havn.engine.database import open_warehouse

        config = load_project(self.project_dir)
        return open_warehouse(config, self.project_dir)

    def _resolve_url(self, watermark: str | None) -> str:
        url: str = self.config.get("url", "")
        watermark_field: str | None = self.config.get("watermark_field")
        watermark_param: str | None = self.config.get("watermark_param") or watermark_field
        if watermark and watermark_param:
            url = _append_watermark_param(url, watermark_param, watermark)
        return url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll_once(self) -> PollResult:
        """Execute a single poll cycle.

        1. Read current watermark from ``_havn.cdc_state``.
        2. Fetch page(s) from the API (with watermark param if incremental).
        3. Insert rows into ``landing.{connector_name}``.
        4. Compute new watermark from ``watermark_field``.
        5. Persist new watermark.
        """
        start = time.perf_counter()
        conn: duckdb.DuckDBPyConnection | None = None

        try:
            conn = self._open_conn()
            ensure_cdc_table(conn)

            # ---- 1. watermark -------------------------------------------
            watermark_field: str | None = self.config.get("watermark_field")
            current_watermark = (
                get_watermark(conn, self.connector_name, self.connector_name)
                if watermark_field
                else None
            )

            # ---- 2. fetch -----------------------------------------------
            url = self._resolve_url(current_watermark)
            method: str = self.config.get("method", "GET").upper()
            headers = _build_headers(self.config)
            timeout: int = int(self.config.get("timeout", 30))
            json_path: str = self.config.get("json_path", "$")
            pagination_key: str | None = self.config.get("pagination_key") or None
            max_pages: int = int(self.config.get("max_pages_per_poll", 10))

            rows = _fetch_pages(
                url, method, headers, timeout, json_path, pagination_key, max_pages
            )

            # ---- 3. insert ----------------------------------------------
            rows_inserted = _upsert_rows(conn, "landing", self.connector_name, rows)

            # ---- 4 & 5. watermark update --------------------------------
            new_watermark: str | None = None
            if watermark_field and rows:
                new_watermark = _max_field_value(rows, watermark_field)

            persisted_watermark = new_watermark or current_watermark
            update_watermark(
                conn,
                self.connector_name,
                self.connector_name,
                cdc_mode="high_watermark" if watermark_field else "full_refresh",
                watermark_value=persisted_watermark,
                rows_synced=rows_inserted,
            )

            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.info(
                "poll_once[%s]: %d rows inserted, watermark=%s (%dms)",
                self.connector_name, rows_inserted, persisted_watermark, duration_ms,
            )
            return PollResult(
                rows_inserted=rows_inserted,
                new_watermark=persisted_watermark,
                duration_ms=duration_ms,
            )

        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.error("poll_once[%s] failed: %s", self.connector_name, exc)
            return PollResult(
                rows_inserted=0,
                new_watermark=None,
                duration_ms=duration_ms,
                error=str(exc),
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def run(self, interval_seconds: int, stop_event: threading.Event) -> None:
        """Loop calling :meth:`poll_once` every *interval_seconds*.

        Each iteration starts only after the previous one completes plus the
        configured delay — polls never overlap even when one takes longer than
        the interval.
        """
        logger.info(
            "APIPollConsumer[%s] starting, interval=%ds",
            self.connector_name, interval_seconds,
        )
        while not stop_event.is_set():
            poll_start = time.monotonic()
            result = self.poll_once()

            if result.error:
                logger.warning(
                    "poll[%s] error (continuing): %s", self.connector_name, result.error
                )
            else:
                logger.info(
                    "poll[%s] ok: %d rows, watermark=%s",
                    self.connector_name, result.rows_inserted, result.new_watermark,
                )

            elapsed = time.monotonic() - poll_start
            if elapsed > interval_seconds:
                logger.warning(
                    "poll[%s] took %.1fs, longer than interval %ds; "
                    "skipping inter-poll sleep",
                    self.connector_name, elapsed, interval_seconds,
                )
                wait = 0.0
            else:
                wait = interval_seconds - elapsed

            stop_event.wait(timeout=wait)

        logger.info("APIPollConsumer[%s] stopped.", self.connector_name)

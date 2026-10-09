"""OpenLineage RunEvents for model builds, without the OpenLineage SDK.

Each model build is an OpenLineage *run* of a *job* named after the model:
a ``START`` event before the build and a ``COMPLETE`` or ``FAIL`` after it.
``inputs`` are the model's upstreams, ``outputs`` the model itself with a
``schema`` facet, an ``outputStatistics`` facet and, from havn's own column
lineage (:func:`havn.engine.sql_analysis.extract_column_lineage`), a
``columnLineage`` facet. When the build belongs to a pipeline run, a
``parent`` run facet points at it, so Marquez groups the models of one run.

Events are plain JSON per the spec (https://openlineage.io/spec/2-0-2/
OpenLineage.json) sent with the standard library: an HTTP POST to
``<url>/<endpoint>`` (Marquez: ``/api/v1/lineage``), or one JSON line per
event appended to a file. Delivery happens on a background thread with a
bounded queue, so a lineage server that is down never slows or fails a
build; events that cannot be delivered are logged and dropped.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

logger = logging.getLogger("havn.telemetry")

SPEC_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_FACETS = "https://openlineage.io/spec/facets"
_SCHEMA_URLS = {
    "parent": f"{_FACETS}/1-1-0/ParentRunFacet.json#/$defs/ParentRunFacet",
    "errorMessage": f"{_FACETS}/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet",
    "processing_engine": f"{_FACETS}/1-1-1/ProcessingEngineRunFacet.json#/$defs/ProcessingEngineRunFacet",
    "sql": f"{_FACETS}/1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet",
    "jobType": f"{_FACETS}/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet",
    "schema": f"{_FACETS}/1-1-1/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet",
    "columnLineage": f"{_FACETS}/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet",
    "outputStatistics": f"{_FACETS}/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet",
}


def producer() -> str:
    from havn import __version__

    return f"https://github.com/chraltro/havn/tree/v{__version__}"


def _facet(name: str, body: dict) -> dict:
    return {"_producer": producer(), "_schemaURL": _SCHEMA_URLS[name], **body}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    return str(uuid.uuid4())


def dataset(namespace: str, name: str, facets: dict | None = None, output_facets: dict | None = None) -> dict:
    d: dict = {"namespace": namespace, "name": name}
    if facets:
        d["facets"] = facets
    if output_facets:
        d["outputFacets"] = output_facets
    return d


def schema_facet(columns: list[tuple[str, str]]) -> dict:
    return _facet("schema", {"fields": [{"name": n, "type": t} for n, t in columns]})


def column_lineage_facet(lineage: dict[str, list[dict]], namespace: str) -> dict | None:
    """``extract_column_lineage`` output as a ColumnLineageDatasetFacet."""
    fields: dict[str, dict] = {}
    for out_col, sources in lineage.items():
        inputs = []
        for src in sources:
            table = src.get("source_table")
            col = src.get("source_column")
            if not table or not col or col == "*" or src.get("resolved") is False:
                continue
            inputs.append({
                "namespace": namespace,
                "name": table,
                "field": col,
                "transformations": [{
                    "type": "DIRECT",
                    "subtype": "IDENTITY" if src.get("source_column") == out_col else "TRANSFORMATION",
                }],
            })
        if inputs:
            fields[out_col] = {"inputFields": inputs}
    if not fields:
        return None
    return _facet("columnLineage", {"fields": fields})


def run_event(
    event_type: str,
    *,
    run_id: str,
    job_namespace: str,
    job_name: str,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
    parent: dict | None = None,
    sql: str | None = None,
    error: str | None = None,
    engine_version: str | None = None,
) -> dict:
    """One OpenLineage RunEvent. ``parent`` is ``{"run_id", "namespace", "name"}``."""
    run_facets: dict = {}
    if parent and parent.get("run_id"):
        run_facets["parent"] = _facet("parent", {
            "run": {"runId": parent["run_id"]},
            "job": {"namespace": parent.get("namespace", job_namespace), "name": parent.get("name", "pipeline")},
        })
    if engine_version:
        from havn import __version__

        run_facets["processing_engine"] = _facet("processing_engine", {
            "version": engine_version, "name": "DuckDB", "openlineageAdapterVersion": __version__,
        })
    if error:
        run_facets["errorMessage"] = _facet("errorMessage", {
            "message": error[:4000], "programmingLanguage": "SQL",
        })
    job_facets: dict = {
        "jobType": _facet("jobType", {"processingType": "BATCH", "integration": "HAVN", "jobType": "MODEL"}),
    }
    if sql:
        job_facets["sql"] = _facet("sql", {"query": sql})
    event = {
        "eventType": event_type,
        "eventTime": _now(),
        "producer": producer(),
        "schemaURL": SPEC_URL,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {"namespace": job_namespace, "name": job_name, "facets": job_facets},
        "inputs": inputs or [],
        "outputs": outputs or [],
    }
    return event


class Emitter:
    """Delivers events for one transport on a daemon thread."""

    def __init__(self, cfg: Any, project_dir: Path | None) -> None:
        self.cfg = cfg
        self.project_dir = Path(project_dir) if project_dir else None
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._file_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="havn-openlineage")
        self._thread.start()

    def emit(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("OpenLineage queue is full; dropping a %s event", event.get("eventType"))

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued event was handled. True if the queue drained."""
        done = threading.Event()

        def _wait() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=_wait, daemon=True).start()
        return done.wait(timeout)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                self._send(event)
            except Exception as e:
                logger.warning("OpenLineage delivery failed: %s", e)
            finally:
                self._queue.task_done()

    def _send(self, event: dict) -> None:
        body = json.dumps(event, default=str)
        if self.cfg.transport == "file":
            path = Path(self.cfg.path)
            if not path.is_absolute() and self.project_dir is not None:
                path = self.project_dir / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock, open(path, "a", encoding="utf-8") as fh:
                fh.write(body + "\n")
            return
        url = self.cfg.url.rstrip("/") + "/" + self.cfg.endpoint.lstrip("/")
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        req = Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=float(self.cfg.timeout_s)) as resp:  # noqa: S310 - configured URL
            if resp.status >= 300:
                raise RuntimeError(f"{url} answered {resp.status}")


_emitters: dict[tuple, Emitter] = {}
_emitters_lock = threading.Lock()


def get_emitter(cfg: Any, project_dir: Path | None) -> Emitter | None:
    if cfg is None or not getattr(cfg, "enabled", False):
        return None
    key = (cfg.transport, cfg.url, cfg.endpoint, cfg.path, cfg.api_key, str(project_dir), cfg.timeout_s)
    with _emitters_lock:
        em = _emitters.get(key)
        if em is None:
            em = Emitter(cfg, project_dir)
            _emitters[key] = em
        return em


def flush_all(timeout: float = 5.0) -> None:
    with _emitters_lock:
        emitters = list(_emitters.values())
    for em in emitters:
        em.flush(timeout)

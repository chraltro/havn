"""Prometheus scrape endpoint.

Exposes ``/metrics`` in Prometheus text-exposition format. Distinct from the
legacy ``/api/metrics`` JSON endpoint (kept for the web UI).

Metrics live in :mod:`havn.engine.observability` and are updated from the
transform engine, query routes, streaming workers, and the resource
manager. See :mod:`havn.engine.observability` for the full catalogue.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from havn.engine.observability import render_prometheus

router = APIRouter()


@router.get("/metrics")
def prometheus_metrics() -> Response:
    """Scrape endpoint for Prometheus / VictoriaMetrics."""
    body = render_prometheus()
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/health")
def health_alias() -> dict:
    """Cheap liveness probe. No warehouse roundtrip — use ``/api/health`` for that."""
    return {"status": "ok"}

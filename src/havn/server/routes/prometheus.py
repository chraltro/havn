"""Prometheus scrape endpoint.

Exposes ``/metrics`` in Prometheus text-exposition format. Distinct from the
legacy ``/api/metrics`` JSON endpoint (kept for the web UI).

Metrics live in :mod:`havn.engine.observability` and are updated from the
transform engine, query routes, streaming workers, and the resource
manager. See :mod:`havn.engine.observability` for the full catalogue.
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, HTTPException, Request, Response

from havn.engine.observability import render_prometheus

router = APIRouter()


@router.get("/metrics")
def prometheus_metrics(request: Request) -> Response:
    """Scrape endpoint for Prometheus / VictoriaMetrics.

    Optional auth: when HAVN_METRICS_TOKEN is set the client must present
    a matching Bearer token. This prevents leaking query counts, model
    names and error rates if the server is exposed publicly.
    """
    expected = os.environ.get("HAVN_METRICS_TOKEN")
    if expected:
        auth = request.headers.get("authorization", "")
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth.split(None, 1)[1].strip()
        if not provided or not hmac.compare_digest(provided, expected):
            raise HTTPException(401, "Invalid or missing metrics token")
    body = render_prometheus()
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/health")
def health_alias() -> dict:
    """Cheap liveness probe. No warehouse roundtrip — use ``/api/health`` for that."""
    return {"status": "ok"}

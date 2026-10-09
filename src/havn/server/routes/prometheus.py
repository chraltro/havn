"""Prometheus scrape endpoint.

Exposes ``/metrics`` in Prometheus text-exposition format. Distinct from the
legacy ``/api/metrics`` JSON endpoint (kept for the web UI).

Two sources go into one response: the process registry in
:mod:`havn.engine.observability` (query latency histogram, transform
durations, active tasks, streaming counters), and series read from the
warehouse's own metadata at scrape time (per-model build duration, rows,
status, freshness age, failures, assertion failures, job status; see
:mod:`havn.engine.telemetry.prometheus`), plus the server's write-queue and
read-pool sizes.

Off unless ``telemetry.prometheus.enabled`` is true in project.yml, or the
``HAVN_METRICS_TOKEN`` environment variable is set (the switch this endpoint
had before it was configurable).
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request, Response

from havn.engine.observability import render_prometheus

logger = logging.getLogger("havn.server")

router = APIRouter()

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _prometheus_config():
    from havn.server.deps import _get_config

    try:
        return _get_config().telemetry.prometheus
    except Exception as e:
        logger.debug("No telemetry config, metrics use defaults: %s", e)
        from havn.config import PrometheusConfig

        return PrometheusConfig()


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(None, 1)[1].strip()
    return ""


def _authorize(request: Request, cfg, metrics_token: str | None) -> None:
    """Who may scrape.

    In order: anyone when ``allow_unauthenticated: true``; a scraper on the
    loopback interface when it is ``localhost``; whoever presents the metrics
    token; and, with havn auth on, any user token with read permission.
    With havn auth off and no metrics token configured the endpoint is as
    open as every other one.
    """
    if cfg.allow_unauthenticated == "true":
        return
    client = request.client.host if request.client else ""
    # Behind a reverse proxy on the same box every request arrives from
    # loopback, so a proxied request (one carrying forwarding headers) never
    # counts as local.
    proxied = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
    if cfg.allow_unauthenticated == "localhost" and client in _LOCAL_HOSTS and not proxied:
        return
    provided = _bearer(request)
    if metrics_token and provided and hmac.compare_digest(provided, metrics_token):
        return
    from havn.server.deps import _get_auth_enabled

    if _get_auth_enabled():
        from havn.server.deps import _require_permission

        _require_permission(request, "read")  # 401 / 403 on a bad user token
        return
    if metrics_token:
        raise HTTPException(401, "Invalid or missing metrics token")


def _server_gauges() -> dict:
    """Queue and pool sizes of this server, when they exist."""
    out: dict = {}
    try:
        import havn.server.deps as deps

        wq = getattr(deps, "_write_queue", None)
        if wq is not None:
            out["havn_write_queue_depth"] = ("Write operations waiting for the write connection.", wq._queue.qsize())
            out["havn_write_queue_capacity"] = ("Maximum depth of the write queue.", wq._queue.maxsize)
        pool = getattr(deps, "_read_pool", None)
        idle = getattr(pool, "_pool", None)
        if idle is not None and hasattr(idle, "qsize"):
            # Sampled while this scrape holds one, so a quiet server shows size - 1.
            out["havn_read_pool_idle"] = ("Read connections idle in the pool.", idle.qsize())
    except Exception as e:
        logger.debug("Server gauges unavailable: %s", e)
    try:
        from havn.engine.resource_manager import get_resource_manager

        snap = get_resource_manager().snapshot()
        out["havn_resource_tasks_active"] = ("Tasks the resource manager is running.", snap.get("total_active", 0))
    except Exception:
        pass
    return out


def _warehouse_block(cfg) -> bytes:
    from havn.server.deps import _get_backend, _get_read_pool

    try:
        if not _get_backend().exists():
            return b""
        from havn.engine.telemetry.prometheus import render_warehouse_metrics

        with _get_read_pool().connection() as cur:
            return render_warehouse_metrics(cur, include_models=cfg.include_models, extra=_server_gauges())
    except Exception as e:
        logger.debug("Warehouse metrics unavailable: %s", e)
        return b""


@router.get("/metrics")
def prometheus_metrics(request: Request) -> Response:
    """Scrape endpoint for Prometheus / VictoriaMetrics."""
    cfg = _prometheus_config()
    env_token = os.environ.get("HAVN_METRICS_TOKEN") or None
    if not cfg.enabled and not env_token:
        raise HTTPException(
            404,
            "Prometheus metrics are off. Set telemetry.prometheus.enabled: true in project.yml.",
        )
    _authorize(request, cfg, cfg.token or env_token)
    body = render_prometheus() + _warehouse_block(cfg)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/health")
def health_alias() -> dict:
    """Cheap liveness probe. No warehouse roundtrip — use ``/api/health`` for that."""
    return {"status": "ok"}

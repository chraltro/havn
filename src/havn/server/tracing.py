"""API request spans for OpenTelemetry (``telemetry.opentelemetry.trace_api``).

One SERVER span per request, named ``METHOD /route/template`` so
``/api/perf/models/gold.orders`` and ``/api/perf/models/silver.x`` group as
one operation. An incoming W3C ``traceparent`` header is honoured, so a
call from a traced client continues that client's trace.

The tracer is looked up per request from the cached project settings
(an ``os.stat`` of project.yml), which keeps the middleware free when
tracing is off.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("havn.server")


def request_tracer() -> Any:
    try:
        from havn.engine.instrumentation import project_settings
        from havn.engine.telemetry.otel import get_tracer
        from havn.server.deps import _get_project_dir

        cfg = project_settings(_get_project_dir()).telemetry.opentelemetry
        if not cfg.enabled or not cfg.trace_api:
            return None
        return get_tracer(cfg)
    except Exception as e:
        logger.debug("Request tracing unavailable: %s", e)
        return None


def _route_template(request: Any) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or request.url.path


async def traced_request(tracer: Any, request: Any, call_next: Any) -> Any:
    from havn.engine.telemetry.otel import end_span, start_span

    span = start_span(
        tracer,
        f"{request.method} {request.url.path}",
        carrier=dict(request.headers),
        attributes={
            "http.request.method": request.method,
            "url.path": request.url.path,
            "client.address": request.client.host if request.client else None,
        },
    )
    try:
        response = await call_next(request)
    except Exception as e:
        end_span(span, error=str(e) or type(e).__name__)
        raise
    route = _route_template(request)
    if span is not None:
        try:
            span.update_name(f"{request.method} {route}")
        except Exception:
            pass
    status = response.status_code
    end_span(
        span,
        error=f"HTTP {status}" if status >= 500 else None,
        attributes={"http.route": route, "http.response.status_code": status},
    )
    return response

"""OpenTelemetry traces: pipeline run -> step -> model spans, and API requests.

Optional twice over. ``telemetry.opentelemetry.enabled`` must be true, and
the ``opentelemetry`` packages must be importable (``pip install
'havn[otel]'``). When either is missing every function here returns None
and every caller treats None as "do nothing", so a project without the
packages pays one failed import, once.

havn builds its own ``TracerProvider`` instead of installing a global one:
an application embedding havn may already own the global provider, and
replacing it would reroute that application's spans. Spans are exported
over OTLP/HTTP (``opentelemetry-exporter-otlp-proto-http``) through a batch
processor, so a slow collector never slows a build.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("havn.telemetry")

_lock = threading.Lock()
_providers: dict[tuple, Any] = {}
_test_exporter: Any = None
_warned_missing = False


def otel_available() -> bool:
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        import opentelemetry.trace  # noqa: F401
    except Exception:
        return False
    return True


def use_test_exporter(exporter: Any) -> None:
    """Route every havn span to ``exporter`` synchronously (tests and debugging).

    Pass None to go back to the configured OTLP exporter.
    """
    global _test_exporter
    with _lock:
        _test_exporter = exporter
        for provider in _providers.values():
            try:
                provider.shutdown()
            except Exception:
                pass
        _providers.clear()


def _make_exporter(cfg: Any) -> Any:
    global _warned_missing
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except Exception:
        if not _warned_missing:
            _warned_missing = True
            logger.warning(
                "telemetry.opentelemetry is enabled but opentelemetry-exporter-otlp-proto-http "
                "is not installed; spans are not exported. pip install 'havn[otel]'"
            )
        return None
    return OTLPSpanExporter(endpoint=cfg.endpoint, headers=dict(cfg.headers or {}))


def get_tracer(cfg: Any) -> Any:
    """A tracer for ``cfg`` (an ``OtelConfig``), or None when tracing is off."""
    if cfg is None or not getattr(cfg, "enabled", False):
        return None
    if not otel_available():
        return None
    key = (cfg.endpoint, tuple(sorted((cfg.headers or {}).items())), cfg.service_name, id(_test_exporter))
    with _lock:
        provider = _providers.get(key)
        if provider is None:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

            from havn import __version__

            provider = TracerProvider(
                resource=Resource.create({
                    "service.name": cfg.service_name or "havn",
                    "service.version": __version__,
                })
            )
            if _test_exporter is not None:
                provider.add_span_processor(SimpleSpanProcessor(_test_exporter))
            else:
                exporter = _make_exporter(cfg)
                if exporter is None:
                    return None
                provider.add_span_processor(BatchSpanProcessor(exporter))
            _providers[key] = provider
    return provider.get_tracer("havn")


def _clean(attributes: dict | None) -> dict:
    """OTel attribute values must be str, bool, int, float or sequences of them."""
    out = {}
    for k, v in (attributes or {}).items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x) for x in v]
        else:
            out[k] = str(v)
    return out


def start_span(tracer: Any, name: str, parent: Any = None, attributes: dict | None = None, carrier: dict | None = None) -> Any:
    """Start a span under ``parent`` (a span) or a W3C ``carrier`` (request headers)."""
    if tracer is None:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind

        context = None
        kind = SpanKind.INTERNAL
        if parent is not None:
            context = trace.set_span_in_context(parent)
        elif carrier is not None:
            from opentelemetry.propagate import extract

            context = extract(carrier)
            kind = SpanKind.SERVER
        return tracer.start_span(name, context=context, kind=kind, attributes=_clean(attributes))
    except Exception as e:
        logger.debug("Could not start span %s: %s", name, e)
        return None


def end_span(span: Any, *, error: str | None = None, attributes: dict | None = None) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        if attributes:
            span.set_attributes(_clean(attributes))
        if error:
            span.set_status(Status(StatusCode.ERROR, error[:500]))
            span.add_event("exception", {"exception.message": error[:2000]})
        span.end()
    except Exception as e:
        logger.debug("Could not end span: %s", e)


def flush(timeout_ms: int = 2000) -> None:
    with _lock:
        providers = list(_providers.values())
    for provider in providers:
        try:
            provider.force_flush(timeout_ms)
        except Exception:
            pass

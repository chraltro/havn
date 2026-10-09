"""Telemetry exporters, all off until ``telemetry:`` in project.yml turns them on.

- :mod:`.prometheus` warehouse-derived series for ``GET /metrics``.
- :mod:`.otel` OpenTelemetry traces (needs the ``otel`` extra).
- :mod:`.openlineage` OpenLineage RunEvents over HTTP or to a file.

Model builds and runs reach these through :mod:`havn.engine.instrumentation`.
"""

from __future__ import annotations

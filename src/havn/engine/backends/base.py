"""Warehouse backend Protocol.

The WarehouseBackend abstraction lets havn run on either a plain DuckDB file
or a DuckLake catalog. All SQL, transforms, queries, notebooks, masking, and
diff logic run identically on both — the boundary is the connection layer
only. Once connect() returns a duckdb.DuckDBPyConnection, downstream code is
backend-agnostic.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable

import duckdb


class BackendStatus(TypedDict, total=False):
    backend: str                 # "duckdb" | "ducklake"
    healthy: bool
    # DuckDB-specific
    path: str
    size_bytes: int
    # DuckLake-specific
    catalog: str
    data_path: str
    snapshot_count: int
    encrypted: bool
    catalog_reachable: bool
    error: str


@runtime_checkable
class WarehouseBackend(Protocol):
    """Protocol every backend implementation satisfies."""

    name: str  # "duckdb" | "ducklake"

    def connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        """Open a DuckDB connection with the warehouse available for queries."""
        ...

    def status(self) -> BackendStatus:
        """Return backend health info for `havn status`."""
        ...

    def exists(self) -> bool:
        """True if the warehouse has been initialized."""
        ...

    def close(self) -> None:
        """Release any cached handles. No-op for DuckDB."""
        ...

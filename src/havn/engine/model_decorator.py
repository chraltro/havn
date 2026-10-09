"""The ``@model`` decorator for Python models (``from havn import model``).

Kept free of imports beyond the standard library: ``import havn`` loads it,
and so does every Python model file, so it must cost nothing.

The decorator does no work at run time beyond tagging the function. havn
reads the arguments statically, from the file's syntax tree, during
discovery (see ``havn.engine.transform.python_models``). That is why they
have to be literals: the DAG, ``havn ls`` and change detection all know a
Python model's config without ever running the file.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def model(fn: F | None = None, /, **config: Any) -> Any:
    """Mark the function that builds a Python model.

    Usable bare or with keyword arguments::

        from havn import model

        @model(materialized="incremental", unique_key="id", tags=["daily"])
        def build(db, ref, is_incremental, this):
            orders = ref("silver.orders")
            if is_incremental:
                orders = orders.filter(f"updated_at > (SELECT max(updated_at) FROM {this})")
            return orders

    The function returns a DuckDB relation, a pandas or polars DataFrame, or
    a pyarrow Table; havn writes it with the same materializations SQL
    models use. Keyword arguments are the same keys ``@config`` takes, plus
    ``description``, ``columns``, ``assertions``, ``grain``, ``owner``,
    ``depends_on``, ``timeout`` and ``idle_timeout``.
    """

    def mark(func: F) -> F:
        func.__havn_model__ = dict(config)  # type: ignore[attr-defined]
        return func

    if fn is not None:
        if not callable(fn) or config:
            raise TypeError("@model takes keyword arguments only, e.g. @model(materialized='table')")
        return mark(fn)
    return mark

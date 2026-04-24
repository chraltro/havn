"""Placeholder for the vendored pypgoutput package.

The upstream project (https://github.com/dgea005/pypgoutput) ships a
Postgres logical-replication decoder for Python. It's the best option
that exists but the upstream is unmaintained, so we plan to vendor it.

Until the vendor import is complete, importing this package raises
ImportError. :mod:`havn.engine.streaming.cdc_logical` converts that into a
friendly :class:`LogicalCDCUnavailable` message and keeps the rest of the
streaming stack running.

To finish the vendor:

1. ``git clone https://github.com/dgea005/pypgoutput tmp/``
2. Copy ``tmp/src/pypgoutput/`` into this directory (preserving ``LICENSE``).
3. Add ``psycopg[binary]>=3.2`` to the ``cdc`` optional extra in pyproject.toml.
4. Delete this placeholder module.
"""

raise ImportError(
    "pypgoutput is not vendored yet; install 'psycopg[binary]' and drop the "
    "upstream source into src/havn/vendor/pypgoutput/."
)

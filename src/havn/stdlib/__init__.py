"""havn standard library — built-in macros auto-registered for every project.

These ship with havn and are registered before user macros, so a fresh
``havn init`` can call them in SQL out of the box. User-defined macros
in ``project_dir/macros/`` with the same name override the stdlib (a
warning is logged on override).

Currently exposes ``havn.stdlib.pii`` for PII masking helpers. New
modules dropped into this package are auto-discovered via
``pkgutil.iter_modules``.
"""

from __future__ import annotations

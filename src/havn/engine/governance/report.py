"""What governance has to say about a project: for validate, check and ``havn pii``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from havn.textio import read_project_text

from .catalog import Key, get_snapshot

_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def exported_relations(project_dir: Path | None, config: Any, known: set[Key]) -> dict[Key, str]:
    """Relations that leave the warehouse: exposures and what export scripts read."""
    out: dict[Key, str] = {}
    for exposure in getattr(config, "exposures", []) or []:
        for dep in exposure.depends_on:
            schema, _, name = dep.lower().partition(".")
            if name:
                out[(schema, name)] = f"exposure {exposure.name}"
    if project_dir is not None:
        export_dir = Path(project_dir) / "export"
        if export_dir.is_dir():
            for path in sorted(list(export_dir.glob("*.py")) + list(export_dir.glob("*.dpnb"))):
                try:
                    text = read_project_text(path)
                except Exception:
                    continue
                for schema, name in _REF_RE.findall(text):
                    key = (schema.lower(), name.lower())
                    if key in known:
                        out.setdefault(key, f"export/{path.name}")
    return out


def governance_warnings(
    conn: duckdb.DuckDBPyConnection,
    project_dir: Path | None,
    config: Any = None,
    models: list | None = None,
) -> list:
    """Validation warnings about classified data and inherited row policies.

    * a column that carries PII (classified directly, by a masking policy, or
      inherited through lineage) in a ``governance.pii_schemas`` schema, an
      exposure or a table an export script reads, with no masking policy;
    * a model whose readers see no rows because it reads a row-protected
      table without passing the policy's columns through unchanged;
    * a model or view whose lineage could not be traced although it reads
      masked columns (governed readers are refused).
    """
    from havn.engine.transform.models import ValidationError

    try:
        snapshot = get_snapshot(conn, project_dir)
    except Exception as e:  # never break validate over governance
        return [ValidationError(model="(governance)", severity="warning",
                                message=f"Governance could not be evaluated: {e}")]
    schemas = {"gold"}
    if config is not None and getattr(config, "governance", None) is not None:
        schemas = {s.lower() for s in config.governance.pii_schemas}
    known = set(snapshot.catalog.tables) | set(snapshot.catalog.views)
    exported = exported_relations(project_dir, config, known)
    model_names = {m.full_name for m in models or []}

    out: list = []
    for key, columns in sorted(snapshot.classifications.items()):
        reason = exported.get(key) or (f"schema {key[0]}" if key[0] in schemas else None)
        if reason is None:
            continue
        for col, info in sorted(columns.items()):
            if info.get("masked"):
                continue
            origin = ", ".join(info.get("from") or []) or "a @pii tag"
            out.append(ValidationError(
                model=f"{key[0]}.{key[1]}",
                severity="warning",
                message=(
                    f"Column {col} carries PII (from {origin}) into {reason} with no masking "
                    "policy. Add a masking policy on the source column, or @declassify "
                    f"{col}: <why> in the model if it no longer identifies anyone."
                ),
            ))
    for p in snapshot.row_policies:
        if not p.get("deny") or not p.get("inherited_from"):
            continue
        rel = f"{p['schema_name']}.{p['table_name']}"
        if model_names and rel not in model_names:
            continue
        out.append(ValidationError(
            model=rel,
            severity="warning",
            message=(
                f"Readers subject to the row policy on {p['inherited_from']} see no rows of "
                f"{rel}: {p.get('deny_reason') or 'the policy columns are not passed through'}. "
                "Pass those columns through unchanged, or add @declassify rows: <why>."
            ),
        ))
    for (schema, name), reason in sorted(snapshot.opaque.items()):
        out.append(ValidationError(
            model=f"{schema}.{name}",
            severity="warning",
            message=f"Governed readers are refused: {reason}.",
        ))
    return out


def pii_report(conn: duckdb.DuckDBPyConnection, project_dir: Path | None) -> dict:
    """Everything ``havn pii`` and the UI show: classifications, policies, notes."""
    from .catalog import describe

    snapshot = get_snapshot(conn, project_dir)
    report = describe(snapshot)
    report["declassified"] = [
        {"relation": f"{s}.{t}", "column": c, "reason": r}
        for (s, t), cols in sorted(snapshot.declassified.items())
        for c, r in sorted(cols.items())
    ]
    return report

"""Run a verification and record its outcome on the change set."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from havn.engine.changesets.store import ChangeSet, get_change_set, set_status

logger = logging.getLogger("havn.changesets")


def mask_report(report: dict, role: str, conn: Any) -> dict:
    """Apply masking policies to the warehouse rows a report carries.

    Data diff samples are rows of real (or scratch-built) models, so they get
    the same masking a query by ``role`` would.
    """
    if conn is None or not report:
        return report
    from havn.engine.masking import apply_masking

    for d in report.get("diffs", []):
        schema, _, table = str(d.get("model", "")).partition(".")
        for key in ("sample_added", "sample_removed", "sample_modified"):
            rows = d.get(key) or []
            if not rows:
                continue
            columns = list(rows[0].keys())
            values = [[r.get(c) for c in columns] for r in rows]
            try:
                masked = apply_masking(columns, values, role, conn, schema=schema, table=table)
            except Exception as e:
                logger.debug("Masking diff samples failed: %s", e)
                d[key] = []
                continue
            d[key] = [dict(zip(columns, row)) for row in masked]
    return report


def verify_and_store(
    project_dir: Path,
    cs_id: str,
    *,
    conn: Any,
    config: Any = None,
    timeout: float | None = None,
    role: str = "admin",
) -> ChangeSet:
    """Verify change set ``cs_id`` on ``conn`` and save the report on it.

    ``role`` is the requesting user's: data diff samples are masked for it.
    """
    from havn.engine.changesets.verify import DEFAULT_TIMEOUT, verify_change_set

    cs = set_status(project_dir, cs_id, "verifying")
    if cs.status != "verifying":
        return cs  # applied or discarded meanwhile
    try:
        report = verify_change_set(
            project_dir, cs, conn=conn, config=config,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
    except Exception as e:  # never leave a change set stuck in "verifying"
        logger.exception("Verification of change set %s crashed", cs_id)
        report = {
            "ok": False,
            "status": "failed",
            "error": f"verification crashed: {e}",
            "checks": [{"name": "verify", "status": "fail", "summary": str(e), "details": []}],
        }
    report = mask_report(report, role, conn)
    # A revision submitted while this one ran must not get this report.
    current = get_change_set(project_dir, cs_id)
    if current.revision != cs.revision:
        return current
    return set_status(project_dir, cs_id, "ready" if report.get("ok") else "failed", report)


def verify_locally(project_dir: Path, cs_id: str, *, env: str | None = None) -> ChangeSet:
    """Verify without a server: open the warehouse read-only in this process."""
    from havn.config import load_project
    from havn.engine.backends import create_backend
    from havn.engine.database import open_warehouse

    project_dir = Path(project_dir)
    config = load_project(project_dir, env=env)
    conn = None
    backend = create_backend(config.database, project_dir=project_dir)
    if backend.exists():
        try:
            conn = open_warehouse(config, project_dir, read_only=True)
        except Exception as e:
            logger.warning("Could not open the warehouse for verification: %s", e)
    try:
        return verify_and_store(project_dir, cs_id, conn=conn, config=config)
    finally:
        if conn is not None:
            conn.close()


def report_text(cs: ChangeSet) -> str:
    """A short plain-text rendering of a change set's report (CLI, MCP)."""
    lines = [f"change set {cs.id} (revision {cs.revision}): {cs.status}"]
    for f in cs.files:
        lines.append(f"  {f.action:<6} {f.path}")
    for p in cs.ignored:
        lines.append(f"  ignored {p} (outside what a change set may touch)")
    report = cs.report or {}
    for check in report.get("checks", []):
        lines.append(f"  [{check['status']:>4}] {check['name']}: {check['summary']}")
    for d in report.get("diffs", []):
        if d.get("removed_model"):
            lines.append(f"    diff {d['model']}: model removed")
        elif d.get("error"):
            lines.append(f"    diff {d['model']}: error {d['error']}")
        else:
            lines.append(
                f"    diff {d['model']}: {d['total_before']} -> {d['total_after']} rows "
                f"(+{d['added']} -{d['removed']} ~{d['modified']})"
            )
    return "\n".join(lines)

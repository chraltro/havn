"""Deploy a commit to an environment, and roll it back if any model fails.

A deploy builds the code at a git ref (usually the base branch after a merge)
against one environment's warehouse:

1. Check the ref out into a throwaway worktree, so the code that runs is the
   commit being deployed, not whatever the working tree has checked out.
2. Plan: ``state:modified+`` against the target warehouse, i.e. every model
   whose SQL or upstream differs from what that warehouse last built, plus
   everything downstream of those.
3. Snapshot exactly those models: table data (Parquet, via ``create_version``),
   view definitions, which objects did not exist yet, and their
   ``_havn.model_state`` / ``_havn.model_columns`` rows.
4. Build them with the ref's code and macros.
5. If any planned model did not build (an error, a failed error-level check,
   a blocked upstream, a stale source), put every planned model back as it
   was. Build state is restored too, so the next deploy plans the same
   models again instead of believing they shipped.

Records go to ``_havn.deploys`` in the warehouse the server runs against, so
the history is readable whichever environment was deployed to.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger("havn.deploy")

DEPLOY_ROOT = (".havn", "deploy")
# Statuses run_transform reports for a model that is in place afterwards.
_OK_STATUSES = {"built", "skipped", "inlined"}

_deploy_lock = threading.Lock()


class DeployError(RuntimeError):
    pass


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def ensure_deploys_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.deploys (
            id           VARCHAR,
            env          VARCHAR,
            ref          VARCHAR,
            commit_sha   VARCHAR,
            pr_id        VARCHAR,
            deployed_by  VARCHAR,
            status       VARCHAR,
            started_at   VARCHAR,
            finished_at  VARCHAR,
            duration_ms  BIGINT,
            detail       JSON,
            error        VARCHAR
        )
    """)


_RECORD_KEYS = ("models", "results", "failed", "version_id", "restored")


def _save(conn: duckdb.DuckDBPyConnection, rec: dict) -> None:
    """Insert or update in one statement, so a reader polling the record while
    a deploy runs never catches it missing (a delete-then-insert would)."""
    ensure_deploys_table(conn)
    values = [
        rec["env"], rec["ref"], rec.get("commit"), rec.get("pr_id"),
        rec.get("deployed_by"), rec["status"], rec["started_at"], rec.get("finished_at"),
        rec.get("duration_ms"), json.dumps({k: rec.get(k) for k in _RECORD_KEYS}), rec.get("error"),
    ]
    exists = conn.execute("SELECT 1 FROM _havn.deploys WHERE id = ?", [rec["id"]]).fetchone()
    if exists:
        conn.execute(
            "UPDATE _havn.deploys SET env = ?, ref = ?, commit_sha = ?, pr_id = ?, deployed_by = ?, "
            "status = ?, started_at = ?, finished_at = ?, duration_ms = ?, detail = ?, error = ? "
            "WHERE id = ?",
            [*values, rec["id"]],
        )
    else:
        conn.execute("INSERT INTO _havn.deploys VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [rec["id"], *values])


def list_deploys(
    conn: duckdb.DuckDBPyConnection, *, limit: int = 20, pr_id: str | None = None,
    deploy_id: str | None = None,
) -> list[dict]:
    try:
        where, params = [], []
        if pr_id:
            where.append("pr_id = ?")
            params.append(pr_id)
        if deploy_id:
            where.append("id = ?")
            params.append(deploy_id)
        sql = (
            "SELECT id, env, ref, commit_sha, pr_id, deployed_by, status, started_at, "
            "finished_at, duration_ms, detail, error FROM _havn.deploys"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + " ORDER BY started_at DESC LIMIT ?"
        )
        rows = conn.execute(sql, [*params, limit]).fetchall()
    except duckdb.Error:
        return []  # no deploys yet (or a read-only connection before the first)
    out = []
    for r in rows:
        detail = json.loads(r[10]) if isinstance(r[10], str) else (r[10] or {})
        out.append({
            "id": r[0], "env": r[1], "ref": r[2], "commit": r[3], "pr_id": r[4],
            "deployed_by": r[5], "status": r[6], "started_at": r[7], "finished_at": r[8],
            "duration_ms": r[9], "error": r[11], **{k: detail.get(k) for k in _RECORD_KEYS},
        })
    return out


# ---------------------------------------------------------------------------
# Worktree
# ---------------------------------------------------------------------------


class _Checkout:
    """The ref checked out into ``.havn/deploy/<id>``, removed on exit."""

    def __init__(self, project_dir: Path, ref: str, name: str):
        self.project_dir = project_dir
        self.ref = ref
        self.path = project_dir.joinpath(*DEPLOY_ROOT, name)
        self.sha: str | None = None

    def __enter__(self) -> _Checkout:
        from havn.engine.git import _run_git, _validate_branch_name, is_git_repo

        if not is_git_repo(self.project_dir):
            raise DeployError("Deploying needs the project to be a git repository")
        if not _validate_branch_name(self.ref):
            raise DeployError(f"Invalid ref: {self.ref!r}")
        res = _run_git(self.project_dir, "rev-parse", "--verify", f"{self.ref}^{{commit}}")
        if res.returncode != 0:
            raise DeployError(f"Unknown ref {self.ref!r}: {res.stderr.strip()}")
        self.sha = res.stdout.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        add = _run_git(
            self.project_dir, "worktree", "add", "--detach", "--force",
            str(self.path), self.sha, timeout=60,
        )
        if add.returncode != 0:
            raise DeployError(f"git worktree add failed: {add.stderr.strip() or add.stdout.strip()}")
        return self

    def __exit__(self, *exc) -> None:
        from havn.engine.pr import _worktree_cleanup

        _worktree_cleanup(self.project_dir, self.path)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _plan(conn: duckdb.DuckDBPyConnection | None, checkout_dir: Path) -> list[str]:
    """Models at the checked-out ref that differ from the target warehouse, plus downstream.

    ``conn`` None means the target warehouse does not exist yet: everything.
    """
    from havn.engine.database import ensure_meta_table
    from havn.engine.selectors import select_models
    from havn.engine.transform.discovery import build_dag, discover_all_models

    models = discover_all_models(checkout_dir)
    ordered = [m.full_name for m in build_dag(models)]
    if conn is None:
        return ordered
    try:
        ensure_meta_table(conn)
    except duckdb.Error:
        pass  # read-only plan against a warehouse that has never been built
    chosen = set(
        select_models(["state:modified+"], models, conn=conn, project_dir=checkout_dir).selected
    )
    return [name for name in ordered if name in chosen]


def plan_deploy(
    project_dir: Path, ref: str, conn: duckdb.DuckDBPyConnection | None,
) -> dict:
    """What deploying ``ref`` to the warehouse behind ``conn`` would rebuild."""
    with _Checkout(project_dir, ref, f"plan-{uuid.uuid4().hex[:8]}") as co:
        return {"ref": ref, "commit": co.sha, "models": _plan(conn, co.path)}


# ---------------------------------------------------------------------------
# Snapshot / restore
# ---------------------------------------------------------------------------


def _object_kind(conn, schema: str, name: str) -> str | None:
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return row[0] if row else None


def _meta_rows(conn, table: str, names: list[str]) -> tuple[list[str], list[tuple]]:
    try:
        cur = conn.execute(f"SELECT * FROM _havn.{table} WHERE model_path IN (SELECT unnest(?))", [names])
        return [d[0] for d in cur.description], cur.fetchall()
    except duckdb.Error:
        return [], []


def _snapshot(conn, project_dir: Path, names: list[str], label: str) -> dict:
    from havn.engine.versioning import create_version, get_version

    objects: dict[str, dict] = {}
    base_tables: list[str] = []
    for full in names:
        schema, name = full.split(".", 1)
        kind = _object_kind(conn, schema, name)
        entry: dict[str, Any] = {"kind": kind}
        if kind == "VIEW":
            row = conn.execute(
                "SELECT sql FROM duckdb_views() "
                "WHERE database_name = current_database() AND schema_name = ? AND view_name = ?",
                [schema, name],
            ).fetchone()
            entry["sql"] = row[0] if row else None
        elif kind == "BASE TABLE":
            base_tables.append(full)
        objects[full] = entry

    version_id, tables_info = None, {}
    if base_tables:
        created = create_version(
            conn, project_dir, description=f"Pre-deploy snapshot: {label}",
            tables=base_tables, trigger="deploy",
        )
        version_id = created.get("version_id")
        # create_version returns table names; the per-table result (Parquet
        # path or error) is in the stored manifest.
        stored = get_version(conn, version_id) if version_id and not created.get("error") else None
        tables_info = (stored or {}).get("tables") or {}
        broken = [t for t in base_tables if not (tables_info.get(t) or {}).get("parquet_file")]
        if created.get("error") or broken:
            raise DeployError(
                "Could not snapshot " + (", ".join(broken) or "the warehouse")
                + f" before deploying ({created.get('error') or 'see the server log'}). Nothing was changed."
            )
    return {
        "objects": objects,
        "version_id": version_id,
        "parquet": {t: info.get("parquet_file") for t, info in tables_info.items()},
        "model_state": _meta_rows(conn, "model_state", names),
        "model_columns": _meta_rows(conn, "model_columns", names),
    }


def _restore(conn, project_dir: Path, snap: dict) -> list[str]:
    restored = []
    for full, before in snap["objects"].items():
        schema, name = full.split(".", 1)
        q = f'"{schema}"."{name}"'
        now = _object_kind(conn, schema, name)
        if now == "VIEW":
            conn.execute(f"DROP VIEW IF EXISTS {q}")
        elif now is not None:
            conn.execute(f"DROP TABLE IF EXISTS {q}")
        if before["kind"] == "BASE TABLE":
            path = str((project_dir / snap["parquet"][full]).resolve()).replace("'", "''")
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            conn.execute(f"CREATE TABLE {q} AS SELECT * FROM read_parquet('{path}')")
        elif before["kind"] == "VIEW" and before.get("sql"):
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            conn.execute(before["sql"])
        restored.append(full)

    names = list(snap["objects"])
    for table in ("model_state", "model_columns"):
        cols, rows = snap[table]
        try:
            conn.execute(f"DELETE FROM _havn.{table} WHERE model_path IN (SELECT unnest(?))", [names])
        except duckdb.Error:
            continue
        if cols and rows:
            placeholders = ", ".join("?" for _ in cols)
            conn.executemany(
                f"INSERT INTO _havn.{table} ({', '.join(cols)}) VALUES ({placeholders})",
                [list(r) for r in rows],
            )
    return restored


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------


def new_record(env: str, ref: str, *, pr_id: str | None = None, deployed_by: str | None = None) -> dict:
    return {
        "id": f"deploy-{uuid.uuid4().hex[:8]}", "env": env, "ref": ref, "commit": None,
        "pr_id": pr_id, "deployed_by": deployed_by, "status": "running", "started_at": _now(),
        "finished_at": None, "duration_ms": None, "models": None, "results": None,
        "failed": None, "version_id": None, "restored": None, "error": None,
    }


def run_deploy(
    project_dir: Path,
    record: dict,
    conn: duckdb.DuckDBPyConnection,
    *,
    record_conn: duckdb.DuckDBPyConnection | None = None,
    db_path: str | None = None,
    restore_macros_from: Path | None = None,
) -> dict:
    """Deploy ``record["ref"]`` to the warehouse behind ``conn``; returns the record.

    ``record_conn`` is where the record is kept (default: ``conn``).
    ``restore_macros_from`` re-registers that directory's macros on ``conn``
    afterwards, for a long-lived connection (the server's) that must go back
    to serving the working tree's macros.
    """
    from havn.engine.database import ensure_meta_table
    from havn.engine.macros import register_macros
    from havn.engine.transform import run_transform

    record_conn = record_conn or conn
    start = time.perf_counter()
    if not _deploy_lock.acquire(blocking=False):
        record.update(status="error", error="Another deploy is running", finished_at=_now())
        _save(record_conn, record)
        return record
    snap = None
    try:
        _save(record_conn, record)
        with _Checkout(project_dir, record["ref"], record["id"]) as co:
            record["commit"] = co.sha
            ensure_meta_table(conn)
            try:
                register_macros(conn, co.path, force_reload=True)
            except Exception as e:
                logger.warning("deploy %s: macro registration failed: %s", record["id"], e)

            planned = _plan(conn, co.path)
            record["models"] = planned
            if not planned:
                record["status"] = "up_to_date"
                return record

            label = f"{record['ref']} {co.sha[:7]} to {record['env']}"
            snap = _snapshot(conn, project_dir, planned, label)
            record["version_id"] = snap["version_id"]

            results = run_transform(
                conn, co.path / "transform", targets=planned,
                db_path=db_path, project_dir=co.path, pipeline_run_id=record["id"],
            ) or {}
            record["results"] = {n: results.get(n) for n in planned}
            not_built = {n: s for n, s in record["results"].items() if s not in _OK_STATUSES}
            if not not_built:
                record["status"] = "success"
                return record

            errors = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT target, error FROM _havn.run_log "
                    "WHERE pipeline_run_id = ? AND error IS NOT NULL",
                    [record["id"]],
                ).fetchall()
            }
            record["failed"] = {
                n: {"status": s, "error": (errors.get(n) or "").splitlines()[0][:500] if errors.get(n) else None}
                for n, s in not_built.items()
            }
            record["restored"] = _restore(conn, project_dir, snap)
            record["status"] = "rolled_back"
            return record
    except Exception as e:
        logger.warning("deploy %s failed: %s", record["id"], e)
        record["error"] = str(e)
        record["status"] = "error"
        if snap is not None:
            try:
                record["restored"] = _restore(conn, project_dir, snap)
                record["status"] = "rolled_back"
            except Exception as re:
                record["error"] += f" | rollback failed: {re}"
        return record
    finally:
        if restore_macros_from is not None:
            try:
                register_macros(conn, restore_macros_from, force_reload=True)
            except Exception as e:
                logger.warning("deploy %s: restoring macros failed: %s", record["id"], e)
        record["finished_at"] = _now()
        record["duration_ms"] = int((time.perf_counter() - start) * 1000)
        try:
            _save(record_conn, record)
        except Exception as e:
            logger.error("deploy %s: could not save record: %s", record["id"], e)
        _deploy_lock.release()

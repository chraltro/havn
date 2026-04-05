"""Pull request engine: PR lifecycle, build-in-worktree diff, review prompt.

PRs are stored as JSON files under ``.havn/prs/`` so their state travels with
the git repository. Build results and data diffs live in ``_havn.pr_builds``
because they're local to each developer's warehouse. All AI review runs
through the developer's own agent adapter via the web UI; no server-side keys.

See ``docs/internal/to-do.md`` for the cloud (hosted) version roadmap.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from havn.engine.git import (
    _run_git,
    _validate_branch_name,
    current_branch,
    diff_files_between,
    git_checkout_branch,
    is_dirty,
    is_git_repo,
    last_commit_hash,
)

logger = logging.getLogger("havn.pr")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PRComment:
    id: str
    author: str
    body: str
    created_at: str
    comment_type: str = "human"  # "human" or "ai_review"
    file: str | None = None      # optional file anchor
    line: int | None = None      # optional line anchor

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "author": self.author,
            "body": self.body,
            "created_at": self.created_at,
            "comment_type": self.comment_type,
            "file": self.file,
            "line": self.line,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PRComment":
        return cls(
            id=data["id"],
            author=data.get("author", ""),
            body=data.get("body", ""),
            created_at=data.get("created_at", ""),
            comment_type=data.get("comment_type", "human"),
            file=data.get("file"),
            line=data.get("line"),
        )


@dataclass
class PullRequest:
    id: str
    title: str
    description: str
    base_ref: str                    # usually "main"
    head_ref: str                    # the PR branch
    author: str
    status: str = "open"             # open / merged / closed
    created_at: str = ""
    updated_at: str = ""
    comments: list[PRComment] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)
    change_requesters: list[str] = field(default_factory=list)
    require_approval: bool = True
    merged_by: str | None = None
    merged_at: str | None = None
    closed_by: str | None = None
    closed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "author": self.author,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "comments": [c.to_dict() for c in self.comments],
            "approvers": self.approvers,
            "change_requesters": self.change_requesters,
            "require_approval": self.require_approval,
            "merged_by": self.merged_by,
            "merged_at": self.merged_at,
            "closed_by": self.closed_by,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PullRequest":
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            base_ref=data.get("base_ref", "main"),
            head_ref=data.get("head_ref", ""),
            author=data.get("author", ""),
            status=data.get("status", "open"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            comments=[PRComment.from_dict(c) for c in data.get("comments", [])],
            approvers=data.get("approvers", []),
            change_requesters=data.get("change_requesters", []),
            require_approval=data.get("require_approval", True),
            merged_by=data.get("merged_by"),
            merged_at=data.get("merged_at"),
            closed_by=data.get("closed_by"),
            closed_at=data.get("closed_at"),
        )


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


_VALID_PR_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _validate_pr_id(pr_id: str) -> None:
    if not pr_id or not _VALID_PR_ID.match(pr_id):
        raise ValueError(f"Invalid PR id: {pr_id!r}")


def _pr_dir(project_dir: Path) -> Path:
    return project_dir / ".havn" / "prs"


def _pr_path(project_dir: Path, pr_id: str) -> Path:
    _validate_pr_id(pr_id)
    return _pr_dir(project_dir) / f"{pr_id}.json"


def _now_iso() -> str:
    """UTC ISO 8601 timestamp. Used for all PR timestamps so lexicographic
    sorting agrees with chronological ordering regardless of local timezone."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _save_pr(project_dir: Path, pr: PullRequest) -> None:
    _pr_dir(project_dir).mkdir(parents=True, exist_ok=True)
    pr.updated_at = _now_iso()
    _pr_path(project_dir, pr.id).write_text(json.dumps(pr.to_dict(), indent=2))


def _load_pr(project_dir: Path, pr_id: str) -> PullRequest | None:
    path = _pr_path(project_dir, pr_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return PullRequest.from_dict(data)
    except Exception as e:
        logger.warning("Failed to load PR %s: %s", pr_id, e)
        return None


# ---------------------------------------------------------------------------
# Metadata table
# ---------------------------------------------------------------------------


def ensure_pr_builds_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create _havn.pr_builds if missing. Safe to call repeatedly."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS _havn")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _havn.pr_builds (
            id              VARCHAR PRIMARY KEY,
            pr_id           VARCHAR NOT NULL,
            branch_head     VARCHAR,
            status          VARCHAR NOT NULL DEFAULT 'running',
            started_at      TIMESTAMP DEFAULT current_timestamp,
            finished_at     TIMESTAMP,
            duration_ms     BIGINT,
            data_diff       JSON,
            lineage_impact  JSON,
            contract_results JSON,
            error           VARCHAR
        )
    """)


def _save_build_record(conn: duckdb.DuckDBPyConnection, record: dict) -> None:
    ensure_pr_builds_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO _havn.pr_builds "
        "(id, pr_id, branch_head, status, started_at, finished_at, duration_ms, "
        " data_diff, lineage_impact, contract_results, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            record["id"],
            record["pr_id"],
            record.get("branch_head"),
            record.get("status", "running"),
            record.get("started_at"),
            record.get("finished_at"),
            record.get("duration_ms"),
            json.dumps(record["data_diff"]) if record.get("data_diff") is not None else None,
            json.dumps(record["lineage_impact"]) if record.get("lineage_impact") is not None else None,
            json.dumps(record["contract_results"]) if record.get("contract_results") is not None else None,
            record.get("error"),
        ],
    )


def _fetch_build_record(
    conn: duckdb.DuckDBPyConnection, pr_id: str
) -> dict | None:
    ensure_pr_builds_table(conn)
    row = conn.execute(
        "SELECT id, pr_id, branch_head, status, started_at, finished_at, "
        "duration_ms, data_diff, lineage_impact, contract_results, error "
        "FROM _havn.pr_builds WHERE pr_id = ? ORDER BY started_at DESC LIMIT 1",
        [pr_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "pr_id": row[1],
        "branch_head": row[2],
        "status": row[3],
        "started_at": str(row[4]) if row[4] else None,
        "finished_at": str(row[5]) if row[5] else None,
        "duration_ms": row[6],
        "data_diff": json.loads(row[7]) if isinstance(row[7], str) else row[7],
        "lineage_impact": json.loads(row[8]) if isinstance(row[8], str) else row[8],
        "contract_results": json.loads(row[9]) if isinstance(row[9], str) else row[9],
        "error": row[10],
    }


# ---------------------------------------------------------------------------
# CRUD lifecycle
# ---------------------------------------------------------------------------


def create_pr(
    project_dir: Path,
    title: str,
    description: str,
    base_ref: str,
    head_ref: str,
    author: str,
    require_approval: bool = True,
) -> PullRequest:
    """Create a new PR and persist it under .havn/prs/."""
    if not title or not title.strip():
        raise ValueError("PR title required")
    if not _validate_branch_name(base_ref):
        raise ValueError(f"Invalid base_ref: {base_ref}")
    if not _validate_branch_name(head_ref):
        raise ValueError(f"Invalid head_ref: {head_ref}")
    if base_ref == head_ref:
        raise ValueError("base_ref and head_ref must differ")

    now = _now_iso()
    pr_id = f"pr-{uuid.uuid4().hex[:8]}"
    pr = PullRequest(
        id=pr_id,
        title=title.strip(),
        description=description or "",
        base_ref=base_ref,
        head_ref=head_ref,
        author=author or "unknown",
        status="open",
        created_at=now,
        updated_at=now,
        require_approval=require_approval,
    )
    _save_pr(project_dir, pr)
    logger.info("Created PR %s (%s -> %s)", pr_id, head_ref, base_ref)
    return pr


def get_pr(project_dir: Path, pr_id: str) -> PullRequest | None:
    return _load_pr(project_dir, pr_id)


def list_prs(project_dir: Path, status: str | None = None) -> list[PullRequest]:
    """List PRs optionally filtered by status."""
    pr_dir = _pr_dir(project_dir)
    if not pr_dir.exists():
        return []
    results: list[PullRequest] = []
    for json_file in sorted(pr_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text())
            pr = PullRequest.from_dict(data)
            if status is None or pr.status == status:
                results.append(pr)
        except Exception as e:
            logger.debug("Skipping malformed PR file %s: %s", json_file.name, e)
    # newest first
    results.sort(key=lambda p: p.created_at, reverse=True)
    return results


def update_pr(
    project_dir: Path,
    pr_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    require_approval: bool | None = None,
) -> PullRequest:
    pr = _load_pr(project_dir, pr_id)
    if pr is None:
        raise ValueError(f"PR '{pr_id}' not found")
    if pr.status != "open":
        raise ValueError(f"Cannot update {pr.status} PR")
    if title is not None:
        pr.title = title.strip() or pr.title
    if description is not None:
        pr.description = description
    if require_approval is not None:
        pr.require_approval = require_approval
    _save_pr(project_dir, pr)
    return pr


def close_pr(project_dir: Path, pr_id: str, user: str) -> PullRequest:
    pr = _load_pr(project_dir, pr_id)
    if pr is None:
        raise ValueError(f"PR '{pr_id}' not found")
    if pr.status == "merged":
        raise ValueError("Cannot close a merged PR")
    pr.status = "closed"
    pr.closed_by = user
    pr.closed_at = _now_iso()
    _save_pr(project_dir, pr)
    return pr


# ---------------------------------------------------------------------------
# Comments / review
# ---------------------------------------------------------------------------


def add_comment(
    project_dir: Path,
    pr_id: str,
    author: str,
    body: str,
    comment_type: str = "human",
    file: str | None = None,
    line: int | None = None,
) -> PRComment:
    pr = _load_pr(project_dir, pr_id)
    if pr is None:
        raise ValueError(f"PR '{pr_id}' not found")
    if not body or not body.strip():
        raise ValueError("Comment body required")
    comment = PRComment(
        id=f"c-{uuid.uuid4().hex[:8]}",
        author=author or "unknown",
        body=body.strip(),
        created_at=_now_iso(),
        comment_type=comment_type if comment_type in ("human", "ai_review") else "human",
        file=file,
        line=line,
    )
    pr.comments.append(comment)
    _save_pr(project_dir, pr)
    return comment


def approve_pr(project_dir: Path, pr_id: str, reviewer: str) -> PullRequest:
    pr = _load_pr(project_dir, pr_id)
    if pr is None:
        raise ValueError(f"PR '{pr_id}' not found")
    if pr.status != "open":
        raise ValueError(f"Cannot approve {pr.status} PR")
    reviewer = reviewer or "unknown"
    if reviewer not in pr.approvers:
        pr.approvers.append(reviewer)
    # Remove from change_requesters if they previously requested changes
    pr.change_requesters = [r for r in pr.change_requesters if r != reviewer]
    _save_pr(project_dir, pr)
    return pr


def request_changes(project_dir: Path, pr_id: str, reviewer: str, reason: str = "") -> PullRequest:
    pr = _load_pr(project_dir, pr_id)
    if pr is None:
        raise ValueError(f"PR '{pr_id}' not found")
    if pr.status != "open":
        raise ValueError(f"Cannot request changes on {pr.status} PR")
    reviewer = reviewer or "unknown"
    if reviewer not in pr.change_requesters:
        pr.change_requesters.append(reviewer)
    # Clear any prior approval from this reviewer
    pr.approvers = [r for r in pr.approvers if r != reviewer]
    if reason.strip():
        pr.comments.append(PRComment(
            id=f"c-{uuid.uuid4().hex[:8]}",
            author=reviewer,
            body=f"Requested changes: {reason.strip()}",
            created_at=_now_iso(),
        ))
    _save_pr(project_dir, pr)
    return pr


# ---------------------------------------------------------------------------
# Lineage impact
# ---------------------------------------------------------------------------


def _compute_lineage_impact(
    changed_files: list[str],
    dag: list,
    project_dir: Path,
) -> dict:
    """Map changed SQL files to their downstream impact.

    Returns ``{changed: [...model fqns...], impacted: [...downstream fqns...]}``.
    """
    changed_models: set[str] = set()
    project_root = project_dir.resolve()
    # Normalize each changed file to posix-separator relative path for matching
    changed_rel = {f.replace("\\", "/") for f in changed_files}

    for model in dag:
        try:
            rel = str(model.path.resolve().relative_to(project_root)).replace("\\", "/")
        except (ValueError, OSError):
            continue
        if rel in changed_rel:
            changed_models.add(model.full_name)

    # BFS downstream
    impacted: set[str] = set(changed_models)
    work = list(changed_models)
    while work:
        current = work.pop()
        for model in dag:
            if current in (model.depends_on or []) and model.full_name not in impacted:
                impacted.add(model.full_name)
                work.append(model.full_name)

    downstream_only = impacted - changed_models
    return {
        "changed": sorted(changed_models),
        "impacted": sorted(downstream_only),
    }


# ---------------------------------------------------------------------------
# Cross-attached diff
# ---------------------------------------------------------------------------


def _diff_across_attached(
    main_conn: duckdb.DuckDBPyConnection,
    dag: list | None = None,
    attached_alias: str = "pr_db",
) -> dict[str, dict]:
    """Diff every user table in main vs the attached PR database.

    Enumerates the union of tables from both databases so newly-added tables
    on the PR branch show up as ``status='added'`` and tables removed on the
    PR branch show up as ``status='removed'``. The ``dag`` parameter is kept
    for backward compatibility but is not used — the table list comes from
    DuckDB's catalog directly.

    Returns a mapping of ``schema.name`` to a diff record with status, row
    counts, added/removed counts, and schema column changes. System schemas
    (``_havn``, ``information_schema``, ``main``) are skipped.
    """
    _ = dag  # unused — kept for backward compat
    results: dict[str, dict] = {}

    main_db = main_conn.execute("SELECT current_database()").fetchone()[0]
    skip_schemas = {"_havn", "information_schema", "main"}

    try:
        main_tables = main_conn.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE database_name = ? AND internal = false",
            [main_db],
        ).fetchall()
        pr_tables = main_conn.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE database_name = ? AND internal = false",
            [attached_alias],
        ).fetchall()
    except Exception as e:
        logger.debug("Table enumeration failed: %s", e)
        return results

    main_set = {(s, n) for s, n in main_tables if s not in skip_schemas}
    pr_set = {(s, n) for s, n in pr_tables if s not in skip_schemas}
    all_tables = sorted(main_set | pr_set)

    for schema, name in all_tables:
        fqn = f"{schema}.{name}"
        main_exists = (schema, name) in main_set
        pr_exists = (schema, name) in pr_set

        if not main_exists:
            try:
                pr_count = main_conn.execute(
                    f'SELECT COUNT(*) FROM "{attached_alias}"."{schema}"."{name}"'
                ).fetchone()[0]
            except Exception:
                pr_count = 0
            results[fqn] = {
                "status": "added",
                "main_rows": 0,
                "pr_rows": pr_count,
                "added_rows": pr_count,
                "removed_rows": 0,
                "schema_changes": [],
            }
            continue

        if not pr_exists:
            try:
                main_count = main_conn.execute(
                    f'SELECT COUNT(*) FROM "{schema}"."{name}"'
                ).fetchone()[0]
            except Exception:
                main_count = 0
            results[fqn] = {
                "status": "removed",
                "main_rows": main_count,
                "pr_rows": 0,
                "added_rows": 0,
                "removed_rows": main_count,
                "schema_changes": [],
            }
            continue

        # Both exist — schema diff first
        try:
            main_cols = main_conn.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE database_name = current_database() "
                "AND schema_name = ? AND table_name = ? ORDER BY column_index",
                [schema, name],
            ).fetchall()
            pr_cols = main_conn.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE database_name = ? "
                "AND schema_name = ? AND table_name = ? ORDER BY column_index",
                [attached_alias, schema, name],
            ).fetchall()
        except Exception as e:
            logger.debug("Column introspection failed for %s: %s", fqn, e)
            continue

        main_col_map = {c[0]: c[1] for c in main_cols}
        pr_col_map = {c[0]: c[1] for c in pr_cols}
        schema_changes: list[dict] = []
        for col, dtype in pr_col_map.items():
            if col not in main_col_map:
                schema_changes.append({"type": "added", "column": col, "data_type": dtype})
            elif main_col_map[col] != dtype:
                schema_changes.append({
                    "type": "type_changed",
                    "column": col,
                    "from": main_col_map[col],
                    "to": dtype,
                })
        for col in main_col_map:
            if col not in pr_col_map:
                schema_changes.append({"type": "removed", "column": col})

        # Row counts — query each side independently so a failure on one
        # doesn't zero both and produce a misleading "removed" delta
        main_count: int | None = None
        pr_count: int | None = None
        try:
            main_count = main_conn.execute(
                f'SELECT COUNT(*) FROM "{schema}"."{name}"'
            ).fetchone()[0]
        except Exception as e:
            logger.debug("main row count failed for %s: %s", fqn, e)
        try:
            pr_count = main_conn.execute(
                f'SELECT COUNT(*) FROM "{attached_alias}"."{schema}"."{name}"'
            ).fetchone()[0]
        except Exception as e:
            logger.debug("pr row count failed for %s: %s", fqn, e)

        added = 0
        removed = 0
        # Compute row-level diff using columns common to both sides. Previously
        # any schema change (even an additive-only new column) skipped this
        # entirely and silently reported 0 added/removed — that hid row
        # modifications. We now diff common columns whenever we have any.
        common_cols = [
            c for c in main_col_map
            if c in pr_col_map and main_col_map[c] == pr_col_map[c]
        ]
        if common_cols:
            col_list = ", ".join(f'"{c}"' for c in common_cols)
            try:
                added = main_conn.execute(
                    f"SELECT COUNT(*) FROM ("
                    f'SELECT {col_list} FROM "{attached_alias}"."{schema}"."{name}" '
                    f"EXCEPT "
                    f'SELECT {col_list} FROM "{schema}"."{name}"'
                    f")"
                ).fetchone()[0]
                removed = main_conn.execute(
                    f"SELECT COUNT(*) FROM ("
                    f'SELECT {col_list} FROM "{schema}"."{name}" '
                    f"EXCEPT "
                    f'SELECT {col_list} FROM "{attached_alias}"."{schema}"."{name}"'
                    f")"
                ).fetchone()[0]
            except Exception as e:
                logger.debug("Row diff failed for %s: %s", fqn, e)

        # Use 0 as the displayed fallback for failed row counts, but mark the
        # entry modified whenever anything differs
        display_main = main_count if main_count is not None else 0
        display_pr = pr_count if pr_count is not None else 0
        status = "unchanged"
        if schema_changes or added or removed or display_main != display_pr:
            status = "modified"
        results[fqn] = {
            "status": status,
            "main_rows": display_main,
            "pr_rows": display_pr,
            "added_rows": added,
            "removed_rows": removed,
            "schema_changes": schema_changes,
        }

    return results


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


# Serializes PR builds within a single process. DuckDB forbids opening the
# same file twice in one process, so every build shares the caller's main
# warehouse connection to ATTACH/DETACH the PR database. Without this lock,
# two concurrent builds would race on that single connection and could see
# each other's ``pr_db`` attachment or clone into the same worktree path.
_build_lock = threading.Lock()


def _pr_build_root(project_dir: Path) -> Path:
    return project_dir / ".havn" / "pr-build"


def _clone_warehouse_via_attach(
    source_conn: duckdb.DuckDBPyConnection,
    dest_path: Path,
) -> None:
    """Clone the source warehouse into a new DuckDB file via ATTACH.

    DuckDB forbids opening the same file through two handles in one process
    (unique-file-handle rule), so we use the caller's existing connection to
    attach the destination file in read-write mode and copy every user table
    into it. The ``_havn`` metadata schema is deliberately skipped — metadata
    tables use PRIMARY KEY constraints that ``CREATE TABLE AS SELECT`` does
    not preserve, and the PR warehouse is better served by fresh metadata
    tables created via ``ensure_meta_table`` on the next connection.

    The destination is detached after cloning so another connection can open
    the resulting file without hitting the unique-handle rule.
    """
    if dest_path.exists():
        dest_path.unlink()
    attach_path = str(dest_path).replace("'", "''")
    source_conn.execute(f"ATTACH '{attach_path}' AS pr_build (READ_WRITE)")
    try:
        source_db = source_conn.execute("SELECT current_database()").fetchone()[0]
        schemas = source_conn.execute(
            "SELECT DISTINCT schema_name FROM duckdb_tables() "
            "WHERE database_name = ? AND internal = false "
            "AND schema_name != '_havn'",
            [source_db],
        ).fetchall()
        for (schema,) in schemas:
            source_conn.execute(f'CREATE SCHEMA IF NOT EXISTS pr_build."{schema}"')

        tables = source_conn.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            "WHERE database_name = ? AND internal = false "
            "AND schema_name != '_havn'",
            [source_db],
        ).fetchall()
        for schema, name in tables:
            try:
                source_conn.execute(
                    f'CREATE TABLE pr_build."{schema}"."{name}" AS '
                    f'SELECT * FROM "{schema}"."{name}"'
                )
            except Exception as e:
                logger.debug("Skipped cloning %s.%s: %s", schema, name, e)
    finally:
        try:
            source_conn.execute("DETACH pr_build")
        except Exception:
            pass


def _worktree_cleanup(project_dir: Path, worktree_path: Path) -> None:
    """Best-effort cleanup of a worktree. Safe to call even if nothing exists."""
    try:
        if worktree_path.exists():
            _run_git(
                project_dir,
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
                timeout=30,
            )
    except Exception as e:
        logger.debug("Worktree cleanup raised: %s", e)
    # If git still thinks the worktree exists but the path is gone, prune it
    try:
        _run_git(project_dir, "worktree", "prune", timeout=10)
    except Exception:
        pass
    # Final belt-and-braces: rm -rf the directory if git left it behind
    if worktree_path.exists():
        try:
            shutil.rmtree(worktree_path, ignore_errors=True)
        except Exception:
            pass


def build_pr(
    project_dir: Path,
    pr_id: str,
    conn: duckdb.DuckDBPyConnection,
    db_path: Path | str | None = None,
) -> dict:
    """Build a PR branch in an isolated worktree and diff against main.

    Creates ``.havn/pr-build/{pr_id}/``, clones the main warehouse into it so
    landing tables are available, runs ``havn transform`` on the PR branch's
    SQL, then ATTACHes the result and diffs every table. The worktree is
    always removed in the ``finally`` block, even on failure or interrupt.

    Args:
        project_dir: Project root.
        pr_id: PR identifier (JSON file stem in ``.havn/prs/``).
        conn: DuckDB connection on the main warehouse. Used to write the clone
            via ATTACH and to attach the PR warehouse read-only for diffing.
        db_path: Path to the main warehouse file. If ``None``, the project
            config is loaded to resolve it (defaults to
            ``project_dir/warehouse.duckdb`` when no config is present).
    """
    from havn.engine.database import connect, ensure_meta_table
    from havn.engine.transform import run_transform
    from havn.engine.transform.discovery import build_dag, discover_models

    # Serialize builds within a single process so concurrent requests don't
    # race on the shared main-warehouse connection's ATTACH/DETACH state.
    with _build_lock:
        return _build_pr_locked(
            project_dir, pr_id, conn, db_path,
            _connect=connect,
            _ensure_meta_table=ensure_meta_table,
            _run_transform=run_transform,
            _build_dag=build_dag,
            _discover_models=discover_models,
        )


def _build_pr_locked(
    project_dir: Path,
    pr_id: str,
    conn: duckdb.DuckDBPyConnection,
    db_path: Path | str | None,
    *,
    _connect,
    _ensure_meta_table,
    _run_transform,
    _build_dag,
    _discover_models,
) -> dict:
    """Implementation of build_pr, called under ``_build_lock``."""
    if not is_git_repo(project_dir):
        raise RuntimeError("Not a git repository")

    pr = get_pr(project_dir, pr_id)
    if pr is None:
        raise ValueError(f"PR '{pr_id}' not found")
    if not _validate_branch_name(pr.head_ref):
        raise ValueError(f"Invalid head_ref: {pr.head_ref}")

    # Resolve the main warehouse path honouring project.yml's database.path
    if db_path is None:
        try:
            from havn.config import load_project
            cfg = load_project(project_dir)
            db_path = project_dir / cfg.database.path
        except Exception:
            db_path = project_dir / "warehouse.duckdb"
    main_warehouse = Path(db_path)

    ensure_pr_builds_table(conn)

    build_id = f"build-{uuid.uuid4().hex[:8]}"
    worktree_root = _pr_build_root(project_dir)
    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_root / pr_id

    record: dict[str, Any] = {
        "id": build_id,
        "pr_id": pr_id,
        "branch_head": None,
        "status": "running",
        "started_at": _now_iso(),
        "finished_at": None,
        "duration_ms": 0,
        "data_diff": None,
        "lineage_impact": None,
        "contract_results": None,
        "error": None,
    }
    _save_build_record(conn, record)

    start = time.perf_counter()
    pr_conn: duckdb.DuckDBPyConnection | None = None
    attached = False
    pr_branch_dag: list = []

    # Defensive cleanup: remove any stale worktree from a previous interrupted
    # build, and drop any lingering pr_db attachment on the caller's connection
    # in case a prior build crashed before its finally block ran.
    _worktree_cleanup(project_dir, worktree_path)
    try:
        conn.execute("DETACH pr_db")
    except Exception:
        pass  # not attached — fine

    try:
        head_hash_res = _run_git(project_dir, "rev-parse", pr.head_ref)
        if head_hash_res.returncode != 0:
            raise RuntimeError(
                f"Cannot resolve {pr.head_ref}: {head_hash_res.stderr.strip() or 'unknown ref'}"
            )
        branch_head = head_hash_res.stdout.strip()
        record["branch_head"] = branch_head

        # Create the worktree pinned to the exact commit (avoids branch conflict
        # if head_ref is checked out elsewhere in the main working tree)
        wt_result = _run_git(
            project_dir,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(worktree_path),
            branch_head,
            timeout=60,
        )
        if wt_result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {wt_result.stderr.strip() or wt_result.stdout.strip()}"
            )

        # Clone the main warehouse into the worktree. DuckDB forbids two
        # handles to the same file within one process, so we use the caller's
        # connection to write the clone via ATTACH rather than copying bytes.
        pr_warehouse = worktree_path / "warehouse.duckdb"
        if main_warehouse.exists():
            try:
                conn.execute("FORCE CHECKPOINT")
            except Exception:
                pass
            _clone_warehouse_via_attach(conn, pr_warehouse)

        # Discover the PR branch's DAG from the worktree BEFORE teardown so
        # new-on-PR models are available for the lineage impact computation.
        pr_branch_dag = _build_dag(_discover_models(worktree_path / "transform"))

        # Open a connection on the PR warehouse and run the PR branch's transforms
        pr_conn = _connect(pr_warehouse, project_dir=worktree_path)
        _ensure_meta_table(pr_conn)
        try:
            transform_results = _run_transform(
                pr_conn,
                worktree_path / "transform",
                db_path=str(pr_warehouse),
                project_dir=worktree_path,
            )
        finally:
            pr_conn.close()
            pr_conn = None

        transform_errors = {
            name: status for name, status in (transform_results or {}).items()
            if status in ("error", "assertion_failed")
        }
        if transform_errors:
            raise RuntimeError(
                "Transform errors in: " + ", ".join(sorted(transform_errors))
            )

        # ATTACH the PR warehouse read-only and diff against main
        attach_path = str(pr_warehouse).replace("'", "''")
        conn.execute(f"ATTACH '{attach_path}' AS pr_db (READ_ONLY)")
        attached = True

        data_diff = _diff_across_attached(conn, attached_alias="pr_db")
        record["data_diff"] = data_diff

        # Union of main DAG + PR-branch DAG so new-on-PR models show up as
        # changed in the lineage impact result
        main_dag = _build_dag(_discover_models(project_dir / "transform"))
        dag_by_fqn = {m.full_name: m for m in main_dag}
        for m in pr_branch_dag:
            dag_by_fqn.setdefault(m.full_name, m)
        union_dag = list(dag_by_fqn.values())

        changed_files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
        record["lineage_impact"] = _compute_lineage_impact(
            changed_files, union_dag, project_dir
        )

        record["status"] = "success"
    except Exception as e:
        logger.warning("build_pr(%s) failed: %s", pr_id, e)
        record["status"] = "error"
        record["error"] = str(e)
    finally:
        if pr_conn is not None:
            try:
                pr_conn.close()
            except Exception:
                pass
        if attached:
            try:
                conn.execute("DETACH pr_db")
            except Exception:
                pass
        _worktree_cleanup(project_dir, worktree_path)

    record["duration_ms"] = int((time.perf_counter() - start) * 1000)
    record["finished_at"] = _now_iso()
    _save_build_record(conn, record)
    return record


def get_latest_build(conn: duckdb.DuckDBPyConnection, pr_id: str) -> dict | None:
    return _fetch_build_record(conn, pr_id)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def can_merge(project_dir: Path, pr: PullRequest) -> dict:
    """Non-destructive virtual merge check via ``git merge-tree``.

    Uses the modern 2-arg form on git >= 2.38 (returns non-zero on conflict).
    Falls back to the 3-arg form on older git (emits ``<<<<<<<`` markers in
    stdout on conflict but always returns 0). Does not touch the working tree.

    Returns ``{can_merge, reason}``.
    """
    if not is_git_repo(project_dir):
        return {"can_merge": False, "reason": "Not a git repository"}
    if not _validate_branch_name(pr.base_ref) or not _validate_branch_name(pr.head_ref):
        return {"can_merge": False, "reason": "Invalid branch ref"}

    base_res = _run_git(project_dir, "rev-parse", pr.base_ref)
    head_res = _run_git(project_dir, "rev-parse", pr.head_ref)
    if base_res.returncode != 0 or head_res.returncode != 0:
        return {"can_merge": False, "reason": "Could not resolve base or head ref"}
    base_sha = base_res.stdout.strip()
    head_sha = head_res.stdout.strip()
    if not base_sha or not head_sha:
        return {"can_merge": False, "reason": "Empty commit SHA"}

    # Try modern 2-arg form first (git >= 2.38). A usage error or "unknown
    # option" comes back as non-zero with a stderr mentioning "usage" — fall
    # back to the 3-arg form in that case.
    result = _run_git(project_dir, "merge-tree", base_sha, head_sha, timeout=30)
    stderr_lower = (result.stderr or "").lower()
    old_git = (
        result.returncode != 0
        and ("usage" in stderr_lower or "unknown option" in stderr_lower)
    )

    if not old_git:
        if result.returncode != 0:
            reason = result.stderr.strip() or result.stdout.strip() or "merge conflict"
            return {"can_merge": False, "reason": reason}
        if "<<<<<<<" in result.stdout or "CONFLICT" in result.stdout:
            return {"can_merge": False, "reason": "merge conflict detected"}
        return {"can_merge": True, "reason": None}

    # Legacy 3-arg form: git merge-tree <merge-base> <branch1> <branch2>
    mb_res = _run_git(project_dir, "merge-base", base_sha, head_sha)
    if mb_res.returncode != 0 or not mb_res.stdout.strip():
        return {"can_merge": False, "reason": "Could not find merge base"}
    merge_base = mb_res.stdout.strip()
    legacy_res = _run_git(
        project_dir, "merge-tree", merge_base, base_sha, head_sha, timeout=30
    )
    # The 3-arg form always exits 0; conflicts show as <<<<<<< markers
    if "<<<<<<<" in legacy_res.stdout or "changed in both" in legacy_res.stdout:
        return {"can_merge": False, "reason": "merge conflict detected"}
    return {"can_merge": True, "reason": None}


def merge_pr(
    project_dir: Path,
    pr_id: str,
    user: str,
    conn: duckdb.DuckDBPyConnection,
) -> dict:
    """Merge the PR into its base branch with a pre-merge version snapshot.

    Refuses to merge if:
    - PR is not open
    - Approval is required and no one has approved
    - Any reviewer has requested changes
    - git merge-tree reports conflicts
    - Working tree is dirty (would block the checkout)
    """
    from havn.engine.versioning import create_version

    pr = get_pr(project_dir, pr_id)
    if pr is None:
        return {"success": False, "error": f"PR '{pr_id}' not found"}
    if pr.status != "open":
        return {"success": False, "error": f"Cannot merge {pr.status} PR"}
    if pr.require_approval and not pr.approvers:
        return {"success": False, "error": "PR requires at least one approval"}
    if pr.change_requesters:
        return {
            "success": False,
            "error": f"Reviewers have requested changes: {', '.join(pr.change_requesters)}",
        }
    if is_dirty(project_dir):
        return {
            "success": False,
            "error": "Working tree has uncommitted changes — commit or stash before merging",
        }

    mc = can_merge(project_dir, pr)
    if not mc["can_merge"]:
        return {"success": False, "error": f"Cannot merge: {mc['reason']}"}

    # Pre-merge snapshot for rollback
    try:
        create_version(
            conn,
            project_dir,
            description=f"Pre-merge snapshot for PR {pr_id}",
            trigger="pr_merge",
        )
    except Exception as e:
        logger.debug("Pre-merge snapshot skipped: %s", e)

    # Remember the current branch so we can restore it on failure
    original_branch = current_branch(project_dir)
    checkout = git_checkout_branch(project_dir, pr.base_ref)
    if not checkout.get("success"):
        return {
            "success": False,
            "error": f"Could not checkout {pr.base_ref}: {checkout.get('error', '')}",
        }

    merge_res = _run_git(
        project_dir,
        "merge",
        "--no-ff",
        "-m",
        f"Merge PR {pr_id}: {pr.title}",
        pr.head_ref,
        timeout=60,
    )
    if merge_res.returncode != 0:
        # Abort the failed merge only if git is actually in a merging state.
        # A non-merge failure (lock file, permission error, etc.) has nothing
        # to abort, and calling `merge --abort` on a clean tree logs a noisy
        # "There is no merge in progress" error that masks the real failure.
        if (project_dir / ".git" / "MERGE_HEAD").exists():
            _run_git(project_dir, "merge", "--abort")
        if original_branch and original_branch != pr.base_ref:
            git_checkout_branch(project_dir, original_branch)
        return {
            "success": False,
            "error": merge_res.stderr.strip() or "git merge failed",
        }

    merge_commit = last_commit_hash(project_dir)
    pr.status = "merged"
    pr.merged_by = user or "unknown"
    pr.merged_at = _now_iso()
    _save_pr(project_dir, pr)

    # Restore the original branch the user was on, so the merge doesn't
    # silently leave them on base_ref
    if original_branch and original_branch != pr.base_ref:
        try:
            git_checkout_branch(project_dir, original_branch)
        except Exception as e:
            logger.debug("Could not restore original branch %s: %s", original_branch, e)

    return {
        "success": True,
        "merge_commit": merge_commit,
        "base_ref": pr.base_ref,
        "head_ref": pr.head_ref,
    }


# ---------------------------------------------------------------------------
# Review prompt
# ---------------------------------------------------------------------------


def build_review_prompt(
    project_dir: Path,
    pr: PullRequest,
    build: dict | None = None,
) -> str:
    """Construct a plain-text prompt for the developer's own agent.

    The server does NOT call any LLM. It just returns this text; the web UI
    (via the existing agent sidebar WebSocket) or the CLI (piping to stdout)
    sends it to whichever agent the developer uses.
    """
    lines: list[str] = []
    lines.append(
        f"You are reviewing a pull request for a havn data platform project."
    )
    lines.append(
        "Look for SQL correctness, missing assertions or contracts, row-count"
        " anomalies, schema regressions, and lineage impact issues. Suggest"
        " specific file:line fixes where possible."
    )
    lines.append("")
    lines.append(f"# PR {pr.id}: {pr.title}")
    lines.append(f"Author: {pr.author}")
    lines.append(f"Branch: {pr.head_ref} -> {pr.base_ref}")
    if pr.description:
        lines.append("")
        lines.append("## Description")
        lines.append(pr.description)

    # Changed files
    changed_files = diff_files_between(project_dir, pr.base_ref, pr.head_ref)
    lines.append("")
    lines.append("## Files changed")
    if changed_files:
        for f in changed_files:
            lines.append(f"- {f}")
    else:
        lines.append("(no files detected)")

    # Data impact
    if build and build.get("data_diff"):
        lines.append("")
        lines.append("## Data impact")
        modified = []
        added = []
        removed = []
        for fqn, d in build["data_diff"].items():
            status = d.get("status")
            if status == "modified":
                modified.append((fqn, d))
            elif status == "added":
                added.append((fqn, d))
            elif status == "removed":
                removed.append((fqn, d))
        if added:
            lines.append("### New tables")
            for fqn, d in added:
                lines.append(f"- {fqn} ({d.get('pr_rows', 0)} rows)")
        if removed:
            lines.append("### Removed tables")
            for fqn, d in removed:
                lines.append(f"- {fqn} (was {d.get('main_rows', 0)} rows)")
        if modified:
            lines.append("### Modified tables")
            for fqn, d in modified:
                delta = (d.get("pr_rows", 0) or 0) - (d.get("main_rows", 0) or 0)
                sign = "+" if delta >= 0 else ""
                lines.append(
                    f"- {fqn}: {d.get('main_rows', 0)} -> {d.get('pr_rows', 0)} "
                    f"({sign}{delta}), +{d.get('added_rows', 0)}/-{d.get('removed_rows', 0)} rows"
                )
                for sc in d.get("schema_changes") or []:
                    if sc.get("type") == "added":
                        lines.append(f"    + column {sc['column']} ({sc.get('data_type', '?')})")
                    elif sc.get("type") == "removed":
                        lines.append(f"    - column {sc['column']}")
                    elif sc.get("type") == "type_changed":
                        lines.append(
                            f"    ~ column {sc['column']}: {sc.get('from')} -> {sc.get('to')}"
                        )
        if not (added or removed or modified):
            lines.append("(no data changes detected)")
    else:
        lines.append("")
        lines.append("## Data impact")
        lines.append("(no build run yet — run `havn pr build` first for data diff)")

    # Lineage impact
    if build and build.get("lineage_impact"):
        li = build["lineage_impact"]
        lines.append("")
        lines.append("## Lineage impact")
        if li.get("changed"):
            lines.append(f"Changed models: {', '.join(li['changed'])}")
        if li.get("impacted"):
            lines.append(f"Downstream impacted: {', '.join(li['impacted'])}")
        if not li.get("changed") and not li.get("impacted"):
            lines.append("(none)")

    if pr.comments:
        lines.append("")
        lines.append("## Existing comments")
        for c in pr.comments:
            prefix = "[AI]" if c.comment_type == "ai_review" else "[review]"
            lines.append(f"{prefix} {c.author}: {c.body}")

    lines.append("")
    lines.append(
        "Respond with specific, actionable feedback. Do not just praise the"
        " changes — flag any risks, missing tests, or assumptions you would"
        " challenge in code review."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State status (.havn/prs/ dirty check)
# ---------------------------------------------------------------------------


def pr_state_status(project_dir: Path) -> dict:
    """Report whether .havn/prs/ has unpushed changes the user should share."""
    if not is_git_repo(project_dir):
        return {"dirty": False, "unpushed_count": 0, "is_git_repo": False}
    # Any modification to .havn/prs/ shows up in git status
    dirty = False
    unpushed = 0
    status = _run_git(project_dir, "status", "--porcelain", ".havn/prs")
    if status.returncode == 0:
        lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
        dirty = len(lines) > 0
    # Count commits ahead of the remote-tracked branch that touch .havn/prs
    upstream = _run_git(project_dir, "rev-parse", "--abbrev-ref", "@{u}")
    if upstream.returncode == 0 and upstream.stdout.strip():
        log = _run_git(
            project_dir,
            "log",
            "--oneline",
            "@{u}..HEAD",
            "--",
            ".havn/prs",
        )
        if log.returncode == 0:
            unpushed = len([ln for ln in log.stdout.splitlines() if ln.strip()])
    return {"dirty": dirty, "unpushed_count": unpushed, "is_git_repo": True}

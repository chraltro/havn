"""A warehouse per git branch.

With ``branches: {enabled: true}`` in project.yml, checking out a git branch
also switches to that branch's data. On any branch other than the main one,
the warehouse resolves to a file of its own (``.havn/branches/<branch>.duckdb``
by default) that starts empty. Every model the branch has not built is read
from the *base* warehouse -- the ``base`` environment's, or the top-level
``database.path`` -- through defer (:mod:`havn.engine.defer`): the base is
attached read-only for the length of a run, and unbuilt references are
rewritten to it. So a branch warehouse is cheap to create, and only what the
branch changes is ever materialized in it.

The pieces:

- **Resolution** (:func:`resolve_branch`) runs inside ``load_project``, so
  every command and the server see the same warehouse. It reads ``.git/HEAD``
  directly rather than shelling out, because it runs on every config load.
  Explicit choices win over it: ``--env`` and ``.havn-env`` keep the
  environment they name. ``HAVN_BRANCH`` (or a command's ``--name``) forces a
  branch, which is how CI names the branch on a detached pull-request
  checkout.
- **Plan** (:func:`plan_branch`): ``state:modified+`` judged against the
  *base* warehouse's build state -- the models whose SQL or upstream differs
  from what the base last built, plus everything downstream.
- **Build** (:func:`build_branch`) builds the plan into the branch warehouse
  with defer to the base, and drops branch copies of models that no longer
  differ from the base so they fall through to it again.
- **Status** (:func:`branch_status`): what is materialized locally, what is
  deferred, and which local models are stale because the base rebuilt one of
  their deferred upstreams after them.
- **Diff** (:func:`diff_branch`): schema and row-level diff of every model the
  branch built against the same model in the base, through
  :func:`havn.engine.diff.diff_model`. :func:`format_markdown` renders it for
  a pull-request comment and ``havn branch diff --markdown``.
- **Housekeeping** (:func:`list_branch_warehouses`, :func:`clean_branches`,
  :func:`remove_branch_warehouse`).

Safety: nothing here writes to the base. Every base access is a read-only
ATTACH, and the delete paths refuse any file that is the base, an
environment's warehouse, or not a branch warehouse at all.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from havn.textio import read_project_text

logger = logging.getLogger("havn.branches")

ENV_VAR = "HAVN_BRANCH"
MARKDOWN_MARKER = "<!-- havn-data-diff -->"
# GitHub rejects issue comments over 65,536 characters.
MARKDOWN_LIMIT = 60_000


class BranchError(Exception):
    """A branch operation cannot run, with a message saying why."""


# ---------------------------------------------------------------------------
# Git HEAD, read from the files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHead:
    """What is checked out, as far as ``.git/HEAD`` says."""

    in_repo: bool = False
    branch: str | None = None
    detached: bool = False
    sha: str | None = None
    git_dir: str | None = None
    common_dir: str | None = None

    @property
    def key(self) -> str:
        """A string that changes exactly when the checkout does."""
        if not self.in_repo:
            return "none"
        if self.branch:
            return f"branch:{self.branch}"
        return f"detached:{self.sha or ''}"


def _find_git_dirs(start: Path) -> tuple[Path, Path] | None:
    """``(git_dir, common_dir)`` for the repository containing ``start``.

    Handles the ``.git`` *file* of a worktree or submodule (``gitdir: ...``)
    and the ``commondir`` file that points a worktree at the shared refs.
    """
    try:
        start = start.resolve()
    except OSError:
        pass
    for d in (start, *start.parents):
        dotgit = d / ".git"
        try:
            if dotgit.is_dir():
                git_dir = dotgit
            elif dotgit.is_file():
                text = read_project_text(dotgit).strip()
                if not text.startswith("gitdir:"):
                    return None
                git_dir = Path(text[len("gitdir:"):].strip())
                if not git_dir.is_absolute():
                    git_dir = d / git_dir
            else:
                continue
            common = git_dir
            commondir = git_dir / "commondir"
            if commondir.is_file():
                c = Path(read_project_text(commondir).strip())
                common = c if c.is_absolute() else git_dir / c
            return git_dir, common
        except OSError:
            return None
    return None


def _packed_refs(common: Path) -> dict[str, str]:
    path = common / "packed-refs"
    out: dict[str, str] = {}
    try:
        text = read_project_text(path)
    except OSError:
        return out
    for line in text.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            out[parts[1].strip()] = parts[0].strip()
    return out


def _resolve_ref(common: Path, ref: str) -> str | None:
    loose = common / ref
    try:
        if loose.is_file():
            return read_project_text(loose).strip() or None
    except OSError:
        return None
    return _packed_refs(common).get(ref)


def read_git_head(project_dir: Path | str) -> GitHead:
    """The checked-out branch of the repository holding ``project_dir``.

    Never raises and never runs git: it is called on every config load and,
    in ``havn serve``, on every request (throttled). No repository, an
    unreadable HEAD and a detached HEAD all come back as values.
    """
    dirs = _find_git_dirs(Path(project_dir))
    if dirs is None:
        return GitHead()
    git_dir, common = dirs
    try:
        head = read_project_text(git_dir / "HEAD").strip()
    except OSError:
        return GitHead(in_repo=True, detached=True, git_dir=str(git_dir), common_dir=str(common))
    if head.startswith("ref:"):
        ref = head[len("ref:"):].strip()
        if ref.startswith("refs/heads/"):
            return GitHead(
                in_repo=True,
                branch=ref[len("refs/heads/"):],
                sha=_resolve_ref(common, ref),
                git_dir=str(git_dir),
                common_dir=str(common),
            )
        return GitHead(in_repo=True, detached=True, git_dir=str(git_dir), common_dir=str(common))
    return GitHead(
        in_repo=True, detached=True, sha=head or None,
        git_dir=str(git_dir), common_dir=str(common),
    )


def _local_branch_exists(head: GitHead, name: str) -> bool:
    if not head.common_dir:
        return False
    return _resolve_ref(Path(head.common_dir), f"refs/heads/{name}") is not None


def detect_main_branches(head: GitHead) -> list[str]:
    """``main``, or ``master`` in a repository that has a master and no main."""
    if _local_branch_exists(head, "main"):
        return ["main"]
    if _local_branch_exists(head, "master"):
        return ["master"]
    return ["main"]


# ---------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_PLAIN_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")


def branch_slug(name: str) -> str:
    """A filename-safe, collision-resistant form of a git branch name.

    A plain lowercase name (``fix-orders``) is used as it is. Anything else --
    slashes, uppercase, dots, a Windows device name, more than 60 characters --
    is flattened to ``[a-z0-9_-]`` and gets 8 hex characters of the full
    name's hash, so ``feature/x`` and ``feature-x`` (or ``Fix`` and ``fix`` on
    a case-insensitive filesystem) never share a warehouse.
    """
    name = (name or "").strip()
    if _PLAIN_SLUG.match(name) and name not in _WINDOWS_RESERVED:
        return name
    flat = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")[:48] or "branch"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{flat}-{digest}"


def _abs(project_dir: Path, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path(project_dir) / p


def _base_location(
    base: str | None,
    environments: dict[str, Any],
    db_raw: dict[str, Any],
) -> tuple[str, str]:
    """``(label, path)`` of the base warehouse as project.yml has it."""
    top = (db_raw or {}).get("path") or "warehouse.duckdb"
    if base:
        env_cfg = environments.get(base)
        env_db = (getattr(env_cfg, "database", None) or {}) if env_cfg is not None else {}
        return base, str(env_db.get("path") or top)
    return "base", str(top)


def resolve_branch(
    project_dir: Path,
    branches: Any,
    *,
    environments: dict[str, Any],
    db_raw: dict[str, Any],
    backend: str = "duckdb",
    env_source: str = "none",
    active_env: str | None = None,
    forced_branch: str | None = None,
    base_override: str | None = None,
    use_branches: bool = True,
    git_head: GitHead | None = None,
):
    """Decide whether this config load uses a branch warehouse.

    Returns a :class:`havn.config.BranchState`. ``load_project`` is the only
    caller that acts on it, so the CLI, the server and the scheduler agree.

    Order, first match wins:

    1. ``use_branches=False`` (``havn deploy``): never a branch warehouse.
    2. ``forced_branch`` (a command's ``--name``) or ``HAVN_BRANCH``: that
       branch, whatever is checked out and whatever ``.havn-env`` says.
    3. Branches not enabled: off.
    4. ``--env`` or ``.havn-env``: the environment asked for.
    5. No repository, or a detached HEAD: off.
    6. One of the main branches: off.
    7. Otherwise the checked-out branch.
    """
    from havn.config import BranchState

    base_label, base_path = _base_location(branches.base, environments, db_raw)
    if base_override:
        base_label, base_path = "base", str(base_override)
    head = git_head if git_head is not None else read_git_head(project_dir)
    main = list(branches.main or []) or detect_main_branches(head)

    state = BranchState(
        in_repo=head.in_repo,
        detached=head.detached,
        git_branch=head.branch,
        main_branches=main,
        base=None if base_override else branches.base,
        base_label=base_label,
        base_path=base_path,
        base_overridden=bool(base_override),
        source="git" if head.in_repo else "none",
    )

    def off(reason: str):
        state.reason = reason
        return state

    if not use_branches:
        return off(
            "branch warehouses are off here (havn deploy and havn serve --auth "
            "always use the base resolution)"
        )

    forced = forced_branch
    source = "--branch"
    if not forced:
        env_value = (os.environ.get(ENV_VAR) or "").strip()
        if env_value:
            forced, source = env_value, ENV_VAR

    if not branches.enabled and not forced:
        return off("branches are not enabled (set branches.enabled: true in project.yml)")
    if backend != "duckdb":
        return off(
            f"branch warehouses need the duckdb backend; this project uses {backend}"
        )

    if forced:
        name = forced
        state.git_branch = forced
        state.source = source
    else:
        if env_source in ("--env", ".havn-env"):
            return off(
                f"environment '{active_env}' was chosen explicitly ({env_source}); "
                "explicit environments win over branch warehouses"
                + (". Run `havn env reset` to follow git branches." if env_source == ".havn-env" else "")
            )
        if not head.in_repo:
            return off("not a git repository")
        if head.detached or not head.branch:
            return off("detached HEAD: no branch is checked out")
        name = head.branch

    if name in main:
        return off(f"'{name}' is a main branch; it uses the base warehouse resolution")

    slug = branch_slug(name)
    template = branches.path or ".havn/branches/{branch}.duckdb"
    path = template.replace("{branch}", slug)
    if _abs(project_dir, path).resolve() == _abs(project_dir, base_path).resolve():
        raise ValueError(
            f"branches.path resolves to the base warehouse ({base_path}) for branch "
            f"'{name}'; a branch must never write to its base"
        )
    state.active = True
    state.slug = slug
    state.path = path
    state.reason = f"on branch '{name}'"
    return state


def branch_warehouse_path(config: Any) -> Path | None:
    """Absolute path of the active branch warehouse, or None when off."""
    state = getattr(config, "branch", None)
    if state is None or not state.active or not state.path:
        return None
    return _abs(config.project_dir, state.path)


def base_warehouse_path(config: Any) -> Path:
    """Absolute path of the base warehouse a branch defers to."""
    state = config.branch
    if state.base_path:
        return _abs(config.project_dir, state.base_path)
    label, path = _base_location(
        config.branches.base, config.environments, (getattr(config, "_raw", {}) or {}).get("database", {})
    )
    return _abs(config.project_dir, path)


def _protected_paths(config: Any) -> set[Path]:
    """Warehouses no branch operation may ever delete."""
    raw_db = (getattr(config, "_raw", {}) or {}).get("database", {}) or {}
    out = {_abs(config.project_dir, raw_db.get("path") or "warehouse.duckdb").resolve()}
    for env_cfg in (config.environments or {}).values():
        p = (env_cfg.database or {}).get("path")
        if p:
            out.add(_abs(config.project_dir, p).resolve())
    try:
        out.add(base_warehouse_path(config).resolve())
    except Exception:
        pass
    return out


def _record_path(warehouse: Path) -> Path:
    return warehouse.with_suffix(".branch.json")


def write_branch_record(config: Any) -> None:
    """Note which git branch a branch warehouse belongs to, beside the file.

    The filename is a slug and slugs are lossy (``feature/x`` is
    ``feature-x-1a2b3c4d``), so ``havn branch list`` and ``clean`` read the
    real name from here. Written once, when the warehouse is first opened.
    Never raises: a missing record only costs ``list`` the pretty name.
    """
    path = branch_warehouse_path(config)
    if path is None:
        return
    record = _record_path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if record.exists():
            return
        state = config.branch
        record.write_text(json.dumps({
            "branch": state.git_branch,
            "slug": state.slug,
            "base": state.base,
            "base_path": state.base_path,
            "created_at": _now(),
        }, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.debug("Could not write the branch record %s: %s", record, e)


def _read_record(warehouse: Path) -> dict:
    record = _record_path(warehouse)
    try:
        return json.loads(read_project_text(record)) or {}
    except (OSError, ValueError):
        return {}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The base, attached read-only
# ---------------------------------------------------------------------------


def require_active(config: Any) -> None:
    state = getattr(config, "branch", None)
    if state is None or not state.active:
        reason = state.reason if state is not None else "branches are not configured"
        raise BranchError(f"Not on a branch warehouse: {reason}.")


def require_base(config: Any) -> Path:
    """The base warehouse path, or a BranchError saying how to get one."""
    path = base_warehouse_path(config)
    if not path.exists():
        state = config.branch
        how = (
            f"Build it first (`havn transform --env {state.base}`)"
            if state.base else "Build it on the main branch first (`havn transform`)"
        )
        raise BranchError(
            f"The base warehouse '{state.base_label}' does not exist at {path}. "
            f"{how}, or pass --base PATH to use a copy (the CI artifact, a backup)."
        )
    return path


@contextmanager
def base_attached(conn, config: Any) -> Iterator[str]:
    """Attach the base read-only on ``conn`` for the block; yields its alias.

    Shares the refcounted attach of :func:`havn.engine.defer.defer_attachment`
    (same alias, derived from the path), so it nests with a transform run's own
    defer session instead of detaching the base out from under it.
    """
    from havn.engine.defer import alias_for_path, defer_attachment

    path = require_base(config)
    alias = alias_for_path(path)
    with defer_attachment(conn, path, alias=alias):
        yield alias


def _catalog_has(conn, catalog: str, schema: str, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE lower(table_catalog) = lower(?) AND table_schema = ? AND table_name = ?",
        [catalog, schema, table],
    ).fetchone()
    return bool(row and row[0])


def _model_state(conn, catalog: str | None = None) -> dict[str, dict]:
    """``{model: {built_at, row_count, materialized_as}}`` from one catalog."""
    prefix = f"{catalog}." if catalog else ""
    try:
        if catalog and not _catalog_has(conn, catalog, "_havn", "model_state"):
            return {}
        rows = conn.execute(
            f"SELECT model_path, last_run_at, row_count, materialized_as "
            f"FROM {prefix}_havn.model_state"
        ).fetchall()
    except Exception as e:
        logger.debug("model_state unreadable (%s): %s", catalog or "local", e)
        return {}
    return {
        str(r[0]).lower(): {"built_at": r[1], "row_count": r[2], "materialized_as": r[3]}
        for r in rows
    }


def _last_source_load(conn, catalog: str) -> Any:
    from havn.engine.transform.discovery import _SOURCE_LOAD_RUN_TYPES

    try:
        if not _catalog_has(conn, catalog, "_havn", "run_log"):
            return None
        placeholders = ", ".join("?" for _ in _SOURCE_LOAD_RUN_TYPES)
        row = conn.execute(
            f"SELECT max(coalesce(finished_at, started_at)) FROM {catalog}._havn.run_log "
            f"WHERE status = 'success' AND run_type IN ({placeholders})",
            list(_SOURCE_LOAD_RUN_TYPES),
        ).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("run_log unreadable in %s: %s", catalog, e)
        return None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def plan_branch(conn, config: Any, alias: str, models: list | None = None) -> list[str]:
    """Models that differ from what the base last built, plus downstream.

    ``state:modified+`` evaluated against the *base* warehouse's
    ``_havn.model_state``, through a cursor whose default catalog is the
    attached base. A base that has never been built has no build state, and
    then every model counts as modified. Returned in DAG order.
    """
    from havn.engine.selectors import select_models
    from havn.engine.transform import build_dag, discover_all_models

    if models is None:
        models = discover_all_models(config.project_dir, config)
    if not models:
        return []
    ordered = [m.full_name for m in build_dag(models)]
    cur = conn.cursor()
    try:
        cur.execute(f"USE {alias}")
        selection = select_models(
            ["state:modified+"], models, conn=cur, project_dir=config.project_dir,
        )
    finally:
        cur.close()
    chosen = set(selection.selected)
    return [name for name in ordered if name in chosen]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def branch_summary(config: Any) -> dict:
    """Resolution facts only: no database is opened. Cheap enough to poll."""
    state = config.branch
    warehouse = branch_warehouse_path(config)
    try:
        base_path = base_warehouse_path(config)
    except Exception:
        base_path = None
    return {
        "enabled": bool(config.branches.enabled),
        "active": bool(state.active),
        "reason": state.reason,
        "branch": state.git_branch,
        "detached": state.detached,
        "in_repo": state.in_repo,
        "source": state.source,
        "main_branches": list(state.main_branches),
        "slug": state.slug,
        "warehouse": {
            "path": state.path,
            "exists": bool(warehouse and warehouse.exists()),
            "size_bytes": warehouse.stat().st_size if warehouse and warehouse.exists() else 0,
        } if state.active else None,
        "base": {
            "name": state.base,
            "label": state.base_label,
            "path": state.base_path,
            "exists": bool(base_path and base_path.exists()),
        },
    }


def branch_status(conn, config: Any) -> dict:
    """What the branch warehouse holds, what it defers, and what is stale.

    ``conn`` is a connection whose current database is the branch warehouse
    (read-only is fine), or any empty connection when the warehouse does not
    exist yet. The base is attached read-only for the duration of the call.
    """
    from havn.engine.defer import DeferError, catalog_objects, target_lockable
    from havn.engine.transform import discover_all_models

    out = branch_summary(config)
    if not config.branch.active:
        return out

    models = discover_all_models(config.project_dir, config)
    by_name = {m.full_name: m for m in models}
    local_catalog = catalog_objects(conn)
    local_state = {k: v for k, v in _model_state(conn).items() if k in by_name}
    materialized = sorted(
        n for n in local_state
        if n in local_catalog or local_state[n].get("materialized_as") == "ephemeral"
    )

    base_info = out["base"]
    plan: list[str] | None = None
    stale: dict[str, list[str]] = {}
    base_built_at = None
    if base_info["exists"]:
        ok, why = target_lockable(base_warehouse_path(config))
        base_info["readable"] = ok
        base_info["reason"] = why
        try:
            with base_attached(conn, config) as alias:
                plan = plan_branch(conn, config, alias, models)
                base_state = _model_state(conn, alias)
                base_built_at = max(
                    (v["built_at"] for v in base_state.values() if v.get("built_at")),
                    default=None,
                )
                last_load = _last_source_load(conn, alias)
                stale = _stale_models(
                    materialized, local_state, base_state, by_name, last_load,
                )
                base_info["readable"] = True
                base_info["reason"] = None
        except (DeferError, BranchError) as e:
            base_info["readable"] = False
            base_info["reason"] = str(e)
    else:
        base_info["readable"] = False
        base_info["reason"] = "the base warehouse does not exist"

    plan_set = set(plan or [])
    from havn.engine.transform.discovery import _compute_upstream_hash, _has_changed, build_dag

    # Planned models whose branch build is missing or behind the branch's SQL.
    needs_build: list[str] = []
    if plan is not None:
        ordered = build_dag(models)
        model_map = {m.full_name: m for m in ordered}
        for m in ordered:
            m.upstream_hash = _compute_upstream_hash(m, model_map)
        for name in plan:
            try:
                changed = _has_changed(conn, model_map[name])
            except Exception:
                changed = True
            if changed or (name not in local_catalog and by_name[name].materialized != "ephemeral"):
                needs_build.append(name)

    out["models"] = {
        "local": [
            {
                "name": n,
                "built_at": str(local_state[n]["built_at"]) if local_state[n].get("built_at") else None,
                "row_count": local_state[n].get("row_count"),
                "materialized_as": local_state[n].get("materialized_as"),
                "stale": n in stale,
                "stale_reasons": stale.get(n, []),
            }
            for n in materialized
        ],
        "modified": plan,
        "needs_build": needs_build if plan is not None else None,
        "prunable": sorted(n for n in materialized if plan is not None and n not in plan_set),
        "deferred": sorted(n for n in by_name if n not in set(materialized)),
        "total": len(models),
    }
    out["stale"] = bool(stale)
    out["base"]["built_at"] = str(base_built_at) if base_built_at else None
    out["up_to_date"] = plan is not None and not needs_build and not stale and not out["models"]["prunable"]
    return out


def _stale_models(
    local: list[str],
    local_state: dict[str, dict],
    base_state: dict[str, dict],
    by_name: dict[str, Any],
    last_source_load: Any,
) -> dict[str, list[str]]:
    """Local models the base has moved under since they were built.

    A local model is stale when one of its upstreams that it reads from the
    base -- found by walking ``depends_on`` until a local model stops the walk
    -- was rebuilt in the base after the local model was built, or when it
    reads a raw source and the base loaded new source data after it.
    """
    local_set = set(local)
    out: dict[str, list[str]] = {}
    for name in local:
        built = local_state.get(name, {}).get("built_at")
        if built is None:
            continue
        reasons: list[str] = []
        seen: set[str] = set()
        work = list(getattr(by_name.get(name), "depends_on", None) or [])
        reads_source = False
        while work:
            dep = work.pop().lower()
            if dep in seen:
                continue
            seen.add(dep)
            if dep in local_set:
                continue
            if dep in by_name:
                base_built = base_state.get(dep, {}).get("built_at")
                if base_built is not None and _later(base_built, built):
                    reasons.append(f"{dep} was rebuilt in the base at {base_built}")
                work.extend(getattr(by_name[dep], "depends_on", None) or [])
            else:
                reads_source = True
        if reads_source and last_source_load is not None and _later(last_source_load, built):
            reasons.append(f"the base loaded new source data at {last_source_load}")
        if reasons:
            out[name] = sorted(reasons)
    return out


def _later(a: Any, b: Any) -> bool:
    try:
        return a > b
    except TypeError:
        return str(a) > str(b)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _drop_local(conn, name: str) -> None:
    from havn.engine.utils import validate_identifier

    schema, _, table = name.partition(".")
    validate_identifier(schema, "schema")
    validate_identifier(table, "table")
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()
    if row:
        kind = "VIEW" if row[0] == "VIEW" else "TABLE"
        conn.execute(f'DROP {kind} IF EXISTS "{schema}"."{table}"')
    for meta in ("model_state", "model_columns", "batch_state", "model_blocked"):
        try:
            conn.execute(f"DELETE FROM _havn.{meta} WHERE model_path = ?", [name])
        except Exception as e:
            logger.debug("prune %s: %s skipped: %s", name, meta, e)


def build_branch(
    conn,
    config: Any,
    *,
    force: bool = False,
    prune: bool = True,
    dry_run: bool = False,
    snapshot: bool = False,
    on_message: Callable[[str], None] | None = None,
) -> dict:
    """Build what the branch changed into the branch warehouse.

    ``conn`` must be a read-write connection to the branch warehouse. The
    plan is ``state:modified+`` against the base; those models are built
    with defer to the base, so every upstream the branch did not touch is
    read from there. With ``prune`` (the default), branch copies of models
    that no longer differ from the base -- a change that was reverted -- are
    dropped first, so they read through to the base again instead of
    shadowing it with old data.

    Returns ``{branch, base, plan, pruned, results, built, failed}``.
    """
    from havn.engine.database import ensure_meta_table
    from havn.engine.defer import DeferSpec, catalog_objects
    from havn.engine.transform import discover_all_models, run_transform

    require_active(config)
    base_path = require_base(config)
    project_dir = Path(config.project_dir)
    say = on_message or (lambda _m: None)

    if not dry_run:
        ensure_meta_table(conn)
    models = discover_all_models(project_dir, config)
    by_name = {m.full_name for m in models}

    with base_attached(conn, config) as alias:
        plan = plan_branch(conn, config, alias, models)
        local_state = _model_state(conn)
        local_catalog = catalog_objects(conn)
        prunable = sorted(
            n for n in local_state
            if n in by_name and n not in set(plan)
            and (n in local_catalog or local_state[n].get("materialized_as") == "ephemeral")
        )
        result: dict[str, Any] = {
            "branch": config.branch.git_branch,
            "base": config.branch.base_label,
            "base_path": str(base_path),
            "plan": plan,
            "pruned": [],
            "results": {},
            "built": [],
            "failed": {},
            "dry_run": dry_run,
        }
        if dry_run:
            result["prunable"] = prunable
            return result

        if prune and prunable:
            for name in prunable:
                _drop_local(conn, name)
            result["pruned"] = prunable
            say(f"pruned {len(prunable)} model(s) that match the base again: {', '.join(prunable)}")

        if not plan:
            say(f"nothing differs from '{config.branch.base_label}'; the branch reads everything from it")
            return result

        say(f"building {len(plan)} model(s) that differ from '{config.branch.base_label}'")
        spec = DeferSpec(
            target=config.branch.base_label,
            path=base_path,
            snapshot=snapshot,
            project_dir=project_dir,
        )
        results = run_transform(
            conn, project_dir / "transform", targets=plan, force=force,
            project_dir=project_dir, db_config=config.database, defer=spec,
        ) or {}

    from havn.engine.deploy import _OK_STATUSES

    result["results"] = {n: results.get(n) for n in plan}
    result["built"] = [n for n, s in result["results"].items() if s == "built"]
    result["failed"] = {
        n: s for n, s in result["results"].items() if s not in _OK_STATUSES
    }
    return result


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _primary_key(model: Any, config: Any) -> list[str] | None:
    from havn.engine.diff import get_primary_key

    pk = get_primary_key(model.sql, config, model.full_name)
    if pk:
        return pk
    key = getattr(model, "unique_key", None)
    if key:
        cols = [c.strip() for c in str(key).split(",") if c.strip()]
        return cols or None
    return None


def _count(conn, ref: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {ref}").fetchone()[0])
    except Exception:
        return 0


def diff_branch(
    conn,
    config: Any,
    *,
    models: list[str] | None = None,
    full: bool = False,
) -> dict:
    """Schema and row-level diff of the branch's models against the base.

    Every model the branch differs in (the plan) or has built is compared,
    branch copy against base copy, with :func:`havn.engine.diff.diff_model`.
    Status per model: ``added`` (no base copy), ``changed``, ``unchanged``,
    ``removed`` (the base has it, the branch no longer defines it) or
    ``not_built`` (planned but not in the branch warehouse yet: run
    ``havn branch build``). Models that are neither planned nor built read
    from the base and are identical by construction, so they are not listed.

    ``models`` narrows the report to those names.
    """
    from havn.engine.defer import catalog_objects
    from havn.engine.diff import diff_model
    from havn.engine.transform import build_dag, discover_all_models

    require_active(config)
    project_dir = Path(config.project_dir)
    all_models = discover_all_models(project_dir, config)
    by_name = {m.full_name: m for m in all_models}
    head = read_git_head(project_dir)

    entries: list[dict] = []
    with base_attached(conn, config) as alias:
        plan = plan_branch(conn, config, alias, all_models)
        local_catalog = catalog_objects(conn)
        base_catalog = catalog_objects(conn, alias)
        local_state = _model_state(conn)
        base_state = _model_state(conn, alias)
        current_db = conn.execute("SELECT current_database()").fetchone()[0]

        wanted = set(plan) | {n for n in local_state if n in by_name and n in local_catalog}
        if models:
            asked = {m.lower() for m in models}
            unknown = sorted(asked - set(by_name) - set(base_state))
            if unknown:
                raise BranchError(f"Unknown model(s): {', '.join(unknown)}")
            wanted = asked

        for model in build_dag([by_name[n] for n in wanted if n in by_name]):
            name = model.full_name
            if model.materialized == "ephemeral":
                continue
            entry: dict[str, Any] = {
                "model": name,
                "status": "unchanged",
                "planned": name in plan,
                "materialized": model.materialized,
                "primary_key": None,
                "before": None,
                "after": None,
                "added": 0,
                "removed": 0,
                "modified": 0,
                "schema_changes": [],
                "sample_added": [],
                "sample_removed": [],
                "sample_modified": [],
                "error": None,
            }
            if name not in local_catalog:
                entry["status"] = "not_built"
                entry["before"] = _count(conn, f'{alias}."{model.schema}"."{model.name}"') if name in base_catalog else None
                entries.append(entry)
                continue
            pk = _primary_key(model, config)
            entry["primary_key"] = pk
            res = diff_model(
                conn,
                f'SELECT * FROM "{current_db}"."{model.schema}"."{model.name}"',
                model.schema,
                model.name,
                primary_key=pk,
                full=full,
                target_catalog=alias,
            )
            if res.error:
                entry["status"] = "error"
                entry["error"] = res.error
                entries.append(entry)
                continue
            entry.update({
                "before": None if res.is_new else res.total_before,
                "after": res.total_after,
                "added": res.added,
                "removed": res.removed,
                "modified": res.modified,
                "schema_changes": [
                    {
                        "column": sc.column,
                        "change": sc.change_type,
                        "old_type": sc.old_type,
                        "new_type": sc.new_type,
                    }
                    for sc in res.schema_changes
                ],
                "sample_added": res.sample_added,
                "sample_removed": res.sample_removed,
                "sample_modified": res.sample_modified,
            })
            if res.is_new:
                entry["status"] = "added"
            elif res.added or res.removed or res.modified or res.schema_changes:
                entry["status"] = "changed"
            entries.append(entry)

        # Models the base built that this branch no longer defines.
        for name in sorted(base_state):
            if name in by_name or name not in base_catalog or name in local_catalog:
                continue
            if models and name not in {m.lower() for m in models}:
                continue
            schema, _, table = name.partition(".")
            entries.append({
                "model": name,
                "status": "removed",
                "planned": False,
                "materialized": base_state[name].get("materialized_as"),
                "primary_key": None,
                "before": _count(conn, f'{alias}."{schema}"."{table}"'),
                "after": None,
                "added": 0,
                "removed": 0,
                "modified": 0,
                "schema_changes": [],
                "sample_added": [],
                "sample_removed": [],
                "sample_modified": [],
                "error": None,
            })

    counts: dict[str, int] = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {
        "branch": config.branch.git_branch,
        "base": config.branch.base_label,
        "base_path": config.branch.base_path,
        "commit": head.sha,
        "generated_at": _now(),
        "models": entries,
        "summary": {
            "added": counts.get("added", 0),
            "changed": counts.get("changed", 0),
            "unchanged": counts.get("unchanged", 0),
            "removed": counts.get("removed", 0),
            "not_built": counts.get("not_built", 0),
            "error": counts.get("error", 0),
        },
    }


def diff_has_changes(report: dict) -> bool:
    """Whether a diff report shows any data or schema change (or a problem)."""
    s = report.get("summary", {})
    return any(s.get(k) for k in ("added", "changed", "removed", "not_built", "error"))


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _cell(value: Any, limit: int = 60) -> str:
    if value is None:
        return "∅"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rows_table(rows: list[dict], max_rows: int) -> list[str]:
    if not rows:
        return []
    cols = list(rows[0].keys())
    lines = [
        "| " + " | ".join(_cell(c, 40) for c in cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for row in rows[:max_rows]:
        lines.append("| " + " | ".join(_cell(row.get(c)) for c in cols) + " |")
    return lines


def _num(n: Any) -> str:
    return "–" if n is None else f"{int(n):,}"


def _schema_label(changes: list[dict]) -> str:
    if not changes:
        return "–"
    adds = sum(1 for c in changes if c["change"] == "added")
    drops = sum(1 for c in changes if c["change"] == "removed")
    types = sum(1 for c in changes if c["change"] == "type_changed")
    parts = []
    if adds:
        parts.append(f"+{adds} col")
    if drops:
        parts.append(f"−{drops} col")
    if types:
        parts.append(f"~{types} type")
    return ", ".join(parts)


_STATUS_LABEL = {
    "added": "new",
    "changed": "changed",
    "unchanged": "unchanged",
    "removed": "removed",
    "not_built": "not built",
    "error": "error",
}


def format_markdown(report: dict, *, max_samples: int = 5) -> str:
    """The data diff as a GitHub pull-request comment.

    Starts with :data:`MARKDOWN_MARKER` so CI can find and update its own
    comment instead of adding one per push. Kept under GitHub's comment size
    limit by dropping sample rows (then shrinking them) when it would not fit.
    """
    for samples in (max_samples, 2, 0):
        text = _format_markdown(report, max_samples=samples)
        if len(text) <= MARKDOWN_LIMIT:
            return text
    return text[:MARKDOWN_LIMIT] + "\n\n_(truncated)_\n"


def _format_markdown(report: dict, *, max_samples: int) -> str:
    branch = report.get("branch") or "?"
    base = report.get("base") or "base"
    lines = [MARKDOWN_MARKER, f"## havn data diff: `{branch}` vs `{base}`", ""]
    s = report.get("summary", {})
    entries = report.get("models", [])
    shown = [e for e in entries if e["status"] != "unchanged"]
    unchanged = [e for e in entries if e["status"] == "unchanged"]
    commit = (report.get("commit") or "")[:7]

    facts = []
    for key, label in (
        ("changed", "changed"), ("added", "new"), ("removed", "removed"),
        ("not_built", "not built"), ("error", "failed to diff"),
    ):
        if s.get(key):
            facts.append(f"{s[key]} {label}")
    if s.get("unchanged"):
        facts.append(f"{s['unchanged']} rebuilt with identical data")
    summary = ", ".join(facts) if facts else "no models differ from the base"
    lines.append(summary + (f" · commit `{commit}`" if commit else ""))
    lines.append("")

    if not shown:
        lines.append("No data changes: every model this branch builds matches the base.")
    else:
        lines.append("| Model | Status | Rows (base → branch) | Added | Removed | Modified | Schema |")
        lines.append("|---|---|---|---:|---:|---:|---|")
        for e in shown:
            rows = f"{_num(e.get('before'))} → {_num(e.get('after'))}"
            lines.append(
                f"| `{e['model']}` | {_STATUS_LABEL.get(e['status'], e['status'])} | {rows} "
                f"| {('+' + format(e['added'], ',')) if e.get('added') else '0'} "
                f"| {('−' + format(e['removed'], ',')) if e.get('removed') else '0'} "
                f"| {('~' + format(e['modified'], ',')) if e.get('modified') else '0'} "
                f"| {_schema_label(e.get('schema_changes') or [])} |"
            )

        for e in shown:
            details: list[str] = []
            if e["status"] == "error":
                details.append(f"Diff failed: `{_cell(e.get('error'), 300)}`")
            elif e["status"] == "not_built":
                details.append("Planned for this branch but not built yet. Run `havn branch build`.")
            elif e["status"] == "removed":
                details.append(f"No longer defined on this branch; the base holds {_num(e.get('before'))} rows.")
            for sc in e.get("schema_changes") or []:
                if sc["change"] == "added":
                    details.append(f"- added column `{sc['column']}` ({sc.get('new_type')})")
                elif sc["change"] == "removed":
                    details.append(f"- removed column `{sc['column']}` ({sc.get('old_type')})")
                else:
                    details.append(
                        f"- `{sc['column']}`: {sc.get('old_type')} → {sc.get('new_type')}"
                    )
            samples: list[str] = []
            if max_samples:
                for key, label, total in (
                    ("sample_added", "Added", e.get("added")),
                    ("sample_removed", "Removed", e.get("removed")),
                    ("sample_modified", "Modified (branch values)", e.get("modified")),
                ):
                    rows = e.get(key) or []
                    if not rows:
                        continue
                    n = min(len(rows), max_samples)
                    noun = "row" if total == 1 else "rows"
                    samples.append(f"**{label}** ({_num(total)} {noun}, showing {n})")
                    samples.append("")
                    samples.extend(_rows_table(rows, max_samples))
                    samples.append("")
            if not details and not samples:
                continue
            lines.extend(["", f"### `{e['model']}`", ""])
            lines.extend(details)
            if samples:
                if details:
                    lines.append("")
                key_note = f" (key: {', '.join(e['primary_key'])})" if e.get("primary_key") else ""
                lines.append(f"<details><summary>Sample rows{key_note}</summary>")
                lines.append("")
                lines.extend(samples)
                lines.append("</details>")

    if unchanged:
        names = ", ".join(f"`{e['model']}`" for e in unchanged[:20])
        more = f" and {len(unchanged) - 20} more" if len(unchanged) > 20 else ""
        lines.extend(["", f"<sub>Rebuilt with identical data: {names}{more}.</sub>"])
    lines.extend([
        "",
        f"<sub>Built by `havn branch build` into a branch warehouse; models it did not "
        f"change were read from `{base}`.</sub>",
        "",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# List / remove / clean
# ---------------------------------------------------------------------------


def _template(config: Any) -> str:
    return (config.branches.path or ".havn/branches/{branch}.duckdb").replace("\\", "/")


def _template_regex(template: str) -> re.Pattern:
    escaped = re.escape(template).replace(re.escape("{branch}"), r"(?P<slug>[^/]+)")
    return re.compile(rf"^{escaped}$", re.IGNORECASE if os.name == "nt" else 0)


def _git_branches(project_dir: Path) -> tuple[set[str], set[str], bool]:
    """``(local branches, branches merged into a main branch, ok)``."""
    from havn.engine.git import _run_git, is_git_repo

    if not is_git_repo(project_dir):
        return set(), set(), False
    res = _run_git(project_dir, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if res.returncode != 0:
        return set(), set(), False
    local = {line.strip() for line in res.stdout.splitlines() if line.strip()}
    merged: set[str] = set()
    head = read_git_head(project_dir)
    for main in detect_main_branches(head):
        if main not in local:
            continue
        m = _run_git(project_dir, "branch", "--merged", main, "--format=%(refname:short)")
        if m.returncode == 0:
            merged |= {line.strip() for line in m.stdout.splitlines() if line.strip()}
    return local, merged, True


def list_branch_warehouses(config: Any) -> list[dict]:
    """Every branch warehouse on disk, with the git state of its branch.

    ``git`` is ``"current"``, ``"exists"``, ``"merged"`` (merged into the main
    branch), ``"gone"`` (no such local branch any more) or ``"unknown"`` (not
    a git repository, or a file whose branch cannot be named).
    """
    project_dir = Path(config.project_dir)
    template = _template(config)
    pattern = _template_regex(template)
    glob = template.replace("{branch}", "*")
    tpl_path = Path(glob)
    if tpl_path.is_absolute():
        root = Path(tpl_path.anchor)
        candidates = root.glob(str(tpl_path.relative_to(root)).replace("\\", "/"))
    else:
        root = project_dir
        candidates = project_dir.glob(glob)

    local, merged, ok = _git_branches(project_dir)
    slug_to_branch = {branch_slug(b): b for b in local}
    main = set(config.branch.main_branches or [])
    current = config.branch.slug if config.branch.active else None

    out = []
    for path in sorted(candidates):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix() if not tpl_path.is_absolute() else path.as_posix()
        except ValueError:
            continue
        match = pattern.match(rel if not tpl_path.is_absolute() else path.as_posix())
        if not match:
            continue
        slug = match.group("slug")
        record = _read_record(path)
        name = record.get("branch") or slug_to_branch.get(slug)
        if slug == current:
            git = "current"
        elif not ok or not name:
            git = "unknown"
        elif name not in local:
            git = "gone"
        elif name in merged and name not in main:
            git = "merged"
        else:
            git = "exists"
        stat = path.stat()
        out.append({
            "slug": slug,
            "branch": name,
            "path": rel,
            "size_bytes": stat.st_size,
            "modified_at": datetime.datetime.fromtimestamp(
                stat.st_mtime, datetime.timezone.utc
            ).isoformat(timespec="seconds"),
            "created_at": record.get("created_at"),
            "git": git,
            "current": slug == current,
        })
    return out


def remove_branch_warehouse(config: Any, path: Path | str) -> None:
    """Delete one branch warehouse (with its WAL and record).

    Refuses anything that is not a branch warehouse by the configured
    template, and anything that is the base or an environment's warehouse,
    whatever the template says. Raises OSError when the file is in use (an
    open ``havn serve`` on Windows).
    """
    project_dir = Path(config.project_dir)
    target = _abs(project_dir, path).resolve()
    if target in _protected_paths(config):
        raise BranchError(f"Refusing to delete {target}: it is not a branch warehouse")
    template = _template(config)
    pattern = _template_regex(template)
    try:
        rel = target.relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        rel = target.as_posix()
    if not pattern.match(rel) and not pattern.match(target.as_posix()):
        raise BranchError(f"Refusing to delete {target}: it does not match branches.path ({template})")
    for p in (target, Path(str(target) + ".wal")):
        if p.exists():
            p.unlink()
    record = _record_path(target)
    if record.exists():
        record.unlink()


def clean_branches(config: Any, *, dry_run: bool = False, include_unknown: bool = False) -> dict:
    """Delete branch warehouses whose branch was merged or deleted.

    The current branch's warehouse is never removed. ``include_unknown`` also
    removes files whose branch cannot be named.
    """
    removed, kept, errors = [], [], {}
    for entry in list_branch_warehouses(config):
        doomed = entry["git"] in ("merged", "gone") or (include_unknown and entry["git"] == "unknown")
        if entry["current"] or not doomed:
            kept.append(entry)
            continue
        if dry_run:
            removed.append(entry)
            continue
        try:
            remove_branch_warehouse(config, entry["path"])
            removed.append(entry)
        except (OSError, BranchError) as e:
            errors[entry["path"]] = str(e)
    return {"removed": removed, "kept": kept, "errors": errors, "dry_run": dry_run}

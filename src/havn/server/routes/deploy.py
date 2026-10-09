"""Deploy a git ref to an environment's warehouse (see havn.engine.deploy)."""

from __future__ import annotations

import duckdb

import logging
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from havn.engine.write_queue import cursor_for
from havn.server.deps import (
    DbConn,
    DbConnReadOnly,
    _get_config,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter(tags=["deploy"])

# The environment name used when project.yml declares none: the project's
# one warehouse.
DEFAULT_ENV = "default"


class DeployRequest(BaseModel):
    env: str = Field(..., min_length=1, max_length=100)
    ref: str = Field(..., min_length=1, max_length=250)
    pr_id: str | None = Field(default=None, max_length=100)


def _is_production(name: str) -> bool:
    import re

    return bool(re.match(r"^(prod|production|prd|live)([-_].*)?$", name or "", re.I))


def _default_ref(project_dir: Path) -> str:
    from havn.engine.git import _run_git

    for ref in ("main", "master"):
        if _run_git(project_dir, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0:
            return ref
    res = _run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
    return res.stdout.strip() if res.returncode == 0 else "main"


def _target(env: str) -> tuple[object, Path, bool]:
    """(env config, warehouse path, whether it is the server's own warehouse)."""
    from havn.config import load_project

    project_dir = _get_project_dir()
    active = _get_config()
    if not active.environments:
        if env != DEFAULT_ENV:
            raise HTTPException(404, f"Unknown environment '{env}'; this project declares none")
        cfg = active
    else:
        if env not in active.environments:
            known = ", ".join(active.environments)
            raise HTTPException(404, f"Unknown environment '{env}' (defined: {known})")
        cfg = load_project(project_dir, env=env)
    path = Path(cfg.database.path)
    path = path if path.is_absolute() else project_dir / path
    active_path = Path(active.database.path)
    active_path = active_path if active_path.is_absolute() else project_dir / active_path
    return cfg, path, path.resolve() == active_path.resolve()


@router.get("/api/deploy/targets")
def deploy_targets(request: Request) -> dict:
    """Environments a ref can be deployed to, and the ref to deploy by default."""
    _require_permission(request, "read")
    config = _get_config()
    project_dir = _get_project_dir()
    names = list(config.environments) or [DEFAULT_ENV]
    envs = []
    for name in names:
        try:
            _, path, is_active = _target(name)
        except HTTPException:
            continue
        envs.append({
            "name": name,
            "active": is_active,
            "production": _is_production(name),
            "database": path.relative_to(project_dir).as_posix() if path.is_relative_to(project_dir) else str(path),
            "exists": path.exists(),
        })
    return {"environments": envs, "default_ref": _default_ref(project_dir)}


@router.get("/api/deploy/plan")
def deploy_plan(
    request: Request,
    conn: DbConnReadOnly,
    env: str = Query(..., min_length=1, max_length=100),
    ref: str = Query(..., min_length=1, max_length=250),
) -> dict:
    """The models deploying ``ref`` to ``env`` would rebuild. Changes nothing."""
    _require_permission(request, "read")
    from havn.engine.database import open_warehouse
    from havn.engine.deploy import DeployError, plan_deploy

    project_dir = _get_project_dir()
    cfg, path, is_active = _target(env)
    try:
        if is_active:
            plan = plan_deploy(project_dir, ref, conn)
        elif not path.exists():
            plan = plan_deploy(project_dir, ref, None)
        else:
            target = open_warehouse(cfg, project_dir, read_only=True)
            try:
                plan = plan_deploy(project_dir, ref, target)
            finally:
                target.close()
    except DeployError as e:
        raise HTTPException(400, str(e))
    except duckdb.Error as e:
        # The target is open elsewhere: a deploy to it is running in this
        # process, or another process holds it (DuckDB takes an exclusive
        # lock on Windows). That is a state to retry, not a server error.
        raise HTTPException(
            409, f"The {env} warehouse is busy, probably with a deploy in progress. Try again shortly. ({e})"
        )
    return {"env": env, **plan}


def _check_production_ref(project_dir: Path, env: str, ref: str, user: dict) -> None:
    """Only reviewed code reaches production, unless an admin deploys it.

    Deploy took any ref, so an editor could put an unmerged, unreviewed
    branch straight into prod and bypass the change-review gate entirely.
    For a production environment the ref's commit must already be on the
    base branch -- that is, merged through review. Admins keep the escape
    hatch (hotfixes, rollbacks to an old tag on main still pass anyway).
    With sign-in off everyone acts as an admin, so nothing changes there.
    """
    from havn.engine.auth import has_permission
    from havn.engine.git import _run_git

    if not _is_production(env) or has_permission(user.get("role", ""), "manage_users"):
        return
    base = _default_ref(project_dir)
    commit = _run_git(project_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if commit.returncode != 0:
        raise HTTPException(400, f"'{ref}' is not a branch, tag or commit in this repository")
    reachable = _run_git(project_dir, "merge-base", "--is-ancestor", commit.stdout.strip(), base)
    if reachable.returncode != 0:
        raise HTTPException(
            403,
            f"Only code merged into {base} can be deployed to {env}. "
            f"Merge '{ref}' through a reviewed change first, or ask an admin.",
        )


@router.post("/api/deploys")
def start_deploy(req: DeployRequest, request: Request, conn: DbConn) -> dict:
    """Deploy ``ref`` to ``env`` in the background; poll GET /api/deploys/{id}."""
    user = _require_permission(request, "execute")
    from havn.engine.deploy import _save, new_record, run_deploy
    from havn.server.routes.pr import _actor

    project_dir = _get_project_dir()
    cfg, path, is_active = _target(req.env)
    _check_production_ref(project_dir, req.env, req.ref, user)
    record = new_record(req.env, req.ref, pr_id=req.pr_id, deployed_by=_actor(user, None))
    _save(conn, record)

    def _run():
        from havn.engine.database import open_warehouse
        from havn.server.deps import _get_shared_conn

        record_cur = None
        target = None
        try:
            record_cur = cursor_for(_get_shared_conn())
            if is_active:
                run_deploy(project_dir, record, record_cur, db_path=str(path),
                           restore_macros_from=project_dir)
            else:
                target = open_warehouse(cfg, project_dir)
                # The deploy closes the target before it saves the final
                # status (see run_deploy's close_target); the close in the
                # finally below is then a no-op backstop for early failures.
                run_deploy(project_dir, record, target, record_conn=record_cur,
                           db_path=str(path), close_target=True)
        except Exception as e:
            logger.error("deploy %s failed to start: %s", record["id"], e)
            if record_cur is not None:
                record.update(status="error", error=str(e))
                try:
                    _save(record_cur, record)
                except Exception:
                    pass
        finally:
            for c in (target, record_cur):
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass

    threading.Thread(target=_run, daemon=True).start()
    return record


@router.get("/api/deploys")
def get_deploys(
    request: Request,
    conn: DbConnReadOnly,
    pr_id: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict]:
    _require_permission(request, "read")
    from havn.engine.deploy import list_deploys

    return list_deploys(conn, limit=limit, pr_id=pr_id)


@router.get("/api/deploys/{deploy_id}")
def get_deploy(deploy_id: str, request: Request, conn: DbConnReadOnly) -> dict:
    _require_permission(request, "read")
    from havn.engine.deploy import list_deploys

    found = list_deploys(conn, limit=1, deploy_id=deploy_id)
    if not found:
        raise HTTPException(404, f"Deploy '{deploy_id}' not found")
    return found[0]

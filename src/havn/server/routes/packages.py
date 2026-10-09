"""Packages API: list installed packages and install the declared ones."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from havn.server.deps import _get_config, _get_project_dir, _require_permission

router = APIRouter()


class InstallPackagesRequest(BaseModel):
    upgrade: bool = False


def _counts(root) -> tuple[int, int]:
    from havn.engine.macros import _discover_all_python_macros, _discover_sql_macros
    from havn.engine.transform import discover_package_models

    try:
        models = len(discover_package_models(root))
    except Exception:
        models = 0
    scalars, tables = _discover_all_python_macros(root.macros_dir, package=root.name)
    macros = len(scalars) + len(tables) + len(_discover_sql_macros(root.macros_dir))
    return models, macros


def _listing(project_dir) -> list[dict]:
    from havn.engine.packages import package_roots, read_lock

    lock = read_lock(project_dir)
    out: list[dict] = []
    for root in package_roots(project_dir):
        entry = lock.get(root.name)
        models, macros = _counts(root)
        out.append({
            "name": root.name,
            "source": entry.source if entry else "",
            "git": entry.git if entry else "",
            "rev": entry.rev if entry else "",
            "commit": entry.commit if entry else "",
            "path": entry.path if entry else "",
            "version": root.manifest.version,
            "description": root.manifest.description,
            "models": models,
            "macros": macros,
        })
    return out


@router.get("/api/packages")
def list_packages_endpoint(request: Request) -> dict:
    """Installed packages, plus the ones declared but not yet installed."""
    _require_permission(request, "read")

    project_dir = _get_project_dir()
    installed = _listing(project_dir)
    names = {p["name"] for p in installed}
    config = _get_config()
    declared = [
        {"name": p.name, "git": p.git or "", "rev": p.rev or "", "path": p.path or ""}
        for p in (config.packages or [])
    ]
    return {
        "packages": installed,
        "declared": declared,
        "missing": [d["name"] for d in declared if d["name"] not in names],
    }


@router.post("/api/packages/install")
def install_packages_endpoint(request: Request, req: InstallPackagesRequest) -> dict:
    """Install every package declared in project.yml and rewrite the lock."""
    _require_permission(request, "execute")

    from havn.engine.packages import install_packages

    project_dir = _get_project_dir()
    config = _get_config()
    if not config.packages:
        raise HTTPException(400, "No packages: block in project.yml")

    results = install_packages(project_dir, config, upgrade=req.upgrade)
    # Package models and macros changed on disk, so the server's cached
    # discovery has to be dropped or the DAG keeps showing the old set.
    from havn.server.deps import _model_cache

    _model_cache["models"] = None

    return {
        "results": [
            {
                "name": r.name,
                "source": r.source,
                "rev": r.ref,
                "commit": r.commit,
                "status": r.status,
                "message": r.message,
                "warnings": r.warnings,
            }
            for r in results
        ],
        "packages": _listing(project_dir),
    }

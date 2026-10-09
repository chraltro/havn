"""Wiring for the surfaces outside the server: CLI and MCP.

``havn serve`` holds the warehouse lock, so when it runs, the CLI and the MCP
server send the question to ``POST /api/ask`` and let the server answer with
its own connection and governance. Without a server they open the warehouse
read-only themselves (:func:`local_context`).
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import yaml

from havn.textio import read_project_text

#: The identity a local (no-server) CLI or MCP read runs as. Whoever runs the
#: CLI can open the warehouse file directly, so masking would not protect
#: anything from them; the governed path still enforces read-only SQL.
LOCAL_USER = {"username": "local", "role": "admin"}


class ServerAskError(RuntimeError):
    """A running ``havn serve`` answered with an error."""


def serve_address(project_dir: Path) -> tuple[str, int] | None:
    """(host, port) of a live ``havn serve`` for this project, if any."""
    info_path = Path(project_dir) / ".havn" / "serve.json"
    if not info_path.exists():
        return None
    try:
        info = json.loads(read_project_text(info_path))
        host = info.get("host", "127.0.0.1")
        port = int(info.get("port", 3000))
        pid = info.get("pid")
    except Exception:
        return None
    if pid is not None:
        from havn.cli.query import _pid_alive

        if not _pid_alive(int(pid)):
            return None
    return host, port


def call_server(project_dir: Path, method: str, path: str, body: dict | None = None,
                timeout: float = 600) -> dict | None:
    """JSON request to a running ``havn serve``; None when none is reachable."""
    address = serve_address(project_dir)
    if address is None:
        return None
    host, port = address
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:
            detail = str(e)
        raise ServerAskError(f"havn serve at {host}:{port}: {detail}") from None
    except (urllib.error.URLError, ConnectionError, OSError):
        return None


@contextlib.contextmanager
def local_context(project_dir: Path, *, env: str | None = None, provider=None) -> Iterator:
    """An AskContext on a read-only connection opened by this process."""
    from havn.config import load_project
    from havn.engine.ai.ask import AskContext
    from havn.engine.ai.config import parse_ai_config
    from havn.engine.ai.providers import provider_from_config
    from havn.engine.backends import create_backend
    from havn.engine.database import open_warehouse
    from havn.engine.transform import discover_all_models

    project_dir = Path(project_dir)
    config = load_project(project_dir, env=env)
    ai = parse_ai_config((config._raw or {}).get("ai"))
    if provider is None:
        provider = provider_from_config(ai)
    try:
        models = discover_all_models(project_dir, config)
    except Exception:
        models = []
    conn = None
    backend = create_backend(config.database, project_dir=project_dir)
    if backend.exists():
        conn = open_warehouse(config, project_dir, read_only=True)
    try:
        yield AskContext(
            project_dir=project_dir,
            conn=conn,
            user=dict(LOCAL_USER),
            provider=provider,
            ai=ai,
            project_config=config,
            models=models,
        )
    finally:
        if conn is not None:
            conn.close()


def accept_suggested_metric(project_dir: Path, definition: dict, path: str | None = None) -> str:
    """Write a suggested metric definition to ``metrics/``. Returns its path.

    The definition is re-validated with the semantic layer's own parser, and
    an existing file or metric name is never overwritten.
    """
    from havn.engine.semantic import SemanticError, _parse_metric, load_metrics

    project_dir = Path(project_dir)
    try:
        metric = _parse_metric(definition, source_path="suggestion")
    except SemanticError as e:
        raise ValueError(str(e))
    existing, _errors = load_metrics(project_dir)
    if metric.name in existing:
        raise ValueError(f"a metric named {metric.name!r} already exists")
    rel = path or f"metrics/{metric.name}.yml"
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.parts[:1] != ("metrics",) \
            or rel_path.suffix not in (".yml", ".yaml") or len(rel_path.parts) != 2:
        raise ValueError("the metric file must be metrics/<name>.yml")
    target = project_dir / rel_path
    if target.exists():
        raise ValueError(f"{rel_path.as_posix()} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in definition.items() if v not in (None, [], "")}
    clean["name"] = metric.name
    target.write_text(
        yaml.safe_dump({"metrics": [clean]}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return rel_path.as_posix()

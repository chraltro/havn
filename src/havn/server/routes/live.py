"""Live models HTTP surface, and the live runner's lifetime inside ``havn serve``.

- ``GET  /api/live/status``                    — live models, lag, sources, runner state.
- ``GET  /api/live/events``                    — SSE: advances, refreshes, state changes.
- ``POST /api/live/start`` / ``/api/live/stop`` — start or stop the runner.
- ``POST /api/live/models/{model}/pause``      — stop refreshing one model.
- ``POST /api/live/models/{model}/resume``     — start again (clears backoff).
- ``POST /api/live/models/{model}/refresh``    — retry now instead of waiting out backoff.
- ``POST /api/live/sources/{source}/advance``  — stamp + announce rows an external
  writer committed to a landing table (the HTTP form of ``advance_source``).

The runner is started by the server's lifespan when the project has live
models and ``live.enabled`` is not false. Its refreshes go through the
server's write queue, like every other write.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from havn.server.deps import (
    DbConnReadOnlyOptional,
    _get_config,
    _get_project_dir,
    _require_permission,
)

logger = logging.getLogger("havn.server")

router = APIRouter()

_runner = None
_runner_lock = threading.Lock()


def _settings():
    from havn.engine.live.settings import LiveSettings

    try:
        return LiveSettings.from_raw(getattr(_get_config(), "live", {}) or {})
    except ValueError as e:
        logger.warning("live: bad live: settings in project.yml, using defaults: %s", e)
        return LiveSettings()


def _models():
    from havn.engine.transform.discovery import discover_all_models

    runner = _runner
    if runner is not None and runner.running and runner.models:
        return runner.models
    try:
        return discover_all_models(_get_project_dir())
    except Exception as e:
        logger.debug("live: discovery failed: %s", e)
        return []


def get_live_runner():
    return _runner


def start_live_runner(*, only_if_live: bool = True):
    """Start the runner (once). Returns it, or None when there is nothing to run."""
    global _runner
    with _runner_lock:
        if _runner is not None and _runner.running:
            return _runner
        settings = _settings()
        if only_if_live and not settings.enabled:
            return None
        models = _models()
        if only_if_live and not any(m.live for m in models):
            return None
        from havn.engine.live.runner import LiveRunner
        from havn.server.deps import _get_write_queue

        runner = LiveRunner(
            _get_project_dir(), _get_write_queue(),
            settings=settings, alerts=_get_config().alerts,
        )
        runner.start()
        _runner = runner
        logger.info("Live runner started (%d live models)", len(runner.graph.order))
        return runner


def stop_live_runner() -> None:
    global _runner
    with _runner_lock:
        runner, _runner = _runner, None
    if runner is not None:
        try:
            runner.stop()
        except Exception as e:
            logger.debug("live runner stop failed: %s", e)


@router.get("/api/live/status")
def live_status_endpoint(request: Request, conn: DbConnReadOnlyOptional) -> dict:
    _require_permission(request, "read")
    from havn.engine.live.status import live_status

    runner = _runner
    snap = runner.snapshot() if runner is not None else {"running": False}
    models = _models()
    if conn is None:
        from havn.engine.live.graph import LiveGraph

        graph = LiveGraph.build(models)
        return {
            "runner": {k: v for k, v in snap.items() if k != "models"},
            "settings": _settings().to_dict(),
            "models": [{"model": n, "materialized": graph.models[n].materialized,
                        "status": "live", "lag_seconds": 0, "inputs": []} for n in graph.order],
            "sources": [],
            "max_lag_seconds": 0,
        }
    return live_status(conn, models, runner=snap, settings=runner.settings if runner else _settings())


@router.get("/api/live/events")
def live_events(request: Request, after: int = 0, max_idle: float = 0.0):
    """Server-sent events from the runner. ``max_idle`` > 0 closes after that long quiet."""
    _require_permission(request, "read")

    def _generate():
        last = after
        idle_since = time.monotonic()
        yield f"event: hello\ndata: {json.dumps({'running': bool(_runner and _runner.running)})}\n\n"
        while True:
            runner = _runner
            if runner is None:
                time.sleep(min(1.0, max_idle or 1.0))
                batch = []
            else:
                if last > runner.last_event_id:
                    last = 0  # the runner restarted; its ids did too
                batch = runner.events_since(last, timeout=min(15.0, max_idle or 15.0))
            for evt in batch:
                last = evt["id"]
                yield f"id: {evt['id']}\nevent: {evt['type']}\ndata: {json.dumps(evt['data'], default=str)}\n\n"
            if batch:
                idle_since = time.monotonic()
            elif max_idle and time.monotonic() - idle_since >= max_idle:
                break
            else:
                yield ": keepalive\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/live/start")
def live_start(request: Request) -> dict:
    _require_permission(request, "execute")
    runner = start_live_runner(only_if_live=False)
    return {"running": bool(runner and runner.running)}


@router.post("/api/live/stop")
def live_stop(request: Request) -> dict:
    _require_permission(request, "execute")
    stop_live_runner()
    return {"running": False}


def _submit(func):
    from havn.engine.write_queue import cursor_for
    from havn.server.deps import _get_write_queue

    def _job(conn):
        cur = cursor_for(conn)
        try:
            return func(cur)
        finally:
            cur.close()

    return _get_write_queue().submit(_job).result(timeout=60)


def _set_paused(model: str, paused: bool) -> dict:
    model = model.lower()
    runner = _runner
    if runner is not None and runner.running:
        try:
            return runner.pause(model) if paused else runner.resume(model)
        except KeyError as e:
            raise HTTPException(404, str(e))
    from havn.engine.live.graph import LiveGraph
    from havn.engine.live.state import set_paused

    if model not in LiveGraph.build(_models()).order:
        raise HTTPException(404, f"{model} is not a live model")
    return _submit(lambda cur: set_paused(cur, model, paused)).to_dict()


@router.post("/api/live/models/{model}/pause")
def live_pause(request: Request, model: str) -> dict:
    _require_permission(request, "execute")
    return _set_paused(model, True)


@router.post("/api/live/models/{model}/resume")
def live_resume(request: Request, model: str) -> dict:
    _require_permission(request, "execute")
    return _set_paused(model, False)


@router.post("/api/live/models/{model}/refresh")
def live_refresh(request: Request, model: str) -> dict:
    _require_permission(request, "execute")
    runner = _runner
    if runner is None or not runner.running:
        raise HTTPException(409, "the live runner is not running")
    try:
        runner.refresh_now(model)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"status": "scheduled", "model": model.lower()}


@router.post("/api/live/sources/{source}/advance")
def live_advance(request: Request, source: str) -> dict:
    _require_permission(request, "execute")
    from havn.engine.live.sources import advance_source, split_source

    try:
        split_source(source)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        adv = _submit(lambda cur: advance_source(cur, source))
    except ValueError as e:
        raise HTTPException(404, str(e))
    if adv is None:
        return {"source": source.lower(), "rows": 0, "advanced": False}
    return {
        "source": adv.source, "rows": adv.rows, "advanced": True,
        "watermark_from": adv.wm_from, "watermark": adv.wm_to,
    }

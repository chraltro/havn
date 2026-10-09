"""Server side of governed Python: spawn the child, answer its SQL, supervise it.

See ``python_child.py`` for what runs in the child. This module starts it,
hands it the code to run, and executes every statement it sends through a
:class:`~havn.engine.governance.statements.GovernedSession` -- the user's
masking and row policies, and the writes their role allows.

Supervision mirrors the in-process runner: a hard timeout, an idle timeout
(no output, no statements and no query in flight), stdout/stderr capture.
Stopping a process is reliable where stopping a thread is not, so a timed-out
governed script is killed rather than orphaned.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import duckdb

from .rewrite import GovernanceError
from .statements import GovernedSession, protected_paths
from .viewer import Viewer

logger = logging.getLogger("havn.governance")

CHILD_SCRIPT = Path(__file__).with_name("python_child.py")
# Imported in the child before the guards go up, so that libraries which
# touch ctypes or spawn a helper at import time still load.
_PRELOAD = ("numpy", "pandas", "pyarrow", "pyarrow.ipc")
_START_TIMEOUT = 60.0
_POLL = 0.1


class GovernedPythonError(Exception):
    """Governed Python could not be started or was refused."""


class GovernedTimeout(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def warehouse_path(conn: duckdb.DuckDBPyConnection) -> Path | None:
    """The file behind the connection's current database, if it is a file."""
    try:
        row = conn.execute(
            "SELECT path FROM duckdb_databases() WHERE database_name = current_database()"
        ).fetchone()
    except duckdb.Error:
        return None
    if not row or not row[0] or str(row[0]) in (":memory:", "memory"):
        return None
    return Path(str(row[0]))


def governance_settings(project_dir: Path | None) -> dict:
    """``governance:`` from project.yml, with defaults."""
    out = {"python": "subprocess", "isolation": "best_effort", "pii_schemas": ["gold"]}
    if project_dir is None:
        return out
    try:
        from havn.config import load_project

        cfg = load_project(project_dir).governance
        out.update({"python": cfg.python, "isolation": cfg.isolation, "pii_schemas": list(cfg.pii_schemas)})
    except Exception:
        logger.debug("Could not read governance settings", exc_info=True)
    return out


def _ipc(table) -> bytes:
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _from_ipc(blob: bytes):
    import pyarrow as pa

    return pa.ipc.open_stream(pa.py_buffer(blob)).read_all()


class _Wire:
    """JSON frames plus binary frames; the server never unpickles child data."""

    MAX_HEADER = 4 * 1024 * 1024

    def __init__(self, conn) -> None:
        self.conn = conn

    def send(self, header: dict, blobs: list[bytes] | None = None) -> None:
        blobs = blobs or []
        self.conn.send_bytes(json.dumps(dict(header, blobs=len(blobs)), default=str).encode())
        for blob in blobs:
            self.conn.send_bytes(blob)

    def recv(self) -> tuple[dict, list[bytes]]:
        raw = self.conn.recv_bytes(self.MAX_HEADER)
        header = json.loads(raw.decode("utf-8"))
        if not isinstance(header, dict):
            raise GovernedPythonError("malformed message from governed process")
        count = int(header.get("blobs", 0) or 0)
        if count < 0 or count > 64:
            raise GovernedPythonError("malformed message from governed process")
        blobs = [self.conn.recv_bytes() for _ in range(count)]
        return header, blobs


class GovernedProcess:
    """One child process and the session that answers its statements."""

    def __init__(
        self,
        session: GovernedSession,
        *,
        cwd: Path | None,
        warehouse: Path | None,
        name: str = "governed",
    ) -> None:
        self.session = session
        self.cwd = cwd
        self.warehouse = warehouse
        self.name = name
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self._out_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.wire: _Wire | None = None
        self.isolated: bool | None = None
        self.busy = False
        self._readers: list[threading.Thread] = []

    # -- lifecycle -------------------------------------------------------

    def start(self) -> dict:
        from multiprocessing.connection import Listener

        authkey = secrets.token_bytes(32)
        listener = Listener(("127.0.0.1", 0), authkey=authkey)
        host, port = listener.address
        env = dict(os.environ)
        # The child must not inherit a way back into this process's import
        # path tricks; it gets the same interpreter and site-packages.
        env.pop("HAVN_GOVERNED_CHILD", None)
        env["HAVN_GOVERNED_CHILD"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [sys.executable, str(CHILD_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.cwd) if self.cwd else None,
            env=env,
            creationflags=creationflags,
        )
        for stream, buf in ((self.proc.stdout, self.stdout), (self.proc.stderr, self.stderr)):
            t = threading.Thread(target=self._pump, args=(stream, buf), daemon=True,
                                 name=f"havn-governed-io:{self.name}")
            t.start()
            self._readers.append(t)
        hello = {
            "host": host,
            "port": port,
            "authkey": authkey.hex(),
            "cwd": str(self.cwd) if self.cwd else "",
            "warehouse": str(self.warehouse) if self.warehouse else "",
            "protected": [str(p) for p in self.session.protected],
            "preload": list(_PRELOAD),
        }
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write((json.dumps(hello) + "\n").encode())
            self.proc.stdin.close()
        except OSError as e:
            listener.close()
            self.kill()
            raise GovernedPythonError(f"Could not start governed Python: {e}") from None

        accepted: dict = {}

        def _accept():
            try:
                accepted["conn"] = listener.accept()
            except Exception as e:  # noqa: BLE001
                accepted["error"] = e

        t = threading.Thread(target=_accept, daemon=True)
        t.start()
        deadline = time.monotonic() + _START_TIMEOUT
        while t.is_alive() and time.monotonic() < deadline:
            t.join(0.1)
            if self.proc.poll() is not None:
                break
        if "conn" not in accepted:
            try:
                listener.close()
            except Exception:
                pass
            self.kill()
            detail = self.stderr.getvalue().strip()[-2000:]
            raise GovernedPythonError(
                "Governed Python process did not start" + (f": {detail}" if detail else "")
            )
        listener.close()  # one connection, ever
        self.wire = _Wire(accepted["conn"])
        if not self.wire.conn.poll(_START_TIMEOUT):
            self.kill()
            raise GovernedPythonError("Governed Python process did not report in")
        hello_reply, _ = self.wire.recv()
        self.isolated = bool(hello_reply.get("isolated"))
        return hello_reply

    def _pump(self, stream, buf: io.StringIO) -> None:
        try:
            for raw in iter(lambda: stream.readline(), b""):
                text = raw.decode("utf-8", errors="replace")
                with self._out_lock:
                    buf.write(text)
        except Exception:
            pass

    def output(self) -> tuple[str, str]:
        with self._out_lock:
            return self.stdout.getvalue(), self.stderr.getvalue()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        self.session.interrupt()
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        if self.wire is not None:
            try:
                self.wire.conn.close()
            except Exception:
                pass
        for t in self._readers:
            t.join(timeout=1)

    def shutdown(self) -> None:
        try:
            if self.wire is not None and self.alive():
                self.wire.send({"op": "shutdown"})
                if self.wire.conn.poll(2):
                    self.wire.recv()
        except Exception:
            pass
        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
        self.kill()

    # -- serving ---------------------------------------------------------

    def activity_token(self) -> tuple:
        out, err = self.output()
        return (len(out), len(err), self.session.requests, self.busy)

    def run(
        self,
        header: dict,
        *,
        timeout: float,
        idle_timeout: float = 0,
        on_poll: Callable[[], None] | None = None,
    ) -> dict:
        """Send one command and answer the child's requests until it is done."""
        assert self.wire is not None
        self.wire.send(header)
        start = time.monotonic()
        last_activity = start
        last_token = self.activity_token()
        # A statement running on the server blocks this loop; the watchdog
        # makes the hard timeout fire anyway (interrupt the query, kill the
        # child).
        expired = threading.Event()

        def _expire() -> None:
            expired.set()
            self.kill()

        watchdog = threading.Timer(timeout, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            return self._serve(start, timeout, idle_timeout, on_poll, expired, last_activity, last_token)
        except (GovernedPythonError, EOFError, OSError):
            if expired.is_set():
                raise GovernedTimeout("timeout") from None
            raise
        finally:
            watchdog.cancel()

    def _serve(self, start, timeout, idle_timeout, on_poll, expired, last_activity, last_token) -> dict:
        assert self.wire is not None
        while True:
            now = time.monotonic()
            if expired.is_set() or now - start >= timeout:
                raise GovernedTimeout("timeout")
            if idle_timeout and now - last_activity > idle_timeout:
                raise GovernedTimeout("idle")
            try:
                ready = self.wire.conn.poll(_POLL)
            except (EOFError, OSError):
                ready = False
                if not self.alive():
                    raise GovernedPythonError("The governed Python process exited unexpectedly") from None
            if on_poll is not None:
                on_poll()
            if not ready:
                if not self.alive():
                    # Give the pipes a moment to drain what it printed.
                    time.sleep(0.1)
                    raise GovernedPythonError(
                        f"The governed Python process exited unexpectedly (code {self.proc.returncode})"
                    )
                token = self.activity_token()
                if token != last_token:
                    last_token, last_activity = token, now
                continue
            try:
                msg, blobs = self.wire.recv()
            except (EOFError, OSError):
                raise GovernedPythonError("The governed Python process closed its connection") from None
            last_activity = time.monotonic()
            if msg.get("op") == "done":
                return msg
            reply, data = self._handle(msg, blobs)
            self.wire.send(reply, data)

    def _handle(self, msg: dict, blobs: list[bytes]) -> tuple[dict, list[bytes]]:
        op = msg.get("op")
        self.busy = True
        try:
            if op == "execute":
                sql = msg.get("sql")
                if not isinstance(sql, str):
                    return {"op": "error", "message": "SQL must be a string"}, []
                names = msg.get("frames") or []
                if not isinstance(names, list) or len(names) != len(blobs):
                    return {"op": "error", "message": "malformed request"}, []
                frames = {str(n): _from_ipc(b) for n, b in zip(names, blobs)}
                params = msg.get("params")
                if params is not None and not isinstance(params, (list, dict)):
                    return {"op": "error", "message": "parameters must be a list or dict"}, []
                result = self.session.execute(sql, params, frames)
                data = [_ipc(result.table)] if result.table is not None else []
                return {
                    "op": "result",
                    "columns": result.columns,
                    "types": result.types,
                    "rowcount": result.rowcount,
                }, data
            if op == "register":
                if len(blobs) != 1:
                    return {"op": "error", "message": "malformed request"}, []
                self.session.register(str(msg.get("name", "")), _from_ipc(blobs[0]))
                return {"op": "ok"}, []
            if op == "unregister":
                self.session.unregister(str(msg.get("name", "")))
                return {"op": "ok"}, []
            return {"op": "error", "message": f"unknown request {op!r}"}, []
        except GovernanceError as e:
            return {"op": "error", "message": f"Governance: {e}"}, []
        except duckdb.Error as e:
            return {"op": "error", "message": f"{type(e).__name__}: {e}"}, []
        except Exception as e:  # noqa: BLE001 - reported to the script, never raised here
            logger.debug("Governed request failed", exc_info=True)
            return {"op": "error", "message": f"{type(e).__name__}: {e}"}, []
        finally:
            self.busy = False


def open_session(
    conn: duckdb.DuckDBPyConnection,
    viewer: Viewer,
    project_dir: Path | None,
) -> tuple[GovernedSession, Path | None]:
    """A governed session on its own cursor of ``conn``'s database."""
    warehouse = warehouse_path(conn)
    cursor = conn.cursor()
    session = GovernedSession(cursor, viewer, project_dir=project_dir, warehouse=warehouse, cwd=project_dir)
    return session, warehouse


def check_isolation_policy(process: GovernedProcess, settings: dict) -> str | None:
    """Kill and refuse under ``isolation: strict`` when the OS did not isolate the child.

    Returns a warning for the run log under ``best_effort``, else None.
    """
    if process.isolated:
        return None
    if settings.get("isolation") == "strict":
        process.kill()
        raise GovernedPythonError(
            "governance.isolation is strict, and on this platform the operating system does "
            "not keep the warehouse file away from a governed Python process (it runs as the "
            "same OS user). Ask an admin to run this script."
        )
    return (
        "havn: on this platform the governed process could open the warehouse file at the OS "
        "level; it runs with Python-level guards only (see docs/governance.md).\n"
    )


@dataclass
class ScriptOutcome:
    status: str
    log_output: str
    error: str | None = None
    reason: str | None = None
    elapsed: float = 0.0
    isolated: bool | None = None
    extra: dict = field(default_factory=dict)


def run_governed_script(
    conn: duckdb.DuckDBPyConnection,
    script_path: Path,
    source: str,
    viewer: Viewer,
    *,
    project_dir: Path | None,
    timeout: float,
    idle_timeout: float,
) -> ScriptOutcome:
    """Run a .py script in a governed child process for ``viewer``."""
    settings = governance_settings(project_dir)
    if settings.get("python") == "refuse":
        raise GovernedPythonError(
            "Masking or row policies apply to you, and this project runs no Python for "
            "governed users (governance.python: refuse). Ask an admin to run it."
        )
    session, warehouse = open_session(conn, viewer, project_dir)
    process = GovernedProcess(session, cwd=project_dir or script_path.parent, warehouse=warehouse,
                              name=script_path.name)
    start = time.monotonic()
    try:
        process.start()
        warning = check_isolation_policy(process, settings)
        try:
            done = process.run(
                {"op": "run_script", "path": str(script_path), "source": source},
                timeout=timeout, idle_timeout=idle_timeout,
            )
        except GovernedTimeout as t:
            process.kill()
            out, err = process.output()
            return ScriptOutcome(status="timeout", log_output=(warning or "") + out + err,
                                 reason=t.reason, elapsed=time.monotonic() - start,
                                 isolated=process.isolated)
        except GovernedPythonError as e:
            process.kill()
            out, err = process.output()
            return ScriptOutcome(status="error", log_output=(warning or "") + out + err, error=str(e),
                                 elapsed=time.monotonic() - start, isolated=process.isolated)
        process.shutdown()
        out, err = process.output()
        log = (warning or "") + out + err
        if done.get("ok"):
            return ScriptOutcome(status="success", log_output=log, elapsed=time.monotonic() - start,
                                 isolated=process.isolated)
        tb = done.get("traceback") or ""
        return ScriptOutcome(status="error", log_output=log + ("\n" + tb if tb else ""),
                             error=done.get("error") or "script failed",
                             elapsed=time.monotonic() - start, isolated=process.isolated)
    finally:
        process.kill()
        session.close()


# ---------------------------------------------------------------------------
# Notebook kernels
# ---------------------------------------------------------------------------


class GovernedKernel:
    """A long-lived governed child for one user's notebook (shared namespace)."""

    IDLE_SECONDS = 1800

    def __init__(
        self,
        conn,
        viewer: Viewer,
        project_dir: Path | None,
        name: str,
        session: GovernedSession | None = None,
    ) -> None:
        self.viewer = viewer
        self.owns_session = session is None
        if session is None:
            session, warehouse = open_session(conn, viewer, project_dir)
        else:
            warehouse = warehouse_path(conn)
        self.session = session
        self.process = GovernedProcess(self.session, cwd=project_dir, warehouse=warehouse, name=name)
        self.settings = governance_settings(project_dir)
        self.warning: str | None = None
        self.last_used = time.monotonic()
        self.lock = threading.Lock()

    def start(self) -> None:
        if self.settings.get("python") == "refuse":
            raise GovernedPythonError(
                "Masking or row policies apply to you, and this project runs no Python for "
                "governed users (governance.python: refuse)."
            )
        self.process.start()
        self.warning = check_isolation_policy(self.process, self.settings)

    def run_cell(self, source: str, timeout: float) -> list[dict]:
        with self.lock:
            self.last_used = time.monotonic()
            try:
                done = self.process.run({"op": "run_cell", "source": source}, timeout=timeout)
            except GovernedTimeout:
                self.close()
                return [{"type": "error", "text": f"Cell timed out after {timeout:g}s; the kernel was restarted."}]
            except GovernedPythonError as e:
                self.close()
                return [{"type": "error", "text": str(e)}]
            outputs = list(done.get("outputs") or [])
            if self.warning:
                outputs.insert(0, {"type": "text", "text": self.warning})
                self.warning = None
            return outputs

    def alive(self) -> bool:
        return self.process.alive()

    def close(self) -> None:
        try:
            self.process.shutdown()
        finally:
            if self.owns_session:
                self.session.close()


_kernels: dict[tuple, GovernedKernel] = {}
_kernels_lock = threading.Lock()


def get_kernel(conn, viewer: Viewer, project_dir: Path | None, notebook: str, *, reset: bool = False) -> GovernedKernel:
    key = (viewer.cache_key(), str(project_dir), notebook)
    with _kernels_lock:
        _reap_locked()
        kernel = _kernels.get(key)
        if kernel is not None and (reset or not kernel.alive()):
            _kernels.pop(key, None)
            kernel.close()
            kernel = None
        if kernel is None:
            kernel = GovernedKernel(conn, viewer, project_dir, notebook)
            kernel.start()
            _kernels[key] = kernel
        return kernel


def drop_kernel(viewer: Viewer, project_dir: Path | None, notebook: str) -> None:
    key = (viewer.cache_key(), str(project_dir), notebook)
    with _kernels_lock:
        kernel = _kernels.pop(key, None)
    if kernel is not None:
        kernel.close()


def _reap_locked() -> None:
    now = time.monotonic()
    for key, kernel in list(_kernels.items()):
        if now - kernel.last_used > GovernedKernel.IDLE_SECONDS or not kernel.alive():
            _kernels.pop(key, None)
            try:
                kernel.close()
            except Exception:
                pass


def close_all_kernels() -> None:
    with _kernels_lock:
        kernels = list(_kernels.values())
        _kernels.clear()
    for kernel in kernels:
        try:
            kernel.close()
        except Exception:
            pass


def _server_cell(kind: str, source: str, session: GovernedSession, project_dir: Path | None) -> dict:
    """A SQL or ingest cell, run governed on the server (no child needed)."""
    from havn.engine.notebook.ingest_cell import execute_ingest_cell
    from havn.engine.notebook.sql_cell import execute_sql_cell

    from .statements import GovernedCursor

    cursor = GovernedCursor(session)
    start = time.perf_counter()
    try:
        if kind == "sql":
            return execute_sql_cell(cursor, source)
        return execute_ingest_cell(cursor, source, project_dir)
    except GovernanceError as e:
        return {"outputs": [{"type": "error", "text": f"Governance: {e}"}],
                "duration_ms": int((time.perf_counter() - start) * 1000)}


def run_cell_governed(
    conn,
    viewer: Viewer,
    project_dir: Path | None,
    notebook: str,
    cell_type: str,
    source: str,
    *,
    reset: bool = False,
    timeout: float = 300,
) -> dict:
    """One interactive notebook cell for a governed user."""
    start = time.perf_counter()
    if cell_type in ("sql", "ingest"):
        session, _w = open_session(conn, viewer, project_dir)
        try:
            return _server_cell(cell_type, source, session, project_dir)
        finally:
            session.close()
    if reset:
        drop_kernel(viewer, project_dir, notebook)
    try:
        kernel = get_kernel(conn, viewer, project_dir, notebook)
    except GovernedPythonError as e:
        return {"outputs": [{"type": "error", "text": str(e)}], "duration_ms": 0}
    outputs = kernel.run_cell(source, timeout)
    return {"outputs": outputs, "duration_ms": int((time.perf_counter() - start) * 1000)}


def run_notebook_governed(
    conn,
    notebook: dict,
    viewer: Viewer,
    *,
    project_dir: Path | None = None,
    stop_on_error: bool = False,
    on_cell_start: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    cell_timeout: float = 3600,
) -> dict:
    """``run_notebook`` for a governed user: same contract, governed execution.

    Code cells share one child process (one namespace) for the run; SQL and
    ingest cells run governed on the server, on the same session, so a
    DataFrame a code cell registers is visible to a later SQL cell.
    """
    kernel: GovernedKernel | None = None
    session, _w = open_session(conn, viewer, project_dir)
    total_ms = 0
    cell_results: list[dict] = []
    skipped: list = []
    stopped = False
    try:
        for cell in notebook.get("cells", []):
            cell_type = cell.get("type", "")
            source = cell.get("source", "")
            if isinstance(source, list):
                source = "".join(source)
            if cell_type not in ("code", "sql", "ingest"):
                continue
            if not stopped and should_stop is not None and should_stop():
                stopped = True
            if stopped:
                cell["outputs"] = []
                skipped.append(cell.get("id"))
                continue
            if on_cell_start is not None:
                on_cell_start(cell)
            t0 = time.perf_counter()
            if cell_type == "code":
                if kernel is None:
                    # The run's session, so registrations carry over to SQL cells.
                    kernel = GovernedKernel(conn, viewer, project_dir, notebook.get("name", "notebook"),
                                            session=session)
                    try:
                        kernel.start()
                    except GovernedPythonError as e:
                        outputs = [{"type": "error", "text": str(e)}]
                        kernel = None
                    else:
                        outputs = kernel.run_cell(source, cell_timeout)
                else:
                    outputs = kernel.run_cell(source, cell_timeout)
                result = {"outputs": outputs, "duration_ms": int((time.perf_counter() - t0) * 1000)}
            else:
                result = _server_cell(cell_type, source, session, project_dir)
            cell["outputs"] = result["outputs"]
            cell["duration_ms"] = result["duration_ms"]
            total_ms += result["duration_ms"]
            has_error = any(o.get("type") == "error" for o in result["outputs"])
            cell_results.append({
                "cell_id": cell.get("id"),
                "type": cell_type,
                "duration_ms": result["duration_ms"],
                "has_error": has_error,
                "outputs": result["outputs"],
            })
            if has_error and stop_on_error:
                stopped = True
    finally:
        if kernel is not None:
            try:
                kernel.close()
            except Exception:
                pass
        session.close()
    notebook["last_run_ms"] = total_ms
    notebook["cell_results"] = cell_results
    if stop_on_error or should_stop is not None:
        notebook["skipped_cells"] = skipped
    return notebook


import atexit  # noqa: E402

atexit.register(close_all_kernels)

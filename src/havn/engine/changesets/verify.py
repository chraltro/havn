"""Verify a change set without touching the project or the warehouse.

The checks, in order, all run against an overlay (a temporary copy of the
project with the change set applied):

``safety``      every changed model's SQL passes the read-only check that
                ``/api/query`` uses, so agent SQL cannot carry a second
                statement or read files during the scratch build.
``validate``    discovery, the DAG (cycles, duplicates) and the name-level
                checks of ``havn validate``.
``bind``        the shadow bind pass, including contract column declarations.
``unit_tests``  the unit tests of every affected model, and any changed test.
``build``       every affected model (changed models plus everything
                downstream) built as a table into a scratch database attached
                next to the warehouse, upstream references to other affected
                models pointed at their scratch copies.
``assertions``  each rebuilt model's ``@assert`` lines, on the scratch copy.
``contracts``   contracts on affected models, on the scratch copy; a changed
                contract on an unaffected model runs on the real table.
``data_diff``   each scratch copy compared with the real table: rows added,
                removed and modified, and schema changes.

The scratch database is a temporary file, detached and deleted at the end.
Nothing is written to the warehouse: model state, run log, profiles and
contract history are not touched.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from havn.engine.changesets.overlay import overlay_project
from havn.engine.changesets.store import ChangeSet, proposed_files

logger = logging.getLogger("havn.changesets")

DEFAULT_TIMEOUT = 300
SAMPLE_ROWS = 5

NOTES = [
    "Incremental and microbatch models are built in full in scratch; the diff "
    "compares a full rebuild with the current table.",
    "Snapshot (SCD2) models are not rebuilt in scratch.",
    "The scratch build uses the macros registered on the running warehouse "
    "connection; unit tests and the bind pass load macros from the change set.",
]


def _check(name: str, status: str, summary: str, details: list | None = None) -> dict:
    return {"name": name, "status": status, "summary": summary, "details": details or []}


def _downstream(models: list, roots: set[str]) -> set[str]:
    children: dict[str, set[str]] = {}
    for m in models:
        for dep in m.depends_on:
            children.setdefault(dep, set()).add(m.full_name)
    out = set(roots)
    stack = list(roots)
    while stack:
        node = stack.pop()
        for child in children.get(node, ()):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def _rel(path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _model_signature(m) -> tuple:
    return (m.content_hash or m.sql, m.materialized, m.schema, m.name)


class _Deadline:
    """Interrupts the connection once the verification budget is spent."""

    def __init__(self, conn, seconds: float) -> None:
        self.expired = False
        self._end = time.monotonic() + seconds
        self._timer = None
        if conn is not None:
            def fire() -> None:
                self.expired = True
                try:
                    conn.interrupt()
                except Exception:
                    pass
            self._timer = threading.Timer(seconds, fire)
            self._timer.daemon = True
            self._timer.start()

    def left(self) -> bool:
        if time.monotonic() >= self._end:
            self.expired = True
        return not self.expired

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()


def verify_change_set(
    project_dir: Path,
    cs: ChangeSet,
    *,
    conn: duckdb.DuckDBPyConnection | None,
    config: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Run every check on ``cs`` and return the verification report.

    ``conn`` is a cursor on the warehouse (read-only is enough). Without one
    the checks that need data (build, assertions, contracts, data diff) are
    skipped and the report says so.
    """
    from havn.engine.transform import build_dag, discover_all_models

    started = time.perf_counter()
    project_dir = Path(project_dir)
    if config is None:
        from havn.config import load_project

        config = load_project(project_dir)

    files = proposed_files(cs)
    checks: list[dict] = []
    report: dict[str, Any] = {
        "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": cs.revision,
        "changed_models": [],
        "affected_models": [],
        "removed_models": [],
        "checks": checks,
        "builds": [],
        "diffs": [],
        "notes": list(NOTES),
        "warehouse": conn is not None,
    }

    def finish() -> dict:
        report["ok"] = bool(checks) and not any(c["status"] == "fail" for c in checks)
        report["status"] = "ready" if report["ok"] else "failed"
        report["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return report

    # --- safety: agent SQL is checked before anything runs it -------------
    from havn.engine.transform.bind import read_only_rejection

    unsafe_paths: dict[str, str] = {}
    for path, content in files.items():
        if content is None or not path.startswith("transform/") or not path.endswith(".sql"):
            continue
        rejection = read_only_rejection(content)
        if rejection:
            unsafe_paths[path] = rejection
    if unsafe_paths:
        checks.append(_check(
            "safety", "fail",
            f"{len(unsafe_paths)} changed model(s) are not plain read-only SELECTs, "
            "so they were not built or tested",
            [{"path": p, "message": m} for p, m in sorted(unsafe_paths.items())],
        ))
    else:
        checks.append(_check("safety", "pass", "Changed SQL is read-only"))

    with overlay_project(project_dir, files) as overlay:
        # --- validate: discovery and the DAG --------------------------------
        try:
            base_models = discover_all_models(project_dir, config)
        except Exception as e:
            logger.debug("Base discovery failed: %s", e)
            base_models = []
        try:
            models = discover_all_models(overlay, config)
            ordered = build_dag(models)
        except Exception as e:
            checks.append(_check("validate", "fail", f"The project no longer loads: {e}"))
            return finish()

        base_by_name = {m.full_name: m for m in base_models}
        by_name = {m.full_name: m for m in ordered}
        changed = {
            name for name, m in by_name.items()
            if name not in base_by_name or _model_signature(base_by_name[name]) != _model_signature(m)
        }
        removed = sorted(set(base_by_name) - set(by_name))
        affected = _downstream(ordered, changed | set(removed)) - set(removed)
        unsafe_models = {
            m.full_name for m in ordered if _rel(m.path, overlay) in unsafe_paths
        }
        report["changed_models"] = sorted(changed)
        report["affected_models"] = [m.full_name for m in ordered if m.full_name in affected]
        report["removed_models"] = removed

        from havn.engine.transform import validate_models

        try:
            v_errors = validate_models(conn, ordered, bind=False, project_dir=overlay)
        except Exception as e:
            v_errors = []
            checks.append(_check("validate", "warn", f"Name-level checks could not run: {e}"))
        else:
            mine = [e for e in v_errors if e.model in affected]
            errs = [e for e in mine if e.severity == "error"]
            details = [
                {"model": e.model, "severity": e.severity, "message": e.message, "line": e.line}
                for e in mine
            ]
            for name in removed:
                dependents = [m.full_name for m in ordered if name in m.depends_on]
                if dependents:
                    details.append({
                        "model": name, "severity": "warning",
                        "message": f"removed, but still referenced by {', '.join(dependents)}",
                        "line": None,
                    })
            checks.append(_check(
                "validate",
                "fail" if errs else ("warn" if details else "pass"),
                f"{len(errs)} error(s) in affected models" if errs
                else f"{len(ordered)} models load, no cycles",
                details,
            ))

        # --- bind ------------------------------------------------------------
        if not affected:
            checks.append(_check("bind", "skip", "No model is affected"))
        else:
            checks.append(_bind_check(conn, ordered, affected, unsafe_models, overlay))

        # --- unit tests --------------------------------------------------------
        checks.append(_unit_test_check(conn, overlay, files, affected, unsafe_models))

        # --- scratch build, assertions, contracts, data diff -------------------
        if conn is None:
            for name in ("build", "assertions", "contracts", "data_diff"):
                checks.append(_check(name, "skip", "No warehouse yet; run a pipeline first"))
            return finish()
        _scratch_checks(
            conn, cs, ordered, affected, unsafe_models, changed, removed,
            overlay, files, config, report, timeout,
        )
    return finish()


def _bind_check(conn, ordered, affected, unsafe_models, overlay) -> dict:
    from havn.engine.transform.analysis import _bind_errors
    from havn.engine.transform.bind import ancestor_closure

    targets = [m.full_name for m in ordered if m.full_name in affected and m.full_name not in unsafe_models]
    if not targets:
        return _check("bind", "skip", "Nothing safe to bind")
    chain = ancestor_closure(
        [m for m in ordered if m.full_name not in unsafe_models], targets
    )
    bind_conn = conn if conn is not None else duckdb.connect(":memory:")
    try:
        errors = _bind_errors(bind_conn, chain, overlay)
    except Exception as e:
        return _check("bind", "warn", f"The bind pass could not run: {e}")
    finally:
        if conn is None:
            bind_conn.close()
    mine = [e for e in errors if e.model in affected or (e.model == "" and e.severity == "error")]
    unavailable = [e for e in errors if e.model == "" and e.severity != "error"]
    errs = [e for e in mine if e.severity == "error"]
    details = [
        {"model": e.model, "severity": e.severity, "message": e.message, "line": e.line}
        for e in mine + unavailable
    ]
    if errs:
        return _check("bind", "fail", f"{len(errs)} bind error(s)", details)
    if unavailable and not mine:
        return _check("bind", "warn", "The bind pass was unavailable", details)
    return _check("bind", "pass", f"{len(targets)} affected model(s) resolve", details)


def _unit_test_check(conn, overlay, files, affected, unsafe_models) -> dict:
    from havn.engine.unit_tests import (
        catalog_from_connection,
        load_unit_tests,
        run_unit_tests,
    )

    cases, load_errors = load_unit_tests(overlay)
    changed_tests = {
        p[len("tests/unit/"):] for p in files if p.startswith("tests/unit/")
    }
    short_affected = {a.split(".")[-1] for a in affected}

    def covers(case) -> bool:
        model = case.model.lower()
        return model in affected or model in short_affected or case.source_path in changed_tests

    chosen = [c for c in cases if covers(c)]
    skipped_unsafe = [c for c in chosen if c.model.lower() in unsafe_models]
    chosen = [c for c in chosen if c not in skipped_unsafe]
    relevant_errors = [
        e for e in load_errors if any(e.startswith(t + ":") for t in changed_tests)
    ]
    if not chosen and not relevant_errors:
        if skipped_unsafe:
            return _check("unit_tests", "fail", "Tests of unsafe models were not run")
        return _check("unit_tests", "skip", "No unit tests cover the affected models")
    catalog = {}
    if conn is not None:
        try:
            catalog = catalog_from_connection(conn)
        except Exception:
            catalog = {}
    result = run_unit_tests(overlay, cases=chosen, catalog=catalog)
    details = [r.to_dict() for r in result.results]
    for e in relevant_errors:
        details.append({"name": "load", "model": "", "status": "error", "message": e})
    failed = result.failed + result.errored + len(relevant_errors)
    if failed:
        return _check("unit_tests", "fail", f"{failed} of {len(chosen)} test(s) failed", details)
    return _check("unit_tests", "pass", f"{result.passed} test(s) passed", details)


def _scratch_checks(
    conn, cs, ordered, affected, unsafe_models, changed, removed,
    overlay, files, config, report, timeout,
) -> None:
    from havn.engine.contracts import discover_contracts, evaluate_contract
    from havn.engine.diff import diff_model, get_primary_key
    from havn.engine.sql_rewrite import rewrite_table_refs
    from havn.engine.transform import resolve_query, run_assertions, substitute_batch_window

    checks = report["checks"]
    by_name = {m.full_name: m for m in ordered}
    alias = f"_havn_verify_{cs.id[:8]}_{uuid.uuid4().hex[:6]}"
    scratch_dir = Path(tempfile.mkdtemp(prefix="havn-scratch-"))
    scratch_path = (scratch_dir / "scratch.duckdb").as_posix().replace("'", "''")
    built: dict[str, str] = {}  # full_name -> scratch relation
    mapping: dict[str, str] = {}  # full_name -> alias.schema.name (rewrite target)
    builds = report["builds"]
    deadline = _Deadline(conn, timeout)

    def rewriter(sql: str) -> str:
        return rewrite_table_refs(sql, mapping) if mapping else sql

    try:
        conn.execute(f"ATTACH '{scratch_path}' AS {alias} (READ_WRITE)")
    except Exception as e:
        deadline.cancel()
        shutil.rmtree(scratch_dir, ignore_errors=True)
        for name in ("build", "assertions", "contracts", "data_diff"):
            checks.append(_check(name, "skip", f"Could not attach a scratch database: {e}"))
        return

    try:
        # --- build -------------------------------------------------------------
        not_built: set[str] = set()
        for m in ordered:
            if m.full_name not in affected:
                continue
            entry = {"model": m.full_name, "status": "built", "rows": None, "error": None,
                     "materialized": m.materialized, "duration_ms": 0}
            builds.append(entry)
            blocked = [d for d in m.depends_on if d in not_built]
            if m.full_name in unsafe_models:
                entry.update(status="skipped", error="rejected by the safety check")
                not_built.add(m.full_name)
                continue
            if blocked:
                entry.update(status="skipped", error=f"upstream not built: {', '.join(blocked)}")
                not_built.add(m.full_name)
                continue
            if m.materialized == "ephemeral":
                entry.update(status="inlined")
                continue
            if m.materialized == "snapshot":
                entry.update(status="skipped", error="snapshot models are not rebuilt in scratch")
                not_built.add(m.full_name)
                continue
            if not deadline.left():
                entry.update(status="skipped", error="verification time budget spent")
                not_built.add(m.full_name)
                continue
            t0 = time.perf_counter()
            try:
                query = resolve_query(m, by_name, rewriter)
                if "{start}" in query or "{end}" in query:
                    query = substitute_batch_window(
                        query, datetime(1900, 1, 1), datetime(2200, 1, 1)
                    )
                query = query.replace("{this}", m.full_name)
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS {alias}."{m.schema}"')
                relation = f'{alias}."{m.schema}"."{m.name}"'
                conn.execute(f"CREATE TABLE {relation} AS\n{query}")
                entry["rows"] = conn.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0]
                built[m.full_name] = relation
                mapping[m.full_name] = f"{alias}.{m.schema}.{m.name}"
            except Exception as e:
                msg = str(e)
                if deadline.expired:
                    msg = "verification time budget spent"
                entry.update(status="error", error=msg)
                not_built.add(m.full_name)
            entry["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        errors = [b for b in builds if b["status"] == "error"]
        if errors:
            checks.append(_check("build", "fail", f"{len(errors)} model(s) failed to build in scratch", builds))
        elif not builds:
            checks.append(_check("build", "skip", "No model is affected"))
        elif any(b["status"] == "skipped" for b in builds):
            checks.append(_check(
                "build", "warn" if not deadline.expired else "fail",
                f"{len(built)} built, {sum(1 for b in builds if b['status'] == 'skipped')} skipped",
                builds,
            ))
        else:
            checks.append(_check("build", "pass", f"{len(built)} model(s) built in scratch", builds))

        # --- assertions ----------------------------------------------------------
        a_details = []
        a_fail = a_warn = 0
        for name, relation in built.items():
            m = by_name[name]
            if not (m.assertion_specs or m.assertions or m.grain):
                continue
            try:
                results = run_assertions(conn, dataclasses.replace(m, full_name=relation))
            except Exception as e:
                a_details.append({"model": name, "expression": "*", "passed": False, "detail": str(e)})
                a_fail += 1
                continue
            for r in results:
                a_details.append({
                    "model": name, "expression": r.expression, "passed": r.passed,
                    "detail": r.detail, "severity": r.severity,
                })
                if not r.passed:
                    if r.severity == "error":
                        a_fail += 1
                    else:
                        a_warn += 1
        if not a_details:
            checks.append(_check("assertions", "skip", "No assertions on the rebuilt models"))
        else:
            checks.append(_check(
                "assertions",
                "fail" if a_fail else ("warn" if a_warn else "pass"),
                f"{sum(1 for d in a_details if d['passed'])} of {len(a_details)} passed",
                a_details,
            ))

        # --- contracts -----------------------------------------------------------
        changed_contract_files = {p for p in files if p.startswith("contracts/")}
        c_details = []
        c_fail = c_warn = 0
        for contract in discover_contracts(overlay / "contracts"):
            rel = _rel(contract.path, overlay) if contract.path else ""
            if contract.model in built:
                result = evaluate_contract(conn, contract, relation=built[contract.model])
                where = "scratch"
            elif contract.model in affected:
                c_details.append({
                    "contract": contract.name, "model": contract.model, "passed": None,
                    "where": "skipped", "results": [], "error": "model was not built in scratch",
                })
                continue
            elif rel in changed_contract_files:
                result = evaluate_contract(conn, contract)
                where = "warehouse"
            else:
                continue
            c_details.append({
                "contract": contract.name, "model": contract.model, "passed": result.passed,
                "severity": result.severity, "where": where, "results": result.results,
                "error": result.error,
            })
            if not result.passed:
                if result.severity == "error":
                    c_fail += 1
                else:
                    c_warn += 1
        if not c_details:
            checks.append(_check("contracts", "skip", "No contracts on the affected models"))
        else:
            checks.append(_check(
                "contracts",
                "fail" if c_fail else ("warn" if c_warn else "pass"),
                f"{sum(1 for d in c_details if d['passed'])} of {len(c_details)} contract(s) passed",
                c_details,
            ))

        # --- data diff -------------------------------------------------------------
        diffs = report["diffs"]
        d_errors = 0
        for name, relation in built.items():
            m = by_name[name]
            pk = get_primary_key(m.sql, config, name)
            try:
                d = diff_model(conn, f"SELECT * FROM {relation}", m.schema, m.name, primary_key=pk)
            except Exception as e:
                diffs.append({"model": name, "error": str(e)})
                d_errors += 1
                continue
            if d.error:
                d_errors += 1
            diffs.append({
                "model": name,
                "changed": name in changed,
                "is_new": d.is_new,
                "added": d.added,
                "removed": d.removed,
                "modified": d.modified,
                "total_before": d.total_before,
                "total_after": d.total_after,
                "primary_key": pk,
                "schema_changes": [dataclasses.asdict(s) for s in d.schema_changes],
                "sample_added": d.sample_added[:SAMPLE_ROWS],
                "sample_removed": d.sample_removed[:SAMPLE_ROWS],
                "sample_modified": d.sample_modified[:SAMPLE_ROWS],
                "error": d.error,
            })
        for name in removed:
            diffs.append({"model": name, "removed_model": True, "error": None})
        if not diffs:
            checks.append(_check("data_diff", "skip", "Nothing was rebuilt"))
        else:
            added = sum(d.get("added") or 0 for d in diffs)
            removed_rows = sum(d.get("removed") or 0 for d in diffs)
            modified = sum(d.get("modified") or 0 for d in diffs)
            checks.append(_check(
                "data_diff",
                "fail" if d_errors else "pass",
                f"{len(diffs)} model(s): +{added} / -{removed_rows} / ~{modified} rows",
            ))
    finally:
        deadline.cancel()
        try:
            conn.execute(f"DETACH {alias}")
        except Exception as e:
            logger.warning("Could not detach scratch database %s: %s", alias, e)
        shutil.rmtree(scratch_dir, ignore_errors=True)

"""``havn ask --eval``: measure how well questions map onto your catalog.

Eval files are YAML, usually ``tests/ask/*.yml``:

.. code-block:: yaml

    cases:
      - question: Revenue by region last year
        expect:
          metrics: [revenue]
          dimensions: [region]
          start: "2025-01-01"
          end: "2026-01-01"
      - question: Average delivery time
        expect: unanswerable
      - question: now by month           # a follow-up
        history:
          - question: Revenue by region
            spec: {metrics: [revenue], dimensions: [region]}
        expect: {metrics: [revenue], dimensions: [region], grain: month}

Only the keys under ``expect`` are compared, so a case can pin just the
metric and leave the rest to the model. Planning only: nothing is run against
the warehouse, so an eval measures the question-to-spec step and nothing else.
"""

from __future__ import annotations

import glob
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from havn.textio import read_project_text

SPEC_KEYS = ("metrics", "dimensions", "grain", "filters", "start", "end", "order_by", "limit")
KINDS = ("query", "unanswerable", "clarify")


@dataclass
class EvalCase:
    name: str
    question: str
    expect_kind: str
    expect: dict
    history: list = field(default_factory=list)
    source: str = ""


def expand_paths(patterns: list[str], base: Path) -> list[Path]:
    """Files named by ``patterns`` (globs allowed), relative to ``base``."""
    files: list[Path] = []
    for pattern in patterns or ["tests/ask/*.yml"]:
        p = Path(pattern)
        full = p if p.is_absolute() else base / p
        matches = sorted(Path(m) for m in glob.glob(str(full)))
        if not matches and full.is_file():
            matches = [full]
        if not matches and full.is_dir():
            matches = sorted(list(full.glob("*.yml")) + list(full.glob("*.yaml")))
        for m in matches:
            if m.is_file() and m not in files:
                files.append(m)
    return files


def load_cases(paths: list[Path]) -> tuple[list[EvalCase], list[str]]:
    cases: list[EvalCase] = []
    errors: list[str] = []
    for path in paths:
        try:
            raw = yaml.safe_load(read_project_text(path)) or {}
        except Exception as e:
            errors.append(f"{path.name}: invalid YAML ({e})")
            continue
        entries = raw.get("cases") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            errors.append(f"{path.name}: expected a 'cases' list")
            continue
        for i, entry in enumerate(entries, 1):
            label = f"{path.name}#{i}"
            if not isinstance(entry, dict) or not str(entry.get("question") or "").strip():
                errors.append(f"{label}: each case needs a question")
                continue
            expect = entry.get("expect")
            if isinstance(expect, str):
                kind, expect = expect.strip().lower(), {}
            elif isinstance(expect, dict):
                expect = dict(expect)
                kind = str(expect.pop("kind", "query")).lower()
            else:
                errors.append(f"{label}: 'expect' must be a mapping or one of {', '.join(KINDS)}")
                continue
            if kind not in KINDS:
                errors.append(f"{label}: unknown expected kind {kind!r}")
                continue
            unknown = set(expect) - set(SPEC_KEYS)
            if unknown:
                errors.append(f"{label}: unknown expect keys {sorted(unknown)}")
                continue
            cases.append(EvalCase(
                name=str(entry.get("name") or label),
                question=str(entry["question"]).strip(),
                expect_kind=kind,
                expect=expect,
                history=list(entry.get("history") or []),
                source=path.name,
            ))
    return cases, errors


def _norm_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return tuple(sorted(str(x).lower() for x in v))
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return str(v).lower()


def _norm(key: str, value: Any) -> Any:
    if key in ("metrics", "dimensions"):
        if isinstance(value, str):
            value = [value]
        return sorted(str(v).lower() for v in (value or []))
    if key == "filters":
        out = []
        for f in value or []:
            if not isinstance(f, dict):
                continue
            op = str(f.get("op") or "=").lower()
            op = {"==": "=", "<>": "!=", "not_in": "not in"}.get(op, op)
            val = f.get("value")
            if op in ("in", "not in") and not isinstance(val, (list, tuple)):
                val = [val]
            out.append((str(f.get("dimension", "")).lower(), op, _norm_value(val)))
        return sorted(out, key=repr)
    if key == "order_by":
        out = []
        for t in value or []:
            if isinstance(t, str):
                name, _, d = t.partition(" ")
                out.append((name.lower(), (d or "desc").lower()))
            elif isinstance(t, dict):
                out.append((str(t.get("field", "")).lower(), str(t.get("direction") or "desc").lower()))
        return out
    if key == "limit":
        return int(value) if value not in (None, "") else None
    if value in (None, ""):
        return None
    return str(value).lower()


def compare(case: EvalCase, kind: str, spec: dict | None) -> list[str]:
    """Mismatches between what a case expects and what was planned."""
    if kind != case.expect_kind:
        return [f"expected {case.expect_kind}, got {kind}"]
    if kind != "query":
        return []
    mismatches = []
    spec = spec or {}
    for key, want in case.expect.items():
        got = spec.get(key)
        if _norm(key, want) != _norm(key, got):
            mismatches.append(f"{key}: expected {want!r}, got {got!r}")
    return mismatches


def run_eval(cases: list[EvalCase], ctx) -> dict:
    """Plan every case with ``ctx`` and score it. Returns a report dict."""
    from havn.engine.ai.ask import AskTurn, plan, prepare_catalog
    from havn.engine.ai.providers import ProviderError
    from havn.engine.semantic import load_metrics

    started = time.perf_counter()
    metrics, load_errors = load_metrics(ctx.project_dir)
    catalog = prepare_catalog(ctx, metrics).catalog
    results = []
    for case in cases:
        t0 = time.perf_counter()
        history = [t for t in (AskTurn.from_any(h) for h in case.history) if t is not None]
        entry: dict[str, Any] = {
            "name": case.name,
            "question": case.question,
            "source": case.source,
            "expected_kind": case.expect_kind,
            "expected": case.expect,
        }
        try:
            planned = plan(case.question, ctx, metrics, catalog, history)
            entry["kind"] = planned.kind if planned.spec is not None or planned.kind != "query" else "unanswerable"
            entry["spec"] = planned.spec.to_dict() if planned.spec else None
            entry["spec_errors"] = planned.errors
            entry["mismatches"] = compare(case, entry["kind"], entry["spec"])
        except ProviderError as e:
            entry["kind"] = "error"
            entry["spec"] = None
            entry["mismatches"] = [f"provider error: {e}"]
        entry["passed"] = not entry["mismatches"]
        entry["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        results.append(entry)
    passed = sum(1 for r in results if r["passed"])
    return {
        "provider": ctx.provider.describe(),
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "accuracy": (passed / len(results)) if results else None,
        "results": results,
        "metric_errors": load_errors,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }

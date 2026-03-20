"""Query plan parsing and structuring for EXPLAIN / EXPLAIN ANALYZE output."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("havn.engine.explain")


@dataclass
class PlanNode:
    """A single node in a query execution plan tree."""

    operator: str
    table: str | None = None
    estimated_rows: int | None = None
    actual_rows: int | None = None
    actual_time_ms: float | None = None
    extra_info: dict = field(default_factory=dict)
    children: list[PlanNode] = field(default_factory=list)


def _parse_json_node(node: dict, *, is_analyze: bool = False) -> PlanNode:
    """Parse a single JSON node from DuckDB's EXPLAIN (FORMAT JSON) output."""
    # EXPLAIN uses "name", EXPLAIN ANALYZE uses "operator_name"
    operator = node.get("operator_name") or node.get("name") or "UNKNOWN"
    operator = operator.strip()

    extra = dict(node.get("extra_info", {}))
    table = extra.pop("Table", None)

    # Estimated rows
    estimated_rows: int | None = None
    est_str = extra.pop("Estimated Cardinality", None)
    if est_str is not None:
        try:
            estimated_rows = int(est_str)
        except (ValueError, TypeError):
            pass

    # Actual rows / timing (EXPLAIN ANALYZE)
    actual_rows: int | None = None
    actual_time_ms: float | None = None
    if is_analyze:
        cardinality = node.get("operator_cardinality")
        if cardinality is not None:
            try:
                actual_rows = int(cardinality)
            except (ValueError, TypeError):
                pass
        timing = node.get("operator_timing")
        if timing is not None:
            try:
                actual_time_ms = round(float(timing) * 1000, 3)
            except (ValueError, TypeError):
                pass

    # Parse children
    children = []
    for child in node.get("children", []):
        children.append(_parse_json_node(child, is_analyze=is_analyze))

    return PlanNode(
        operator=operator,
        table=table,
        estimated_rows=estimated_rows,
        actual_rows=actual_rows,
        actual_time_ms=actual_time_ms,
        extra_info=extra,
        children=children,
    )


def _parse_json_plan(raw_json: str, *, is_analyze: bool = False) -> PlanNode:
    """Parse DuckDB JSON plan output into a PlanNode tree."""
    data = json.loads(raw_json)

    if isinstance(data, list):
        # EXPLAIN (FORMAT JSON) returns a list with one root
        if len(data) == 0:
            return PlanNode(operator="EMPTY")
        return _parse_json_node(data[0], is_analyze=is_analyze)
    elif isinstance(data, dict):
        # EXPLAIN ANALYZE with json profiling returns a dict with "children"
        # The root is a query-level wrapper; real plan is in children
        children = data.get("children", [])
        if children:
            # Skip the EXPLAIN_ANALYZE wrapper node if present
            root = _parse_json_node(children[0], is_analyze=is_analyze)
            if root.operator == "EXPLAIN_ANALYZE" and root.children:
                return root.children[0]
            return root
        return PlanNode(
            operator="QUERY",
            extra_info={
                k: v
                for k, v in data.items()
                if k not in ("children",) and v
            },
        )
    return PlanNode(operator="UNKNOWN")


def _parse_text_node(lines: list[str]) -> PlanNode:
    """Parse a single box-drawn node from DuckDB's text EXPLAIN output.

    Extracts operator name, table, row estimates, timing, and extra info
    from the box-drawing character format.
    """
    # Clean lines: strip box chars and whitespace
    clean = []
    for line in lines:
        stripped = line.strip()
        # Remove box-drawing borders
        stripped = re.sub(r"[│┌┐└┘├┤┬┴┼─╶╴╷╵╌╎┈┊]", "", stripped)
        stripped = stripped.strip()
        if stripped:
            clean.append(stripped)

    if not clean:
        return PlanNode(operator="UNKNOWN")

    operator = clean[0]
    table = None
    estimated_rows = None
    actual_rows = None
    actual_time_ms = None
    extra_info: dict = {}
    extra_lines: list[str] = []

    separator_seen = False
    for line in clean[1:]:
        if set(line) <= {"-", " ", "\u2500"}:
            separator_seen = True
            continue
        if not separator_seen:
            # Still part of operator name (multi-line)
            operator = operator + line
            continue

        # Table
        m = re.match(r"Table:\s*(.+)", line)
        if m:
            table = m.group(1).strip()
            continue

        # Timing (from EXPLAIN ANALYZE)
        m = re.match(r"\((\d+\.?\d*)(s|ms)\)", line)
        if m:
            val = float(m.group(1))
            if m.group(2) == "s":
                actual_time_ms = round(val * 1000, 3)
            else:
                actual_time_ms = round(val, 3)
            continue

        # Row count with ~ (estimated)
        m = re.match(r"~(\d+)\s*rows?", line)
        if m:
            estimated_rows = int(m.group(1))
            continue

        # Row count without ~ (actual from ANALYZE)
        m = re.match(r"(\d+)\s*rows?", line)
        if m:
            actual_rows = int(m.group(1))
            continue

        extra_lines.append(line)

    # Parse extra lines into key-value pairs where possible
    current_key = None
    current_values: list[str] = []
    for line in extra_lines:
        kv = re.match(r"(.+?):\s*(.+)", line)
        if kv:
            if current_key:
                extra_info[current_key] = (
                    current_values[0]
                    if len(current_values) == 1
                    else current_values
                )
            current_key = kv.group(1).strip()
            current_values = [kv.group(2).strip()]
        elif current_key and line.endswith(":"):
            # New section header like "Projections:"
            if current_key:
                extra_info[current_key] = (
                    current_values[0]
                    if len(current_values) == 1
                    else current_values
                )
            current_key = line[:-1].strip()
            current_values = []
        elif current_key:
            current_values.append(line)
        else:
            extra_info.setdefault("_details", [])
            extra_info["_details"].append(line)

    if current_key and current_values:
        extra_info[current_key] = (
            current_values[0] if len(current_values) == 1 else current_values
        )

    return PlanNode(
        operator=operator,
        table=table,
        estimated_rows=estimated_rows,
        actual_rows=actual_rows,
        actual_time_ms=actual_time_ms,
        extra_info=extra_info,
    )


def _parse_text_plan(raw_text: str) -> PlanNode:
    """Parse DuckDB's box-drawing EXPLAIN text into a PlanNode tree.

    This is a fallback for when JSON format is not available.
    It parses the indented box-drawing format by splitting on
    tree connectors (the vertical bars with horizontal branches).
    """
    # Split into individual node boxes
    lines = raw_text.split("\n")
    boxes: list[tuple[int, list[str]]] = []
    current_box: list[str] = []
    box_col = 0

    for line in lines:
        top_match = re.search(r"\u250C", line)  # ┌
        bottom_match = re.search(r"\u2514", line)  # └

        if top_match:
            if current_box:
                boxes.append((box_col, current_box))
            current_box = [line]
            box_col = top_match.start()
        elif current_box:
            current_box.append(line)
            if bottom_match:
                boxes.append((box_col, current_box))
                current_box = []

    if current_box:
        boxes.append((box_col, current_box))

    if not boxes:
        return PlanNode(operator="EMPTY")

    # Parse each box into a node
    nodes = [(_parse_text_node(box_lines), col) for col, box_lines in boxes]

    # Build tree from position relationships
    # DuckDB uses ┬ and ┴ connectors between parent and children
    if len(nodes) == 1:
        return nodes[0][0]

    # Simple heuristic: nodes are in top-down order, parent links via ├ or ┴
    # Build parent-child relationships based on connector lines
    root = nodes[0][0]
    stack: list[PlanNode] = [root]

    for i in range(1, len(nodes)):
        node = nodes[i][0]
        # Each subsequent node is a child of the most recent compatible parent
        if stack:
            stack[-1].children.append(node)
        stack.append(node)

    return root


def explain_query(conn, sql: str) -> tuple[PlanNode, str]:
    """Run EXPLAIN on a query and return (structured plan, raw text).

    Attempts JSON format first, falls back to text parsing.
    """
    raw_text = ""
    try:
        result = conn.execute(f"EXPLAIN (FORMAT JSON) {sql}")
        rows = result.fetchall()
        raw_json = rows[0][1] if rows and len(rows[0]) > 1 else rows[0][0]
        plan = _parse_json_plan(raw_json, is_analyze=False)

        # Also get text format for raw display
        result2 = conn.execute(f"EXPLAIN {sql}")
        rows2 = result2.fetchall()
        raw_text = "\n".join(
            str(r[1]) if len(r) > 1 else str(r[0]) for r in rows2
        )
        return plan, raw_text
    except Exception:
        logger.debug("JSON EXPLAIN failed, falling back to text", exc_info=True)

    # Fallback: text format
    result = conn.execute(f"EXPLAIN {sql}")
    rows = result.fetchall()
    raw_text = "\n".join(
        str(r[1]) if len(r) > 1 else str(r[0]) for r in rows
    )
    plan = _parse_text_plan(raw_text)
    return plan, raw_text


def explain_analyze_query(conn, sql: str) -> tuple[PlanNode, str]:
    """Run EXPLAIN ANALYZE on a query and return (structured plan, raw text).

    Enables JSON profiling, runs the query, then parses the result.
    """
    raw_text = ""
    try:
        conn.execute("PRAGMA enable_profiling='json'")
        result = conn.execute(f"EXPLAIN ANALYZE {sql}")
        rows = result.fetchall()
        raw_json = rows[0][1] if rows and len(rows[0]) > 1 else rows[0][0]

        plan = _parse_json_plan(raw_json, is_analyze=True)

        # Get text format for raw display
        try:
            conn.execute("PRAGMA enable_profiling='query_tree'")
            result2 = conn.execute(f"EXPLAIN ANALYZE {sql}")
            rows2 = result2.fetchall()
            raw_text = "\n".join(
                str(r[1]) if len(r) > 1 else str(r[0]) for r in rows2
            )
        except Exception:
            raw_text = raw_json

        return plan, raw_text
    except Exception:
        logger.debug(
            "JSON EXPLAIN ANALYZE failed, falling back to text", exc_info=True
        )

    # Fallback: text format
    try:
        conn.execute("PRAGMA enable_profiling='query_tree'")
    except Exception:
        pass
    result = conn.execute(f"EXPLAIN ANALYZE {sql}")
    rows = result.fetchall()
    raw_text = "\n".join(
        str(r[1]) if len(r) > 1 else str(r[0]) for r in rows
    )
    plan = _parse_text_plan(raw_text)
    return plan, raw_text


def plan_to_dict(node: PlanNode) -> dict:
    """Serialize a PlanNode tree to a JSON-compatible dict."""
    d: dict = {"operator": node.operator}
    if node.table:
        d["table"] = node.table
    if node.estimated_rows is not None:
        d["estimated_rows"] = node.estimated_rows
    if node.actual_rows is not None:
        d["actual_rows"] = node.actual_rows
    if node.actual_time_ms is not None:
        d["actual_time_ms"] = node.actual_time_ms
    if node.extra_info:
        d["extra_info"] = node.extra_info
    if node.children:
        d["children"] = [plan_to_dict(c) for c in node.children]
    return d


def _compute_total_time(node_dict: dict) -> float:
    """Compute total time across all nodes (for percentage calculations)."""
    total = node_dict.get("actual_time_ms", 0) or 0
    for child in node_dict.get("children", []):
        total += _compute_total_time(child)
    return total


def enrich_plan_dict(plan_dict: dict) -> dict:
    """Add computed fields like time_percentage to a plan dict."""
    total_time = _compute_total_time(plan_dict)
    if total_time > 0:
        _annotate_percentages(plan_dict, total_time)
    plan_dict["_total_time_ms"] = round(total_time, 3)
    return plan_dict


def _annotate_percentages(node: dict, total_time: float) -> None:
    """Recursively add time_percentage to each node."""
    t = node.get("actual_time_ms")
    if t is not None and total_time > 0:
        node["time_percentage"] = round((t / total_time) * 100, 1)
    for child in node.get("children", []):
        _annotate_percentages(child, total_time)

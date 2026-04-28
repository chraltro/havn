import React, { useState, useMemo, useRef, useCallback, useEffect } from "react";

/* ───────────────────────── type helpers ───────────────────────── */

function compareValues(a, b) {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  const numA = Number(a);
  const numB = Number(b);
  if (!isNaN(numA) && !isNaN(numB)) return numA - numB;
  return String(a).localeCompare(String(b));
}

const CLR = {
  int: "var(--havn-accent)",
  float: "var(--havn-accent)",
  text: "var(--havn-green)",
  bool: "var(--havn-purple)",
  temporal: "var(--havn-yellow)",
  other: "var(--havn-text-dim)",
};

function typeDisplay(dbType) {
  const t = dbType.toUpperCase().trim();
  if (/^(BIGINT|INT8)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(INTEGER|INT4|INT|SIGNED)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(SMALLINT|INT2|SHORT)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(TINYINT|INT1)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(UBIGINT)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(UINTEGER|UINT)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(USMALLINT)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(UTINYINT)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(HUGEINT|UHUGEINT)$/.test(t)) return { label: "123", color: CLR.int };
  if (/^(FLOAT|FLOAT4|REAL)$/.test(t)) return { label: "1.2", color: CLR.float };
  if (/^(DOUBLE|FLOAT8)$/.test(t)) return { label: "1.2", color: CLR.float };
  if (/^DECIMAL|^NUMERIC/.test(t)) return { label: "1.2", color: CLR.float };
  if (/^BOOL(EAN)?$/.test(t)) return { label: "T/F", color: CLR.bool };
  if (/^VARCHAR/.test(t)) return { label: "VARCHAR", color: CLR.text };
  if (/^TEXT$/.test(t)) return { label: "TEXT", color: CLR.text };
  if (/^CHAR/.test(t)) return { label: "CHAR", color: CLR.text };
  if (/^STRING$/.test(t)) return { label: "STRING", color: CLR.text };
  if (/^BLOB|^BYTEA$/.test(t)) return { label: "BLOB", color: CLR.text };
  if (/^UUID$/.test(t)) return { label: "UUID", color: CLR.text };
  if (/^ENUM/.test(t)) return { label: "ENUM", color: CLR.text };
  if (/^TIMESTAMP\s*WITH\s*TIME\s*ZONE|^TIMESTAMPTZ/.test(t)) return { label: "TIMESTAMPTZ", color: CLR.temporal };
  if (/^TIMESTAMP/.test(t)) return { label: "TIMESTAMP", color: CLR.temporal };
  if (/^DATETIME$/.test(t)) return { label: "DATETIME", color: CLR.temporal };
  if (/^DATE$/.test(t)) return { label: "DATE", color: CLR.temporal };
  if (/^TIME\s*WITH\s*TIME\s*ZONE|^TIMETZ/.test(t)) return { label: "TIMETZ", color: CLR.temporal };
  if (/^TIME$/.test(t)) return { label: "TIME", color: CLR.temporal };
  if (/^INTERVAL/.test(t)) return { label: "INTERVAL", color: CLR.temporal };
  if (/^JSON$/.test(t)) return { label: "JSON", color: CLR.other };
  if (/^STRUCT|^MAP/.test(t)) return { label: "{ }", color: CLR.other };
  if (/^LIST|^ARRAY|\[\]/.test(t)) return { label: "[ ]", color: CLR.other };
  if (/^UNION/.test(t)) return { label: "UNION", color: CLR.other };
  return { label: t.length > 10 ? t.slice(0, 10) : t, color: CLR.other };
}

/* Infer column type category from data sample */
const TYPE_NUMBER = "number";
const TYPE_DATE = "date";
const TYPE_BOOL = "boolean";
const TYPE_TEXT = "text";

function inferColumnType(rows, colIndex) {
  for (let i = 0; i < Math.min(rows.length, 20); i++) {
    const v = rows[i]?.[colIndex];
    if (v === null || v === undefined) continue;
    if (typeof v === "boolean" || v === "true" || v === "false") return TYPE_BOOL;
    const s = String(v).trim();
    if (s === "") continue;
    if (!isNaN(Number(s))) return TYPE_NUMBER;
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s)) return TYPE_DATE;
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return TYPE_DATE;
    return TYPE_TEXT;
  }
  return TYPE_TEXT;
}

// Map a DB type string ("VARCHAR", "BIGINT", "TIMESTAMP", ...) to the
// formatting category. The DB type is authoritative when supplied — without
// this, a VARCHAR column of numeric-looking strings (IDs, phone numbers,
// ZIP codes) gets thousands-separator formatting because the data sample
// passes !isNaN(Number(s)).
function dbTypeToCategory(dbType) {
  if (!dbType) return null;
  const t = String(dbType).toUpperCase().trim();
  if (/^(BIGINT|INT8|INTEGER|INT4|INT|SIGNED|SMALLINT|INT2|SHORT|TINYINT|INT1|UBIGINT|UINTEGER|UINT|USMALLINT|UTINYINT|HUGEINT|UHUGEINT|FLOAT|FLOAT4|REAL|DOUBLE|FLOAT8)$/.test(t))
    return TYPE_NUMBER;
  if (/^DECIMAL|^NUMERIC/.test(t)) return TYPE_NUMBER;
  if (/^BOOL(EAN)?$/.test(t)) return TYPE_BOOL;
  if (/^TIMESTAMP|^DATETIME$|^DATE$|^TIME/.test(t)) return TYPE_DATE;
  // Anything text-shaped (VARCHAR, TEXT, CHAR, STRING, UUID, ENUM) — and
  // anything we don't recognize — is treated as text so we don't apply
  // numeric formatting to it.
  return TYPE_TEXT;
}

// Columns whose values are numbers in the database but identifiers to a human:
// surrogate keys, postal codes, phone-like numbers, product codes. They
// should render as plain digits without thousand separators ("1234567" not
// "1,234,567"). Triggered purely by name; the underlying type stays numeric
// so sorting and aggregation still work.
const _ID_LIKE_NAME_RE =
  /(?:^|_)(id|ids|no|nr|num|number|code|zip|zipcode|postal|postcode|sku|isbn|ean|gtin|imei|ssn|tin|vat|cvr|orgnr|cprnr)(?:$|_)/i;

export function isIdentifierLikeName(name) {
  if (!name) return false;
  return _ID_LIKE_NAME_RE.test(String(name));
}

function inferTypeDisplay(rows, colIndex) {
  for (let i = 0; i < Math.min(rows.length, 20); i++) {
    const v = rows[i]?.[colIndex];
    if (v === null || v === undefined) continue;
    if (typeof v === "boolean" || v === "true" || v === "false")
      return { label: "T/F", color: CLR.bool };
    const s = String(v).trim();
    if (s === "") continue;
    if (!isNaN(Number(s))) {
      return s.includes(".") ? { label: "1.2", color: CLR.float } : { label: "123", color: CLR.int };
    }
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s))
      return { label: "TIMESTAMP", color: CLR.temporal };
    if (/^\d{4}-\d{2}-\d{2}$/.test(s))
      return { label: "DATE", color: CLR.temporal };
    if (/^\d{2}:\d{2}(:\d{2})?/.test(s))
      return { label: "TIME", color: CLR.temporal };
    return { label: "VARCHAR", color: CLR.text };
  }
  return { label: "VARCHAR", color: CLR.text };
}

/* ───────────────── formatting helpers ───────────────── */

const numFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
const dateFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const dateFmtShort = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });

function formatCell(value, colType) {
  if (value === null || value === undefined) return null;
  if (colType === TYPE_NUMBER) {
    const n = Number(value);
    if (!isNaN(n)) return numFmt.format(n);
  }
  if (colType === TYPE_DATE) {
    const d = new Date(value);
    if (!isNaN(d.getTime())) {
      const s = String(value);
      return s.includes("T") || s.includes(" ") ? dateFmt.format(d) : dateFmtShort.format(d);
    }
  }
  if (colType === TYPE_BOOL) {
    const s = String(value).toLowerCase();
    return s === "true" || s === "1" ? "true" : "false";
  }
  return String(value);
}

/* ───────────────── CSV export helper ───────────────── */

function downloadCSV(columns, rows) {
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return s.includes(",") || s.includes('"') || s.includes("\n") ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [columns.map(esc).join(",")];
  for (const row of rows) lines.push(row.map(esc).join(","));
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "export.csv";
  a.click();
  URL.revokeObjectURL(url);
}

/* ───────────────── grouping helpers ───────────────── */

function buildGroupTree(rows, groupColIndices, allColTypes) {
  if (!groupColIndices.length) return null;
  const root = { key: "__root__", label: "", depth: -1, children: [], rows: [], expanded: true };

  for (let ri = 0; ri < rows.length; ri++) {
    const row = rows[ri];
    let node = root;
    for (let gi = 0; gi < groupColIndices.length; gi++) {
      const ci = groupColIndices[gi];
      const val = row[ci] === null ? "(null)" : String(row[ci]);
      let child = node.children.find((c) => c.key === val);
      if (!child) {
        child = { key: val, label: val, depth: gi, children: [], rows: [], expanded: true, colIndex: ci };
        node.children.push(child);
      }
      node = child;
    }
    node.rows.push(ri);
  }

  /* Compute subtotals for numeric columns */
  function computeSubtotals(node, rows, colCount) {
    const subs = new Array(colCount).fill(null);
    const allRowIndices = collectRowIndices(node);
    for (let ci = 0; ci < colCount; ci++) {
      if (allColTypes[ci] !== TYPE_NUMBER) continue;
      let sum = 0;
      let count = 0;
      for (const ri of allRowIndices) {
        const v = Number(rows[ri][ci]);
        if (!isNaN(v)) { sum += v; count++; }
      }
      if (count > 0) subs[ci] = sum;
    }
    node.subtotals = subs;
    node.rowCount = allRowIndices.length;
    for (const ch of node.children) computeSubtotals(ch, rows, colCount);
  }

  function collectRowIndices(node) {
    let indices = [...node.rows];
    for (const ch of node.children) indices = indices.concat(collectRowIndices(ch));
    return indices;
  }

  computeSubtotals(root, rows, rows[0]?.length || 0);
  return root;
}

/* ───────────────── constants ───────────────── */

const ROW_HEIGHT = 28;
const VIRTUAL_THRESHOLD = 200;
const OVERSCAN = 5;

/* ───────────────── main component ───────────────── */

export default function SortableTable({
  columns,
  rows,
  columnTypes,
  maskedColumns,
  selectable,
  groupColumns,
  onCellClick,
  conditionalRules,
  showSummary,
}) {
  /* ─── state ─── */
  const [sortCols, setSortCols] = useState([]);
  const [columnOrder, setColumnOrder] = useState(null);
  const [columnWidths, setColumnWidths] = useState(null);
  const [hiddenCols, setHiddenCols] = useState(new Set());
  const [pinnedLeft, setPinnedLeft] = useState(new Set());
  const [pinnedRight, setPinnedRight] = useState(new Set());
  const [wrapText, setWrapText] = useState(false);
  const [showFilters, setShowFilters] = useState(false);
  const [columnFilters, setColumnFilters] = useState(new Map());
  const [copiedCell, setCopiedCell] = useState(null);
  const [selectedRows, setSelectedRows] = useState(new Set());
  const [showColumnMenu, setShowColumnMenu] = useState(false);
  const [contextMenu, setContextMenu] = useState(null);
  const [groupExpanded, setGroupExpanded] = useState({});

  /* ─── refs ─── */
  const scrollRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const copiedTimeout = useRef(null);
  const columnMenuRef = useRef(null);
  const contextMenuRef = useRef(null);
  const dragColRef = useRef(null);
  const resizeRef = useRef(null);

  /* ─── derived: type info ─── */
  const resolvedTypes = useMemo(() => {
    return columns.map((_, i) => {
      if (columnTypes && columnTypes[i]) return typeDisplay(columnTypes[i]);
      return inferTypeDisplay(rows, i);
    });
  }, [columns, rows, columnTypes]);

  const colTypes = useMemo(() => {
    return columns.map((col, i) => {
      const fromDb = columnTypes && dbTypeToCategory(columnTypes[i]);
      const base = fromDb || inferColumnType(rows, i);
      // Demote numeric columns whose name reads as an identifier (id, _no,
      // _number, _code, postal, ...) to TEXT for display. The underlying
      // value is still a number so sorting works, but formatCell will skip
      // thousand separators and the cell aligns left like other identifiers.
      if (base === TYPE_NUMBER && isIdentifierLikeName(col)) {
        return TYPE_TEXT;
      }
      return base;
    });
  }, [columns, rows, columnTypes]);

  /* ─── derived: summary row (sum for numeric, count for text) ─── */
  const summaryRow = useMemo(() => {
    if (!showSummary || rows.length === 0) return null;
    return columns.map((_, ci) => {
      if (colTypes[ci] === TYPE_NUMBER) {
        let sum = 0, count = 0;
        for (const row of rows) {
          const n = Number(row[ci]);
          if (!isNaN(n)) { sum += n; count++; }
        }
        return count > 0 ? sum : null;
      }
      return null;
    });
  }, [showSummary, rows, columns, colTypes]);

  /* ─── derived: column order (respecting reorder + visibility + pinning) ─── */
  const effectiveOrder = useMemo(() => {
    const base = columnOrder || columns.map((_, i) => i);
    const visible = base.filter((i) => !hiddenCols.has(i));
    const left = visible.filter((i) => pinnedLeft.has(i));
    const right = visible.filter((i) => pinnedRight.has(i));
    const center = visible.filter((i) => !pinnedLeft.has(i) && !pinnedRight.has(i));
    return [...left, ...center, ...right];
  }, [columnOrder, columns, hiddenCols, pinnedLeft, pinnedRight]);

  /* ─── derived: filtering ─── */
  const filteredRows = useMemo(() => {
    if (columnFilters.size === 0) return rows;
    return rows.filter((row) => {
      for (const [ci, filter] of columnFilters) {
        if (!filter) continue;
        const val = row[ci] === null ? "" : String(row[ci]).toLowerCase();
        if (!val.includes(filter.toLowerCase())) return false;
      }
      return true;
    });
  }, [rows, columnFilters]);

  /* ─── derived: sorting (multi-column) ─── */
  const sortedRows = useMemo(() => {
    if (sortCols.length === 0) return filteredRows;
    return [...filteredRows].sort((a, b) => {
      for (const { colIndex, dir } of sortCols) {
        const cmp = compareValues(a[colIndex], b[colIndex]);
        if (cmp !== 0) return dir === "desc" ? -cmp : cmp;
      }
      return 0;
    });
  }, [filteredRows, sortCols]);

  /* ─── derived: grouping ─── */
  const groupColIndices = useMemo(() => {
    if (!groupColumns || !groupColumns.length) return [];
    return groupColumns.map((name) => columns.indexOf(name)).filter((i) => i >= 0);
  }, [groupColumns, columns]);

  const groupTree = useMemo(() => {
    if (!groupColIndices.length) return null;
    return buildGroupTree(sortedRows, groupColIndices, colTypes);
  }, [sortedRows, groupColIndices, colTypes]);

  /* ─── handlers: multi-column sort ─── */
  const handleSort = useCallback((colIndex, shiftKey) => {
    setSortCols((prev) => {
      const existing = prev.findIndex((s) => s.colIndex === colIndex);
      if (shiftKey) {
        const next = [...prev];
        if (existing >= 0) {
          if (next[existing].dir === "asc") {
            next[existing] = { colIndex, dir: "desc" };
          } else {
            next.splice(existing, 1);
          }
        } else {
          next.push({ colIndex, dir: "asc" });
        }
        return next;
      }
      /* Non-shift: single column toggle */
      if (existing >= 0 && prev.length === 1) {
        if (prev[0].dir === "asc") return [{ colIndex, dir: "desc" }];
        return [];
      }
      return [{ colIndex, dir: "asc" }];
    });
  }, []);

  /* ─── handlers: column reorder via drag ─── */
  const handleDragStart = useCallback((e, colIndex) => {
    dragColRef.current = colIndex;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(colIndex));
  }, []);

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  const handleDrop = useCallback((e, targetIndex) => {
    e.preventDefault();
    const srcIndex = dragColRef.current;
    if (srcIndex === null || srcIndex === targetIndex) return;
    setColumnOrder((prev) => {
      const order = prev || columns.map((_, i) => i);
      const next = [...order];
      const srcPos = next.indexOf(srcIndex);
      const tgtPos = next.indexOf(targetIndex);
      if (srcPos < 0 || tgtPos < 0) return next;
      next.splice(srcPos, 1);
      next.splice(tgtPos, 0, srcIndex);
      return next;
    });
    dragColRef.current = null;
  }, [columns]);

  /* ─── handlers: column resize ─── */
  const handleResizeStart = useCallback((e, colIndex) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const currentWidths = columnWidths || columns.map(() => 150);
    const startWidth = currentWidths[colIndex];

    const onMove = (me) => {
      const delta = me.clientX - startX;
      const newWidth = Math.max(40, startWidth + delta);
      setColumnWidths((prev) => {
        const ws = prev ? [...prev] : columns.map(() => 150);
        ws[colIndex] = newWidth;
        return ws;
      });
    };
    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  }, [columnWidths, columns]);

  /* ─── handlers: context menu (pinning) ─── */
  const handleHeaderContext = useCallback((e, colIndex) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, colIndex });
  }, []);

  const handlePin = useCallback((colIndex, side) => {
    if (side === "left") {
      setPinnedLeft((prev) => { const s = new Set(prev); s.add(colIndex); return s; });
      setPinnedRight((prev) => { const s = new Set(prev); s.delete(colIndex); return s; });
    } else if (side === "right") {
      setPinnedRight((prev) => { const s = new Set(prev); s.add(colIndex); return s; });
      setPinnedLeft((prev) => { const s = new Set(prev); s.delete(colIndex); return s; });
    } else {
      setPinnedLeft((prev) => { const s = new Set(prev); s.delete(colIndex); return s; });
      setPinnedRight((prev) => { const s = new Set(prev); s.delete(colIndex); return s; });
    }
    /* Ensure column widths exist when pinning */
    if (side !== "unpin") {
      setColumnWidths((prev) => prev || columns.map(() => 150));
    }
    setContextMenu(null);
  }, [columns]);

  /* ─── handlers: cell click to copy ─── */
  const handleCellClick = useCallback((rowIdx, colIdx, value) => {
    if (onCellClick) onCellClick(rowIdx, colIdx, value);
    const text = value === null || value === undefined ? "" : String(value);
    navigator.clipboard.writeText(text).catch(() => {});
    setCopiedCell({ row: rowIdx, col: colIdx });
    if (copiedTimeout.current) clearTimeout(copiedTimeout.current);
    copiedTimeout.current = setTimeout(() => setCopiedCell(null), 500);
  }, [onCellClick]);

  /* ─── handlers: row selection ─── */
  const handleSelectRow = useCallback((rowIdx) => {
    setSelectedRows((prev) => {
      const s = new Set(prev);
      if (s.has(rowIdx)) s.delete(rowIdx);
      else s.add(rowIdx);
      return s;
    });
  }, []);

  const handleSelectAll = useCallback(() => {
    setSelectedRows((prev) => {
      if (prev.size === sortedRows.length) return new Set();
      return new Set(sortedRows.map((_, i) => i));
    });
  }, [sortedRows]);

  /* ─── handlers: export selected ─── */
  const handleExportSelected = useCallback(() => {
    const visibleCols = effectiveOrder;
    const colNames = visibleCols.map((i) => columns[i]);
    const selectedData = [...selectedRows].sort((a, b) => a - b).map((ri) => {
      return visibleCols.map((ci) => sortedRows[ri][ci]);
    });
    downloadCSV(colNames, selectedData);
  }, [selectedRows, sortedRows, effectiveOrder, columns]);

  /* ─── close menus on outside click ─── */
  useEffect(() => {
    const handler = (e) => {
      if (showColumnMenu && columnMenuRef.current && !columnMenuRef.current.contains(e.target)) {
        setShowColumnMenu(false);
      }
      if (contextMenu && contextMenuRef.current && !contextMenuRef.current.contains(e.target)) {
        setContextMenu(null);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showColumnMenu, contextMenu]);

  /* ─── virtual scroll handler ─── */
  const handleScroll = useCallback((e) => {
    setScrollTop(e.target.scrollTop);
  }, []);

  /* ─── compute pinned offsets ─── */
  const pinnedOffsets = useMemo(() => {
    const offsets = {};
    const ws = columnWidths || columns.map(() => 150);
    /* left offsets */
    let leftAcc = 0;
    for (const ci of effectiveOrder) {
      if (pinnedLeft.has(ci)) {
        offsets[ci] = { side: "left", offset: leftAcc };
        leftAcc += ws[ci] || 150;
      }
    }
    /* right offsets */
    let rightAcc = 0;
    for (let k = effectiveOrder.length - 1; k >= 0; k--) {
      const ci = effectiveOrder[k];
      if (pinnedRight.has(ci)) {
        offsets[ci] = { side: "right", offset: rightAcc };
        rightAcc += ws[ci] || 150;
      }
    }
    return offsets;
  }, [effectiveOrder, pinnedLeft, pinnedRight, columnWidths, columns]);

  const useVirtual = sortedRows.length > VIRTUAL_THRESHOLD && !groupTree;
  // Virtualized rendering puts the header in one <table> and the body rows
  // in a separate <table>. Browsers compute auto column widths from each
  // table's own content, so the two tables drift apart and headers stop
  // lining up over the data. Force tableLayout: fixed (with a default width
  // when the user hasn't resized) for the virtual path so both tables share
  // the same column geometry.
  const hasFixedWidths = columnWidths !== null || useVirtual;
  const fixedWidths = useMemo(
    () => columnWidths || columns.map(() => 150),
    [columnWidths, columns],
  );
  const showSelectCol = selectable === true;

  /* ─── group toggle ─── */
  const toggleGroup = useCallback((key) => {
    setGroupExpanded((prev) => ({ ...prev, [key]: prev[key] === undefined ? false : !prev[key] }));
  }, []);

  /* ─── flatten group tree for rendering ─── */
  const flatGroupRows = useMemo(() => {
    if (!groupTree) return null;
    const flat = [];
    function walk(node, path) {
      if (node.depth >= 0) {
        const key = path;
        const expanded = groupExpanded[key] === undefined ? true : groupExpanded[key];
        flat.push({ type: "group", node, key, expanded, depth: node.depth });
        if (!expanded) return;
      }
      for (const ch of node.children) walk(ch, path + "/" + ch.key);
      for (const ri of node.rows) flat.push({ type: "row", rowIndex: ri, depth: node.depth + 1 });
    }
    walk(groupTree, "");
    return flat;
  }, [groupTree, groupExpanded]);

  /* ─── render helpers ─── */

  function renderHeaderCell(ci, posInOrder) {
    const sym = resolvedTypes[ci];
    const sortEntry = sortCols.find((s) => s.colIndex === ci);
    const sortIndex = sortEntry ? sortCols.indexOf(sortEntry) : -1;
    const isActive = sortEntry != null;
    const isPinnedL = pinnedLeft.has(ci);
    const isPinnedR = pinnedRight.has(ci);
    const pinInfo = pinnedOffsets[ci];

    const thStyle = {
      ...S.th,
      cursor: "pointer",
      userSelect: "none",
      width: hasFixedWidths ? fixedWidths[ci] : undefined,
      minWidth: hasFixedWidths ? fixedWidths[ci] : undefined,
      position: pinInfo ? "sticky" : "sticky",
      top: 0,
      left: pinInfo?.side === "left" ? pinInfo.offset : undefined,
      right: pinInfo?.side === "right" ? pinInfo.offset : undefined,
      zIndex: pinInfo ? 3 : 2,
      background: "var(--havn-bg)",
      borderLeft: isPinnedL ? "2px solid var(--havn-accent)" : undefined,
      borderRight: isPinnedR ? "2px solid var(--havn-accent)" : undefined,
    };

    return (
      <th
        key={ci}
        style={thStyle}
        onClick={(e) => handleSort(ci, e.shiftKey)}
        onContextMenu={(e) => handleHeaderContext(e, ci)}
        draggable
        onDragStart={(e) => handleDragStart(e, ci)}
        onDragOver={handleDragOver}
        onDrop={(e) => handleDrop(e, ci)}
        role="columnheader"
        scope="col"
        aria-sort={isActive ? (sortEntry.dir === "asc" ? "ascending" : "descending") : "none"}
      >
        <span style={S.thInner}>
          {maskedColumns && maskedColumns[columns[ci]] && (
            <span style={S.maskIcon} title={`Masked: ${maskedColumns[columns[ci]]}`}>&#x1F6E1;</span>
          )}
          <span>{columns[ci]}</span>
          <span style={{ ...S.typeSymbol, color: sym.color }}>{sym.label}</span>
          <span style={{ ...S.sortIcon, color: isActive ? "var(--havn-accent)" : "var(--havn-text-dim)" }}>
            {isActive
              ? (sortEntry.dir === "asc" ? "\u25B2" : "\u25BC") + (sortCols.length > 1 ? String(sortIndex + 1) : "")
              : "\u25B4\u25BE"}
          </span>
        </span>
        {/* Resize handle */}
        <div
          style={S.resizeHandle}
          onPointerDown={(e) => handleResizeStart(e, ci)}
          onClick={(e) => e.stopPropagation()}
        />
      </th>
    );
  }

  function renderFilterRow() {
    return (
      <tr>
        {showSelectCol && <th style={{ ...S.th, padding: "2px 4px" }} />}
        {effectiveOrder.map((ci) => {
          const pinInfo = pinnedOffsets[ci];
          return (
            <th
              key={ci}
              style={{
                ...S.th,
                padding: "2px 4px",
                top: ROW_HEIGHT + 2,
                position: pinInfo ? "sticky" : "sticky",
                left: pinInfo?.side === "left" ? pinInfo.offset : undefined,
                right: pinInfo?.side === "right" ? pinInfo.offset : undefined,
                zIndex: pinInfo ? 3 : 2,
                background: "var(--havn-bg)",
              }}
            >
              <input
                type="text"
                placeholder="Filter..."
                value={columnFilters.get(ci) || ""}
                onChange={(e) => {
                  setColumnFilters((prev) => {
                    const next = new Map(prev);
                    if (e.target.value) next.set(ci, e.target.value);
                    else next.delete(ci);
                    return next;
                  });
                }}
                onClick={(e) => e.stopPropagation()}
                style={S.filterInput}
              />
            </th>
          );
        })}
      </tr>
    );
  }

  function renderCell(row, ci, rowIdx, isOddRow) {
    const value = row[ci];
    const ct = colTypes[ci];
    const formatted = formatCell(value, ct);
    const isCopied = copiedCell && copiedCell.row === rowIdx && copiedCell.col === ci;
    const isNumeric = ct === TYPE_NUMBER;
    const pinInfo = pinnedOffsets[ci];

    // Conditional formatting: check rules for this column/value
    let condBg = null;
    let condColor = null;
    if (conditionalRules?.length > 0 && value !== null && value !== undefined) {
      const n = Number(value);
      const colName = columns[ci];
      if (!isNaN(n)) {
        for (const rule of conditionalRules) {
          // Match if rule targets this column (by name) or applies to all (empty column)
          if (rule.column && rule.column !== colName) continue;
          const rv = Number(rule.value);
          if (isNaN(rv)) continue;
          let match = false;
          switch (rule.op) {
            case ">": match = n > rv; break;
            case "<": match = n < rv; break;
            case ">=": match = n >= rv; break;
            case "<=": match = n <= rv; break;
            case "=": match = n === rv; break;
            default: break;
          }
          if (match) {
            condBg = rule.bgColor || "#22c55e";
            // Auto-contrast text color
            const hex = condBg.replace("#", "");
            const r = parseInt(hex.substring(0, 2), 16) / 255;
            const g = parseInt(hex.substring(2, 4), 16) / 255;
            const b = parseInt(hex.substring(4, 6), 16) / 255;
            const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
            condColor = lum > 0.4 ? "#000" : "#fff";
            break;
          }
        }
      }
    }

    const style = {
      ...S.td,
      textAlign: isNumeric ? "right" : "left",
      whiteSpace: wrapText ? "normal" : "nowrap",
      backgroundColor: condBg
        ? condBg + "33" // 20% opacity background
        : isCopied
          ? "rgba(99,102,241,0.2)"
          : isOddRow
            ? "rgba(128,128,128,0.04)"
            : "transparent",
      color: condBg ? condBg : undefined,
      fontWeight: condBg ? 600 : undefined,
      transition: isCopied ? "background-color 0.5s" : undefined,
      width: hasFixedWidths ? fixedWidths[ci] : undefined,
      minWidth: hasFixedWidths ? fixedWidths[ci] : undefined,
      position: pinInfo ? "sticky" : undefined,
      left: pinInfo?.side === "left" ? pinInfo.offset : undefined,
      right: pinInfo?.side === "right" ? pinInfo.offset : undefined,
      zIndex: pinInfo ? 1 : undefined,
      background: pinInfo && !condBg ? "var(--havn-bg)" : undefined,
    };

    return (
      <td
        key={ci}
        style={style}
        title={value === null ? "" : String(value)}
        onClick={() => handleCellClick(rowIdx, ci, value)}
      >
        {value === null ? <span style={S.null}>NULL</span> : formatted}
      </td>
    );
  }

  function renderDataRow(row, rowIdx, isOddRow, extraPadding) {
    return (
      <tr
        key={rowIdx}
        style={{
          height: useVirtual ? ROW_HEIGHT : undefined,
          ...S.dataRow,
        }}
      >
        {showSelectCol && (
          <td style={{ ...S.td, width: 32, minWidth: 32, textAlign: "center", paddingLeft: extraPadding || undefined }}>
            <input
              type="checkbox"
              checked={selectedRows.has(rowIdx)}
              onChange={() => handleSelectRow(rowIdx)}
              style={{ cursor: "pointer" }}
            />
          </td>
        )}
        {effectiveOrder.map((ci, posIdx) => {
          const cell = renderCell(row, ci, rowIdx, isOddRow);
          if (posIdx === 0 && extraPadding) {
            return React.cloneElement(cell, {
              key: ci,
              style: { ...cell.props.style, paddingLeft: `${12 + extraPadding}px` },
            });
          }
          return cell;
        })}
      </tr>
    );
  }

  function renderGroupRows() {
    if (!flatGroupRows) return null;
    return flatGroupRows.map((item, idx) => {
      if (item.type === "group") {
        const { node, key, expanded, depth } = item;
        const indent = depth * 20;
        return (
          <tr key={"g-" + key} style={{ background: "rgba(128,128,128,0.06)" }}>
            {showSelectCol && <td style={S.td} />}
            <td
              style={{ ...S.td, paddingLeft: `${12 + indent}px`, fontWeight: 600, cursor: "pointer" }}
              onClick={() => toggleGroup(key)}
              colSpan={1}
            >
              <span style={{ marginRight: 6, fontSize: 10 }}>{expanded ? "\u25BC" : "\u25B6"}</span>
              {node.label}
              <span style={{ ...S.typeSymbol, marginLeft: 8 }}>({node.rowCount})</span>
            </td>
            {effectiveOrder.slice(1).map((ci) => (
              <td key={ci} style={{ ...S.td, color: "var(--havn-text-dim)", fontSize: 11, textAlign: colTypes[ci] === TYPE_NUMBER ? "right" : "left" }}>
                {node.subtotals[ci] !== null ? numFmt.format(node.subtotals[ci]) : ""}
              </td>
            ))}
          </tr>
        );
      }
      const { rowIndex, depth } = item;
      return renderDataRow(sortedRows[rowIndex], rowIndex, rowIndex % 2 === 1, depth * 20);
    });
  }

  /* ─── virtual body ─── */
  function renderVirtualBody() {
    const totalHeight = sortedRows.length * ROW_HEIGHT;
    const containerHeight = scrollRef.current?.clientHeight || 400;
    const startIdx = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
    const endIdx = Math.min(sortedRows.length, Math.ceil((scrollTop + containerHeight) / ROW_HEIGHT) + OVERSCAN);

    return (
      <div
        ref={scrollRef}
        style={{ overflowY: "auto", maxHeight: "calc(100vh - 200px)", position: "relative" }}
        onScroll={handleScroll}
      >
        <table style={{ ...S.table, tableLayout: hasFixedWidths ? "fixed" : undefined }}>
          <thead style={{ position: "sticky", top: 0, zIndex: 4 }}>
            <tr>
              {showSelectCol && (
                <th style={{ ...S.th, width: 32, minWidth: 32, textAlign: "center" }}>
                  <input type="checkbox" checked={selectedRows.size === sortedRows.length && sortedRows.length > 0} onChange={handleSelectAll} style={{ cursor: "pointer" }} />
                </th>
              )}
              {effectiveOrder.map((ci, pos) => renderHeaderCell(ci, pos))}
            </tr>
            {showFilters && renderFilterRow()}
          </thead>
        </table>
        <div style={{ height: totalHeight, position: "relative" }}>
          <table style={{ ...S.table, tableLayout: hasFixedWidths ? "fixed" : undefined, position: "absolute", top: startIdx * ROW_HEIGHT, width: "100%" }}>
            <tbody>
              {sortedRows.slice(startIdx, endIdx).map((row, i) => {
                const actualIdx = startIdx + i;
                return renderDataRow(row, actualIdx, actualIdx % 2 === 1);
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  /* ─── regular body ─── */
  function renderRegularBody() {
    return (
      <div style={{ overflowX: "auto" }}>
        <table style={{ ...S.table, tableLayout: hasFixedWidths ? "fixed" : undefined }}>
          <thead>
            <tr>
              {showSelectCol && (
                <th style={{ ...S.th, width: 32, minWidth: 32, textAlign: "center", position: "sticky", top: 0, zIndex: 2, background: "var(--havn-bg)" }}>
                  <input type="checkbox" checked={selectedRows.size === sortedRows.length && sortedRows.length > 0} onChange={handleSelectAll} style={{ cursor: "pointer" }} />
                </th>
              )}
              {effectiveOrder.map((ci, pos) => renderHeaderCell(ci, pos))}
            </tr>
            {showFilters && renderFilterRow()}
          </thead>
          <tbody>
            {groupTree
              ? renderGroupRows()
              : sortedRows.map((row, i) => renderDataRow(row, i, i % 2 === 1))}
          </tbody>
          {summaryRow && (
            <tfoot>
              <tr style={{ borderTop: "2px solid var(--havn-border)", fontWeight: 700, fontSize: 11 }}>
                {showSelectCol && <td style={S.td} />}
                {effectiveOrder.map((ci) => (
                  <td key={ci} style={{ ...S.td, textAlign: colTypes[ci] === TYPE_NUMBER ? "right" : "left", color: "var(--havn-text-secondary)" }}>
                    {summaryRow[ci] !== null ? numFmt.format(summaryRow[ci]) : (ci === effectiveOrder[0] ? "Total" : "")}
                  </td>
                ))}
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    );
  }

  /* ─── toolbar ─── */
  function renderToolbar() {
    return (
      <div style={S.toolbar}>
        <button
          style={S.toolbarBtn}
          onClick={() => setShowFilters((v) => !v)}
          title="Toggle column filters"
        >
          {showFilters ? "Hide Filters" : "Filters"}
        </button>
        <button
          style={S.toolbarBtn}
          onClick={() => setWrapText((v) => !v)}
          title="Toggle text wrapping"
        >
          {wrapText ? "No Wrap" : "Wrap"}
        </button>
        {showSelectCol && selectedRows.size > 0 && (
          <button style={{ ...S.toolbarBtn, color: "var(--havn-accent)" }} onClick={handleExportSelected}>
            Export Selected ({selectedRows.size})
          </button>
        )}
        <div style={{ marginLeft: "auto", position: "relative" }}>
          <button
            style={S.toolbarBtn}
            onClick={() => setShowColumnMenu((v) => !v)}
            title="Column visibility"
          >
            &#9881;
          </button>
          {showColumnMenu && (
            <div ref={columnMenuRef} style={S.columnMenu}>
              {columns.map((col, i) => (
                <label key={i} style={S.columnMenuItem}>
                  <input
                    type="checkbox"
                    checked={!hiddenCols.has(i)}
                    onChange={() => {
                      setHiddenCols((prev) => {
                        const s = new Set(prev);
                        if (s.has(i)) s.delete(i);
                        else s.add(i);
                        return s;
                      });
                    }}
                  />
                  <span style={{ marginLeft: 6 }}>{col}</span>
                </label>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  /* ─── context menu (pinning) ─── */
  function renderContextMenu() {
    if (!contextMenu) return null;
    const { x, y, colIndex } = contextMenu;
    const isPinned = pinnedLeft.has(colIndex) || pinnedRight.has(colIndex);
    return (
      <div
        ref={contextMenuRef}
        style={{ ...S.contextMenu, left: x, top: y }}
      >
        <div style={S.contextMenuItem} onClick={() => handlePin(colIndex, "left")}>Pin Left</div>
        <div style={S.contextMenuItem} onClick={() => handlePin(colIndex, "right")}>Pin Right</div>
        {isPinned && <div style={S.contextMenuItem} onClick={() => handlePin(colIndex, "unpin")}>Unpin</div>}
      </div>
    );
  }

  return (
    <div style={{ position: "relative" }}>
      {renderToolbar()}
      {useVirtual ? renderVirtualBody() : renderRegularBody()}
      {renderContextMenu()}
    </div>
  );
}

/* ───────────────── styles (all var(--havn-*)) ───────────────── */

const S = {
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: "12px",
    fontFamily: "var(--havn-font-mono)",
  },
  th: {
    textAlign: "left",
    padding: "6px 12px",
    borderBottom: "2px solid var(--havn-border-light)",
    color: "var(--havn-text-secondary)",
    fontWeight: 600,
    background: "var(--havn-bg)",
    position: "relative",
    whiteSpace: "nowrap",
  },
  thInner: {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    width: "100%",
  },
  maskIcon: { fontSize: "10px", opacity: 0.7, flexShrink: 0 },
  typeSymbol: { fontSize: "9px", fontWeight: 500, opacity: 0.8 },
  sortIcon: { fontSize: "8px", lineHeight: 1, marginLeft: "auto", flexShrink: 0 },
  td: {
    padding: "4px 12px",
    borderBottom: "1px solid var(--havn-border)",
    color: "var(--havn-text)",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
    maxWidth: "300px",
    cursor: "pointer",
  },
  null: { color: "var(--havn-text-dim)", fontStyle: "italic", fontSize: "11px" },
  dataRow: {},
  resizeHandle: {
    position: "absolute",
    right: 0,
    top: 0,
    bottom: 0,
    width: 5,
    cursor: "col-resize",
    zIndex: 5,
  },
  toolbar: {
    display: "flex",
    alignItems: "center",
    gap: "4px",
    padding: "4px 8px",
    borderBottom: "1px solid var(--havn-border)",
    fontSize: "11px",
    fontFamily: "var(--havn-font)",
    background: "var(--havn-bg)",
  },
  toolbarBtn: {
    background: "none",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius, 4px)",
    color: "var(--havn-text-secondary)",
    padding: "2px 8px",
    cursor: "pointer",
    fontSize: "11px",
    fontFamily: "var(--havn-font)",
  },
  filterInput: {
    width: "100%",
    boxSizing: "border-box",
    padding: "2px 4px",
    fontSize: "11px",
    fontFamily: "var(--havn-font-mono)",
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius, 4px)",
    color: "var(--havn-text)",
    outline: "none",
  },
  columnMenu: {
    position: "absolute",
    right: 0,
    top: "100%",
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius, 4px)",
    padding: "4px 0",
    zIndex: 100,
    minWidth: 160,
    maxHeight: 300,
    overflowY: "auto",
    boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
  },
  columnMenuItem: {
    display: "flex",
    alignItems: "center",
    padding: "4px 12px",
    cursor: "pointer",
    fontSize: "11px",
    color: "var(--havn-text)",
    whiteSpace: "nowrap",
  },
  contextMenu: {
    position: "fixed",
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius, 4px)",
    padding: "4px 0",
    zIndex: 200,
    minWidth: 120,
    boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
  },
  contextMenuItem: {
    padding: "6px 14px",
    cursor: "pointer",
    fontSize: "12px",
    color: "var(--havn-text)",
    whiteSpace: "nowrap",
  },
};

/* Inject hover style via a global stylesheet (only once) */
if (typeof document !== "undefined" && !document.getElementById("havn-sortable-table-styles")) {
  const sheet = document.createElement("style");
  sheet.id = "havn-sortable-table-styles";
  sheet.textContent = `
    .havn-st-ctx-item:hover { background: rgba(99,102,241,0.1); }
  `;
  /* Row hover */
  sheet.textContent += `
    table tr:hover td { background-color: rgba(99,102,241,0.06) !important; }
  `;
  document.head.appendChild(sheet);
}

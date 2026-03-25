import React, { useState, useMemo } from "react";
import { fmtNum } from "./chartUtils";

/**
 * Pivot table with row grouping, sorting, conditional formatting, and CSV export.
 * Extends the SortableTable pattern for dashboard use.
 */

export default function PivotTable({ columns, rows, config }) {
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("asc");
  const [collapsedGroups, setCollapsedGroups] = useState(new Set());

  const groupCol = config?.group_column || null;
  const groupIdx = groupCol ? columns.indexOf(groupCol) : -1;

  // Determine which columns are numeric
  const numericCols = useMemo(() => {
    const result = new Set();
    if (rows.length === 0) return result;
    for (let ci = 0; ci < columns.length; ci++) {
      let numCount = 0;
      const sample = Math.min(rows.length, 50);
      for (let ri = 0; ri < sample; ri++) {
        const v = rows[ri][ci];
        if (v !== null && v !== undefined && v !== "" && !isNaN(Number(v))) numCount++;
      }
      if (numCount / sample > 0.5) result.add(ci);
    }
    return result;
  }, [columns, rows]);

  // Sort rows
  const sorted = useMemo(() => {
    if (sortCol === null) return rows;
    const colIdx = columns.indexOf(sortCol);
    if (colIdx < 0) return rows;
    const isNum = numericCols.has(colIdx);
    return [...rows].sort((a, b) => {
      const va = a[colIdx], vb = b[colIdx];
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      let cmp;
      if (isNum) cmp = Number(va) - Number(vb);
      else cmp = String(va).localeCompare(String(vb));
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [rows, sortCol, sortDir, columns, numericCols]);

  // Group rows if group column specified
  const groups = useMemo(() => {
    if (groupIdx < 0) return null;
    const map = new Map();
    for (const row of sorted) {
      const key = String(row[groupIdx] ?? "(empty)");
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(row);
    }
    return map;
  }, [sorted, groupIdx]);

  // Compute subtotals per group
  function computeSubtotals(groupRows) {
    return columns.map((_, ci) => {
      if (!numericCols.has(ci)) return "";
      let sum = 0;
      for (const row of groupRows) {
        const v = Number(row[ci]);
        if (!isNaN(v)) sum += v;
      }
      return sum;
    });
  }

  // Conditional formatting
  const thresholds = config?.thresholds || null; // { column, low, high }

  function cellStyle(colIdx, value) {
    if (!thresholds || columns[colIdx] !== thresholds.column) return {};
    const n = Number(value);
    if (isNaN(n)) return {};
    if (n <= thresholds.low) return { color: "var(--havn-red)" };
    if (n >= thresholds.high) return { color: "var(--havn-green)" };
    return {};
  }

  function toggleSort(col) {
    if (sortCol === col) {
      if (sortDir === "asc") setSortDir("desc");
      else { setSortCol(null); setSortDir("asc"); }
    } else {
      setSortCol(col);
      setSortDir("asc");
    }
  }

  function toggleGroup(key) {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // Export CSV
  function exportCSV() {
    const header = columns.join(",");
    const body = rows.map(r => r.map(v => {
      const s = String(v ?? "");
      return s.includes(",") || s.includes('"') ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(",")).join("\n");
    const blob = new Blob([header + "\n" + body], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "pivot-export.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div style={st.container}>
      <div style={st.toolbar}>
        <span style={st.rowCount}>{rows.length} row{rows.length !== 1 ? "s" : ""}</span>
        <button style={st.exportBtn} onClick={exportCSV} title="Export CSV">Export</button>
      </div>
      <div style={st.tableWrap}>
        <table style={st.table}>
          <thead>
            <tr>
              {columns.map((col, ci) => (
                <th
                  key={ci}
                  style={{
                    ...st.th,
                    cursor: "pointer",
                    textAlign: numericCols.has(ci) ? "right" : "left",
                  }}
                  onClick={() => toggleSort(col)}
                >
                  {col}
                  {sortCol === col && (
                    <span style={{ marginLeft: 4, fontSize: 10 }}>
                      {sortDir === "asc" ? "▲" : "▼"}
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups ? (
              Array.from(groups.entries()).map(([key, groupRows]) => {
                const collapsed = collapsedGroups.has(key);
                const subtotals = computeSubtotals(groupRows);
                return (
                  <React.Fragment key={key}>
                    <tr style={st.groupRow} onClick={() => toggleGroup(key)}>
                      <td colSpan={columns.length} style={st.groupCell}>
                        <span style={{ marginRight: 6, fontSize: 10 }}>{collapsed ? "▶" : "▼"}</span>
                        {key} ({groupRows.length})
                        {/* Inline subtotals */}
                        {numericCols.size > 0 && (
                          <span style={st.subtotalInline}>
                            {columns.map((_, ci) => numericCols.has(ci) ? `${columns[ci]}: ${fmtNum(subtotals[ci])}` : null).filter(Boolean).join(" | ")}
                          </span>
                        )}
                      </td>
                    </tr>
                    {!collapsed && groupRows.map((row, ri) => (
                      <tr key={ri} style={st.dataRow}>
                        {row.map((cell, ci) => (
                          <td key={ci} style={{ ...st.td, textAlign: numericCols.has(ci) ? "right" : "left", ...cellStyle(ci, cell) }}>
                            {cell === null || cell === undefined ? <span style={st.null}>NULL</span> : numericCols.has(ci) ? fmtNum(cell) : String(cell)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </React.Fragment>
                );
              })
            ) : (
              sorted.map((row, ri) => (
                <tr key={ri} style={st.dataRow}>
                  {row.map((cell, ci) => (
                    <td key={ci} style={{ ...st.td, textAlign: numericCols.has(ci) ? "right" : "left", ...cellStyle(ci, cell) }}>
                      {cell === null || cell === undefined ? <span style={st.null}>NULL</span> : numericCols.has(ci) ? fmtNum(cell) : String(cell)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const st = {
  container: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    overflow: "hidden",
  },
  toolbar: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "4px 8px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
  },
  rowCount: {
    fontSize: 11,
    color: "var(--havn-text-secondary)",
  },
  exportBtn: {
    background: "none",
    border: "1px solid var(--havn-border)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    padding: "2px 8px",
    borderRadius: 4,
    fontSize: 11,
  },
  tableWrap: {
    flex: 1,
    overflow: "auto",
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontFamily: "var(--havn-font-mono)",
    fontSize: 12,
  },
  th: {
    position: "sticky",
    top: 0,
    background: "var(--havn-bg)",
    padding: "6px 10px",
    borderBottom: "2px solid var(--havn-border)",
    fontWeight: 600,
    color: "var(--havn-text)",
    fontSize: 11,
    whiteSpace: "nowrap",
    userSelect: "none",
  },
  td: {
    padding: "4px 10px",
    borderBottom: "1px solid var(--havn-border)",
    color: "var(--havn-text)",
    maxWidth: 200,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  dataRow: {},
  groupRow: {
    cursor: "pointer",
  },
  groupCell: {
    padding: "6px 10px",
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    fontWeight: 600,
    fontSize: 12,
    color: "var(--havn-text)",
    borderBottom: "1px solid var(--havn-border)",
  },
  subtotalInline: {
    marginLeft: 16,
    fontSize: 11,
    fontWeight: 400,
    color: "var(--havn-text-secondary)",
  },
  null: {
    fontStyle: "italic",
    opacity: 0.4,
  },
};

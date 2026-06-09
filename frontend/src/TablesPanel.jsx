import React, { useState, useEffect, useRef } from "react";
import { api } from "./api";
import SortableTable from "./SortableTable";
import { useHintTriggerFn } from "./HintSystem";

// Split a qualified name into [schema, table] on the LAST dot, so
// catalog-qualified names (e.g. DuckLake "__ducklake.foo.orders") keep the
// table as the final segment instead of dropping it.
function splitTableName(qualified) {
  const lastDot = (qualified || "").lastIndexOf(".");
  if (lastDot === -1) return ["", qualified || ""];
  return [qualified.slice(0, lastDot), qualified.slice(lastDot + 1)];
}

export default function TablesPanel({ selectedTable, onQueryTable, tables, onSelectTable }) {
  const [columns, setColumns] = useState([]);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [rowCount, setRowCount] = useState(null);
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("ASC");
  const [stats, setStats] = useState(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [maskingPolicies, setMaskingPolicies] = useState({});
  // schema.table -> { description, columns: { col_name -> description }, grain, owner }
  const [docsMap, setDocsMap] = useState({});
  const [showDocs, setShowDocs] = useState(true);

  // Load masking policies once on mount, build lookup map
  useEffect(() => {
    api.listMaskingPolicies().then(policies => {
      const map = {};
      for (const p of policies) {
        map[`${p.schema_name}.${p.table_name}.${p.column_name}`] = p.method;
      }
      setMaskingPolicies(map);
    }).catch(() => {});
  }, []);

  // Load structured docs once. Response shape: { schemas: [{name, tables: [...]}], lineage }
  useEffect(() => {
    api.getStructuredDocs().then(d => {
      const map = {};
      const schemas = (d && d.schemas) || [];
      for (const s of schemas) {
        for (const t of s.tables || []) {
          const colMap = {};
          for (const c of t.columns || []) {
            if (c && c.description) colMap[c.name] = c.description;
          }
          const fullName = t.full_name || `${s.name}.${t.name}`;
          map[fullName] = {
            description: t.description || "",
            columns: colMap,
            grain: t.grain || [],
            owner: t.owner || "",
            source_freshness: t.source_freshness || [],
          };
        }
      }
      setDocsMap(map);
    }).catch(() => {});
  }, []);
  const setHintTrigger = useHintTriggerFn();
  const hasTriggeredRef = useRef(false);

  useEffect(() => {
    if (selectedTable && !hasTriggeredRef.current) {
      hasTriggeredRef.current = true;
      setHintTrigger("firstTableSelected", true);
    }
    // Reset docs visibility to the default per table, so a "Hide Docs" choice
    // on one table doesn't silently carry over to the next.
    setShowDocs(true);
  }, [selectedTable]);

  const requestIdRef = useRef(0);

  useEffect(() => {
    if (!selectedTable) {
      setColumns([]);
      setPreview(null);
      setRowCount(null);
      setSortCol(null);
      setStats(null);
      return;
    }
    const lastDot = selectedTable.lastIndexOf(".");
    const schema = selectedTable.slice(0, lastDot);
    const name = selectedTable.slice(lastDot + 1);
    const myRequest = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    setRowCount(null);
    setSortCol(null);
    setStats(null);
    Promise.all([
      api.describeTable(schema, name),
      api.runQuery(`SELECT * FROM ${schema}.${name} LIMIT 100`),
      api.runQuery(`SELECT COUNT(*) AS cnt FROM ${schema}.${name}`),
    ])
      .then(([info, data, countData]) => {
        if (myRequest !== requestIdRef.current) return;
        setColumns(info.columns);
        setPreview(data);
        if (countData.rows && countData.rows[0]) {
          setRowCount(countData.rows[0][0]);
        }
      })
      .catch((e) => {
        if (myRequest !== requestIdRef.current) return;
        setError(e.message || "Failed to load table data");
      })
      .finally(() => {
        if (myRequest !== requestIdRef.current) return;
        setLoading(false);
      });
  }, [selectedTable]);

  function handleColumnClick(colName) {
    if (!selectedTable) return;
    const lastDot = selectedTable.lastIndexOf(".");
    const schema = selectedTable.slice(0, lastDot);
    const name = selectedTable.slice(lastDot + 1);
    const newDir = sortCol === colName && sortDir === "ASC" ? "DESC" : "ASC";
    setSortCol(colName);
    setSortDir(newDir);
    const myRequest = ++requestIdRef.current;
    setLoading(true);
    api.runQuery(`SELECT * FROM ${schema}.${name} ORDER BY "${colName}" ${newDir} LIMIT 100`)
      .then((data) => {
        if (myRequest !== requestIdRef.current) return;
        setPreview(data);
      })
      .catch((e) => {
        if (myRequest !== requestIdRef.current) return;
        setError(e.message || "Failed to sort table");
      })
      .finally(() => {
        if (myRequest !== requestIdRef.current) return;
        setLoading(false);
      });
  }

  async function loadStats() {
    if (!selectedTable || statsLoading) return;
    setStatsLoading(true);
    const lastDot = selectedTable.lastIndexOf(".");
    const schema = selectedTable.slice(0, lastDot);
    const name = selectedTable.slice(lastDot + 1);
    const numericCols = columns.filter((c) =>
      /int|float|double|decimal|numeric|bigint|smallint|tinyint|real/i.test(c.type),
    );
    if (numericCols.length === 0) {
      setStats([]);
      setStatsLoading(false);
      return;
    }
    const selects = numericCols.map((c) =>
      `MIN("${c.name}") AS "${c.name}_min", MAX("${c.name}") AS "${c.name}_max", SUM(CASE WHEN "${c.name}" IS NULL THEN 1 ELSE 0 END) AS "${c.name}_nulls"`,
    ).join(", ");
    try {
      const data = await api.runQuery(`SELECT ${selects} FROM ${schema}.${name}`);
      if (data.rows && data.rows[0]) {
        const row = data.rows[0];
        const result = numericCols.map((c, i) => ({
          name: c.name,
          type: c.type,
          min: row[i * 3],
          max: row[i * 3 + 1],
          nulls: row[i * 3 + 2],
        }));
        setStats(result);
      }
    } catch {
      setStats([]);
    } finally {
      setStatsLoading(false);
    }
  }

  function handleQueryTable() {
    if (!selectedTable || !onQueryTable) return;
    const [schema, name] = splitTableName(selectedTable);
    onQueryTable(schema, name);
  }

  if (!selectedTable) {
    // Group tables by schema for an overview
    const bySchema = {};
    if (tables) {
      for (const t of tables) {
        if (!bySchema[t.schema]) bySchema[t.schema] = [];
        bySchema[t.schema].push(t);
      }
    }
    const schemaNames = Object.keys(bySchema).sort();

    if (schemaNames.length === 0) {
      return (
        <div style={st.emptyState}>
          <div style={st.emptyIcon}>
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <line x1="3" y1="9" x2="21" y2="9" />
              <line x1="3" y1="15" x2="21" y2="15" />
              <line x1="9" y1="3" x2="9" y2="21" />
            </svg>
          </div>
          <div style={st.emptyTitle}>No tables yet</div>
          <div style={st.emptyHint}>Run a pipeline or import data to create tables in your warehouse.</div>
        </div>
      );
    }

    return (
      <div style={{ padding: "20px 24px", overflow: "auto", height: "100%" }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "var(--havn-text)", marginBottom: 4, letterSpacing: "-0.01em" }}>Warehouse</div>
        <div style={{ fontSize: 12, color: "var(--havn-text-secondary)", marginBottom: 20 }}>
          {tables.length} table{tables.length !== 1 ? "s" : ""} across {schemaNames.length} schema{schemaNames.length !== 1 ? "s" : ""}
        </div>
        {schemaNames.map(schema => (
          <div key={schema} style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: "0.5px", marginBottom: 6 }}>{schema}</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {bySchema[schema].map(t => (
                <button
                  key={t.name}
                  onClick={() => onSelectTable && onSelectTable(t.schema, t.name)}
                  style={{
                    padding: "5px 10px",
                    background: "var(--havn-bg-secondary)",
                    border: "1px solid var(--havn-border)",
                    borderRadius: "var(--havn-radius)",
                    color: "var(--havn-text)",
                    cursor: "pointer",
                    fontSize: 12,
                    fontFamily: "var(--havn-font-mono)",
                    fontWeight: 500,
                    transition: "border-color 0.12s ease",
                  }}
                  onMouseEnter={e => e.currentTarget.style.borderColor = "var(--havn-accent)"}
                  onMouseLeave={e => e.currentTarget.style.borderColor = "var(--havn-border)"}
                >
                  {t.name}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (loading && !preview) {
    return <div style={st.placeholder}>Loading...</div>;
  }

  if (error) {
    return <div style={{ ...st.placeholder, color: "var(--havn-red)" }}>{error}</div>;
  }

  function formatCount(n) {
    if (n == null) return "";
    return `${n.toLocaleString()} row${n !== 1 ? "s" : ""}`;
  }

  return (
    <div style={st.container}>
      <div style={st.tableHeader}>
        <div style={st.nameGroup}>
          <span style={st.schemaLabel}>{splitTableName(selectedTable)[0]}</span>
          <span style={st.nameSep}>.</span>
          <strong style={st.selectedName}>{splitTableName(selectedTable)[1]}</strong>
        </div>
        <div style={st.metaBadges}>
          <span style={st.metaBadge}>{columns.length} col{columns.length !== 1 ? "s" : ""}</span>
          {rowCount != null && <span style={st.metaBadge}>{formatCount(rowCount)}</span>}
        </div>
        <div style={st.headerActions}>
          {onQueryTable && (
            <button onClick={handleQueryTable} style={st.actionBtnPrimary}>Query this table</button>
          )}
          <button onClick={loadStats} disabled={statsLoading} style={st.actionBtn}>
            {statsLoading ? "Loading..." : stats ? "Refresh Stats" : "Show Stats"}
          </button>
          {stats && (
            <button onClick={() => setStats(null)} style={st.actionBtn}>Hide Stats</button>
          )}
          {docsMap[selectedTable] && (docsMap[selectedTable].description || Object.keys(docsMap[selectedTable].columns || {}).length > 0) && (
            <button onClick={() => setShowDocs(s => !s)} style={st.actionBtn}>
              {showDocs ? "Hide Docs" : "Show Docs"}
            </button>
          )}
        </div>
      </div>
      {showDocs && docsMap[selectedTable] && (docsMap[selectedTable].description || (docsMap[selectedTable].grain && docsMap[selectedTable].grain.length) || docsMap[selectedTable].owner) && (
        <div style={{ padding: "8px 12px", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", marginBottom: 12, fontSize: 12, color: "var(--havn-text-secondary)" }}>
          {docsMap[selectedTable].description && <div style={{ marginBottom: 4 }}>{docsMap[selectedTable].description}</div>}
          {(docsMap[selectedTable].grain || []).length > 0 && (
            <div>
              <span style={{ color: "var(--havn-text-dim)" }}>grain: </span>
              <span style={{ fontFamily: "var(--havn-font-mono)" }}>{(docsMap[selectedTable].grain || []).join(", ")}</span>
            </div>
          )}
          {docsMap[selectedTable].owner && (
            <div>
              <span style={{ color: "var(--havn-text-dim)" }}>owner: </span>
              <span>{docsMap[selectedTable].owner}</span>
            </div>
          )}
        </div>
      )}
      <div style={st.columnsSection}>
        <div style={st.sectionLabel}>Columns</div>
        <div style={st.columnsBar} data-havn-hint="columns-bar">
          {columns.map((c) => {
            const maskKey = selectedTable ? `${selectedTable}.${c.name}` : '';
            const maskMethod = maskingPolicies[maskKey];
            const colDocs = docsMap[selectedTable]?.columns?.[c.name] || "";
            const isActive = sortCol === c.name;
            const titleParts = [];
            if (colDocs) titleParts.push(colDocs);
            if (maskMethod) titleParts.push(`Masked: ${maskMethod}`);
            titleParts.push(`Sort by ${c.name}`);
            return (
              <button
                key={c.name}
                onClick={() => handleColumnClick(c.name)}
                style={{
                  ...st.colChip,
                  ...(isActive ? st.colChipActive : {}),
                }}
                title={titleParts.join(" \u2022 ")}
              >
                {colDocs && <span title={colDocs} style={{ marginRight: 4, opacity: 0.6 }}>&#9432;</span>}
                {maskMethod && <span style={st.maskIcon} title={`Masked: ${maskMethod}`}>&#x1F6E1;</span>}
                <span style={st.colName}>{c.name}</span>
                <span style={st.colType}>{c.type}</span>
                {isActive && <span style={st.sortArrow}>{sortDir === "ASC" ? "\u2191" : "\u2193"}</span>}
              </button>
            );
          })}
        </div>
        {showDocs && Object.keys(docsMap[selectedTable]?.columns || {}).length > 0 && (
          <div style={{ marginTop: 8, fontSize: 11, color: "var(--havn-text-secondary)", display: "grid", gridTemplateColumns: "auto 1fr", columnGap: 12, rowGap: 4 }}>
            {columns.filter(c => docsMap[selectedTable]?.columns?.[c.name]).map(c => (
              <React.Fragment key={c.name}>
                <span style={{ fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-dim)" }}>{c.name}</span>
                <span>{docsMap[selectedTable].columns[c.name]}</span>
              </React.Fragment>
            ))}
          </div>
        )}
      </div>
      {stats && stats.length > 0 && (
        <div style={st.statsBar}>
          <div style={st.sectionLabel}>Stats — numeric columns</div>
          <div style={st.statsList}>
            {stats.map((s) => (
              <div key={s.name} style={st.statItem}>
                <span style={st.statName}>{s.name}</span>
                <div style={st.statValues}>
                  <span style={st.statVal}><span style={st.statKey}>min</span> {s.min ?? "NULL"}</span>
                  <span style={st.statVal}><span style={st.statKey}>max</span> {s.max ?? "NULL"}</span>
                  <span style={st.statVal}><span style={st.statKey}>nulls</span> {s.nulls}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      {stats && stats.length === 0 && (
        <div style={st.statsBar}>
          <div style={st.statsEmpty}>No numeric columns found for stats.</div>
        </div>
      )}
      {preview && (
        <div style={st.previewSection}>
          <div style={st.sectionLabel}>Preview — first 100 rows</div>
        </div>
      )}
      {preview && (
        <div style={st.previewWrap}>
          <SortableTable
            columns={preview.columns}
            rows={preview.rows}
            columnTypes={columns.map((c) => c.type)}
            maskedColumns={selectedTable ? preview.columns.reduce((acc, col) => {
              const m = maskingPolicies[`${selectedTable}.${col}`];
              if (m) acc[col] = m;
              return acc;
            }, {}) : undefined}
          />
        </div>
      )}
    </div>
  );
}

const st = {
  container: {
    display: "flex", flexDirection: "column", height: "100%", overflow: "auto", padding: "16px",
  },

  /* ── Empty state ─────────────────────────────────────────────── */
  emptyState: {
    display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
    padding: "48px 24px", height: "100%", textAlign: "center",
  },
  emptyIcon: {
    color: "var(--havn-text-dim)", marginBottom: "16px", opacity: 0.5,
  },
  emptyTitle: {
    fontSize: "14px", fontWeight: 600, color: "var(--havn-text-secondary)", marginBottom: "6px",
  },
  emptyHint: {
    fontSize: "12px", color: "var(--havn-text-dim)", maxWidth: "280px", lineHeight: "1.5",
  },
  placeholder: {
    color: "var(--havn-text-dim)", padding: "24px", textAlign: "center",
  },

  /* ── Table header ────────────────────────────────────────────── */
  tableHeader: {
    display: "flex", alignItems: "center", gap: "12px", padding: "10px 4px 12px",
    fontSize: "13px", flexWrap: "wrap", borderBottom: "1px solid var(--havn-border)",
    marginBottom: "2px",
  },
  nameGroup: {
    display: "flex", alignItems: "baseline", gap: "1px",
    fontFamily: "var(--havn-font-mono)", fontSize: "14px",
  },
  schemaLabel: {
    color: "var(--havn-text-secondary)", fontWeight: 400,
  },
  nameSep: {
    color: "var(--havn-text-dim)",
  },
  selectedName: {
    color: "var(--havn-text)", fontWeight: 600,
  },
  metaBadges: {
    display: "flex", gap: "8px", alignItems: "center",
  },
  metaBadge: {
    fontSize: "11px", color: "var(--havn-text-secondary)",
    fontFamily: "var(--havn-font-mono)", background: "var(--havn-bg-tertiary)",
    padding: "2px 8px", borderRadius: "var(--havn-radius)",
    border: "1px solid var(--havn-border)",
  },
  headerActions: {
    marginLeft: "auto", display: "flex", gap: "8px",
  },
  actionBtn: {
    padding: "5px 12px", background: "var(--havn-btn-bg)",
    border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius)",
    color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px", fontWeight: 500,
  },
  actionBtnPrimary: {
    padding: "5px 14px", background: "transparent",
    border: "1px solid var(--havn-accent)", borderRadius: "var(--havn-radius)",
    color: "var(--havn-accent)", cursor: "pointer", fontSize: "11px", fontWeight: 600,
  },

  /* ── Section label (shared) ──────────────────────────────────── */
  sectionLabel: {
    fontSize: "10px", color: "var(--havn-text-dim)", fontWeight: 600,
    textTransform: "uppercase", letterSpacing: "0.5px",
    padding: "0 2px", marginBottom: "6px",
  },

  /* ── Columns bar ─────────────────────────────────────────────── */
  columnsSection: {
    padding: "12px 4px 4px",
  },
  columnsBar: {
    display: "flex", flexWrap: "wrap", gap: "5px",
  },
  colChip: {
    display: "inline-flex", alignItems: "center", gap: "6px",
    background: "var(--havn-btn-bg)", border: "1px solid var(--havn-border)",
    padding: "4px 10px", borderRadius: "var(--havn-radius)",
    fontSize: "11px", fontFamily: "var(--havn-font-mono)",
    cursor: "pointer", color: "var(--havn-text)", lineHeight: "1.3",
    transition: "border-color 0.12s ease",
  },
  colChipActive: {
    borderColor: "var(--havn-accent)", background: "color-mix(in srgb, var(--havn-accent) 8%, var(--havn-btn-bg))",
  },
  colName: {
    fontWeight: 500,
  },
  colType: {
    color: "var(--havn-text-dim)", fontSize: "10px",
  },
  maskIcon: {
    marginRight: "1px", fontSize: "10px", opacity: 0.6,
  },
  sortArrow: {
    color: "var(--havn-accent)", fontWeight: 700, fontSize: "12px",
  },

  /* ── Stats bar ───────────────────────────────────────────────── */
  statsBar: {
    padding: "12px 4px", borderTop: "1px solid var(--havn-border)", marginTop: "4px",
  },
  statsEmpty: {
    fontSize: "12px", color: "var(--havn-text-dim)", fontStyle: "italic",
  },
  statsList: {
    display: "flex", flexWrap: "wrap", gap: "6px",
  },
  statItem: {
    display: "flex", flexDirection: "column", gap: "4px",
    padding: "8px 12px", background: "var(--havn-bg-tertiary)",
    borderRadius: "var(--havn-radius)", border: "1px solid var(--havn-border)",
    fontSize: "11px", fontFamily: "var(--havn-font-mono)", minWidth: "120px",
  },
  statName: {
    fontWeight: 600, color: "var(--havn-text)", fontSize: "12px",
    borderBottom: "1px solid var(--havn-border)", paddingBottom: "4px", marginBottom: "1px",
  },
  statValues: {
    display: "flex", flexDirection: "column", gap: "2px",
  },
  statVal: {
    color: "var(--havn-text-secondary)", fontSize: "11px",
  },
  statKey: {
    color: "var(--havn-text-dim)", display: "inline-block", width: "36px",
  },

  /* ── Preview ─────────────────────────────────────────────────── */
  previewSection: {
    padding: "12px 4px 0",
  },
  previewWrap: {
    overflow: "auto", flex: 1,
  },
};

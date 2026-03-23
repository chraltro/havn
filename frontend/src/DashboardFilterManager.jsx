import React, { useState, useEffect } from "react";
import { api } from "./api";
import { useDashboard } from "./DashboardContext";

/**
 * Filter management modal — add, edit, remove dashboard-level filters.
 * Each filter has: id, label, type, column, options_sql (optional).
 */

const FILTER_TYPES = [
  { value: "dropdown", label: "Dropdown", desc: "Single-select from values" },
  { value: "multi_select", label: "Multi-select", desc: "Check multiple values" },
  { value: "date_range", label: "Date range", desc: "From/to with presets" },
  { value: "text", label: "Text search", desc: "Free-text filter" },
  { value: "number_range", label: "Number range", desc: "Min/max numeric" },
];

export default function DashboardFilterManager({ onClose }) {
  const { dashboard, saveDashboard } = useDashboard();
  const [filters, setFilters] = useState(dashboard?.filters || []);
  const [allColumns, setAllColumns] = useState([]);

  // Load all warehouse columns for the column picker
  useEffect(() => {
    api.getAutocomplete().then(data => {
      setAllColumns(data?.columns || []);
    }).catch(() => {});
  }, []);

  function addFilter() {
    setFilters(prev => [
      ...prev,
      {
        id: `filter_${Date.now()}`,
        label: "",
        type: "dropdown",
        column: "",
        options_sql: "",
      },
    ]);
  }

  function updateFilter(index, updates) {
    setFilters(prev => prev.map((f, i) => i === index ? { ...f, ...updates } : f));
  }

  function removeFilter(index) {
    setFilters(prev => prev.filter((_, i) => i !== index));
  }

  function moveFilter(index, dir) {
    setFilters(prev => {
      const next = [...prev];
      const target = index + dir;
      if (target < 0 || target >= next.length) return next;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }

  async function handleSave() {
    // Validate: each filter needs at least a column and label
    const valid = filters.filter(f => f.column && f.label);
    await saveDashboard({ filters: valid });
    onClose();
  }

  // Auto-generate label from column name
  function autoLabel(col) {
    return col.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  // Auto-generate options_sql for dropdown/multi_select
  function autoOptionsSql(col) {
    // Find the table for this column
    const colInfo = allColumns.find(c => c.name === col);
    if (colInfo) return `SELECT DISTINCT ${col} FROM ${colInfo.schema}.${colInfo.table} ORDER BY 1`;
    return "";
  }

  return (
    <div style={st.overlay} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div style={st.panel}>
        <div style={st.header}>
          <h3 style={st.heading}>Manage Filters</h3>
          <button style={st.closeBtn} onClick={onClose}>×</button>
        </div>

        <div style={st.body}>
          {filters.length === 0 && (
            <div style={st.empty}>
              <div style={{ fontSize: 14, color: "var(--havn-text-secondary)" }}>
                No filters yet. Add a filter to let users slice dashboard data.
              </div>
            </div>
          )}

          {filters.map((f, i) => (
            <div key={f.id || i} style={st.filterCard}>
              <div style={st.filterRow}>
                <div style={{ flex: 1 }}>
                  <label style={st.fieldLabel}>Label</label>
                  <input
                    style={st.input}
                    value={f.label}
                    placeholder="e.g., Date range, Category"
                    onChange={(e) => updateFilter(i, { label: e.target.value })}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <label style={st.fieldLabel}>Type</label>
                  <select
                    style={st.select}
                    value={f.type}
                    onChange={(e) => updateFilter(i, { type: e.target.value })}
                  >
                    {FILTER_TYPES.map(t => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div style={st.filterRow}>
                <div style={{ flex: 1 }}>
                  <label style={st.fieldLabel}>Column</label>
                  <select
                    style={st.select}
                    value={f.column}
                    onChange={(e) => {
                      const col = e.target.value;
                      const updates = { column: col };
                      if (!f.label) updates.label = autoLabel(col);
                      if ((f.type === "dropdown" || f.type === "multi_select") && !f.options_sql) {
                        updates.options_sql = autoOptionsSql(col);
                      }
                      updateFilter(i, updates);
                    }}
                  >
                    <option value="">Select column...</option>
                    {allColumns.map(c => (
                      <option key={c.full_name} value={c.name}>
                        {c.schema}.{c.table}.{c.name} ({c.type})
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {(f.type === "dropdown" || f.type === "multi_select") && (
                <div style={{ marginTop: 6 }}>
                  <label style={st.fieldLabel}>Options SQL (auto-generated)</label>
                  <input
                    style={st.input}
                    value={f.options_sql || ""}
                    placeholder="SELECT DISTINCT column FROM schema.table ORDER BY 1"
                    onChange={(e) => updateFilter(i, { options_sql: e.target.value })}
                  />
                </div>
              )}

              <div style={st.filterActions}>
                <button style={st.moveBtn} onClick={() => moveFilter(i, -1)} disabled={i === 0} title="Move up">↑</button>
                <button style={st.moveBtn} onClick={() => moveFilter(i, 1)} disabled={i === filters.length - 1} title="Move down">↓</button>
                <button style={st.removeBtn} onClick={() => removeFilter(i)} title="Remove">Remove</button>
              </div>
            </div>
          ))}

          <button style={st.addBtn} onClick={addFilter}>+ Add Filter</button>
        </div>

        <div style={st.footer}>
          <button style={st.cancelBtn} onClick={onClose}>Cancel</button>
          <button style={st.saveBtn} onClick={handleSave}>Save Filters</button>
        </div>
      </div>
    </div>
  );
}

const st = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 2000 },
  panel: { background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 12, width: 600, maxWidth: "90vw", maxHeight: "80vh", display: "flex", flexDirection: "column" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 20px", borderBottom: "1px solid var(--havn-border)" },
  heading: { margin: 0, fontSize: 16, fontWeight: 600, color: "var(--havn-text)" },
  closeBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 22, cursor: "pointer" },
  body: { flex: 1, overflow: "auto", padding: 20 },
  empty: { textAlign: "center", padding: 24 },
  filterCard: { border: "1px solid var(--havn-border)", borderRadius: 8, padding: 14, marginBottom: 12, background: "var(--havn-bg-secondary, var(--havn-bg))" },
  filterRow: { display: "flex", gap: 10, marginBottom: 6 },
  fieldLabel: { display: "block", fontSize: 11, fontWeight: 600, color: "var(--havn-text-secondary)", marginBottom: 3 },
  input: { width: "100%", padding: "6px 10px", border: "1px solid var(--havn-border)", borderRadius: 5, background: "var(--havn-bg)", color: "var(--havn-text)", fontSize: 13, outline: "none", boxSizing: "border-box" },
  select: { width: "100%", padding: "6px 10px", border: "1px solid var(--havn-border)", borderRadius: 5, background: "var(--havn-bg)", color: "var(--havn-text)", fontSize: 13, outline: "none" },
  filterActions: { display: "flex", gap: 6, marginTop: 8, justifyContent: "flex-end" },
  moveBtn: { background: "none", border: "1px solid var(--havn-border)", borderRadius: 4, color: "var(--havn-text-secondary)", cursor: "pointer", padding: "2px 8px", fontSize: 13 },
  removeBtn: { background: "none", border: "1px solid var(--havn-red, #ef4444)", borderRadius: 4, color: "var(--havn-red, #ef4444)", cursor: "pointer", padding: "2px 10px", fontSize: 12 },
  addBtn: { display: "block", width: "100%", padding: 10, border: "1px dashed var(--havn-border)", borderRadius: 8, background: "none", color: "var(--havn-accent)", cursor: "pointer", fontSize: 13, fontWeight: 500 },
  footer: { display: "flex", gap: 8, justifyContent: "flex-end", padding: "12px 20px", borderTop: "1px solid var(--havn-border)" },
  cancelBtn: { background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text-secondary)", borderRadius: 6, padding: "8px 18px", cursor: "pointer", fontSize: 13 },
  saveBtn: { background: "var(--havn-accent)", color: "#fff", border: "none", borderRadius: 6, padding: "8px 18px", cursor: "pointer", fontSize: 13, fontWeight: 600 },
};

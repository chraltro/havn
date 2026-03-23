import React, { useState, useEffect, useRef, useCallback } from "react";
import { api } from "./api";
import { useDashboard } from "./DashboardContext";

/**
 * Global filter bar rendered above the dashboard canvas.
 * Supports: dropdown, multi_select, date_range, text, number_range, schema.
 */

// Shared cache for options_sql results, keyed by SQL string.
const _optionsSqlCache = new Map();

export default function DashboardFilterBar() {
  const { dashboard, globalFilters, setFilter, parameters, setParameter, clearCrossFilter, crossFilter } = useDashboard();

  const filters = dashboard?.filters || [];
  const params = dashboard?.settings?.parameters || [];

  if (filters.length === 0 && params.length === 0 && !crossFilter) return null;

  return (
    <div style={st.bar}>
      {/* Global filters */}
      {filters.map(f => (
        <FilterControl key={f.id} filter={f} value={globalFilters[f.column] ?? null} onChange={(val) => setFilter(f.column, val)} />
      ))}

      {/* Parameters */}
      {params.map(p => (
        <ParamControl key={p.name} param={p} value={parameters[p.name] ?? ""} onChange={(val) => setParameter(p.name, val)} />
      ))}

      {/* Cross-filter indicator */}
      {crossFilter && (
        <div style={st.crossIndicator}>
          <span style={{ fontSize: 11 }}>
            Filtered: {crossFilter.column} = {String(crossFilter.value)}
          </span>
          <button style={st.clearBtn} onClick={clearCrossFilter} title="Clear cross-filter">×</button>
        </div>
      )}
    </div>
  );
}

// ---------- Date preset helpers ----------

function startOfMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function startOfQuarter(d) {
  const q = Math.floor(d.getMonth() / 3) * 3;
  return new Date(d.getFullYear(), q, 1);
}

function fmtDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function daysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

const DATE_PRESETS = [
  { label: "Last 7d", calc: () => ({ from: fmtDate(daysAgo(7)), to: fmtDate(new Date()) }) },
  { label: "Last 30d", calc: () => ({ from: fmtDate(daysAgo(30)), to: fmtDate(new Date()) }) },
  { label: "This month", calc: () => ({ from: fmtDate(startOfMonth(new Date())), to: fmtDate(new Date()) }) },
  { label: "This quarter", calc: () => ({ from: fmtDate(startOfQuarter(new Date())), to: fmtDate(new Date()) }) },
  { label: "YTD", calc: () => ({ from: fmtDate(new Date(new Date().getFullYear(), 0, 1)), to: fmtDate(new Date()) }) },
  { label: "All time", calc: () => null },
];

// ---------- Filter Controls ----------

function FilterControl({ filter, value, onChange }) {
  const [options, setOptions] = useState([]);
  const cacheRef = useRef(_optionsSqlCache);

  // Load options from SQL (cached) or static list
  useEffect(() => {
    if (filter.options_sql) {
      const cached = cacheRef.current.get(filter.options_sql);
      if (cached) {
        setOptions(cached);
        return;
      }
      api.runQuery(filter.options_sql).then(result => {
        if (result?.rows) {
          const opts = result.rows.map(r => String(r[0] ?? ""));
          cacheRef.current.set(filter.options_sql, opts);
          setOptions(opts);
        }
      }).catch(() => {});
    } else if (filter.options) {
      setOptions(filter.options);
    }
  }, [filter.options_sql, filter.options]);

  switch (filter.type) {
    case "dropdown":
      return (
        <div style={st.filterGroup}>
          <label style={st.filterLabel}>{filter.label}</label>
          <select
            style={st.filterSelect}
            value={value || ""}
            onChange={(e) => onChange(e.target.value || null)}
          >
            <option value="">All</option>
            {options.map(opt => <option key={opt} value={opt}>{opt}</option>)}
          </select>
        </div>
      );

    case "multi_select":
      return <MultiSelectFilter filter={filter} options={options} value={value} onChange={onChange} />;

    case "date_range":
      return <DateRangeFilter filter={filter} value={value} onChange={onChange} />;

    case "text":
      return (
        <div style={st.filterGroup}>
          <label style={st.filterLabel}>{filter.label}</label>
          <input
            type="text"
            style={st.filterInput}
            placeholder={filter.placeholder || "Search..."}
            value={value || ""}
            onChange={(e) => onChange(e.target.value || null)}
          />
        </div>
      );

    case "number_range":
      return <NumberRangeFilter filter={filter} value={value} onChange={onChange} />;

    default:
      return (
        <div style={st.filterGroup}>
          <label style={st.filterLabel}>{filter.label}</label>
          <input
            style={st.filterInput}
            value={value || ""}
            onChange={(e) => onChange(e.target.value || null)}
          />
        </div>
      );
  }
}

// ---------- DateRangeFilter ----------

function DateRangeFilter({ filter, value, onChange }) {
  const from = value?.from || "";
  const to = value?.to || "";

  const handleFrom = useCallback((e) => {
    const v = e.target.value;
    if (!v && !to) { onChange(null); return; }
    onChange({ from: v || "", to });
  }, [to, onChange]);

  const handleTo = useCallback((e) => {
    const v = e.target.value;
    if (!v && !from) { onChange(null); return; }
    onChange({ from, to: v || "" });
  }, [from, onChange]);

  const applyPreset = useCallback((preset) => {
    const result = preset.calc();
    onChange(result);
  }, [onChange]);

  return (
    <div style={st.filterGroup}>
      <label style={st.filterLabel}>{filter.label}</label>
      <div style={st.datePresets}>
        {DATE_PRESETS.map(p => (
          <button
            key={p.label}
            style={{
              ...st.presetPill,
              ...(isPresetActive(p, value) ? st.presetPillActive : {}),
            }}
            onClick={() => applyPreset(p)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div style={st.dateRow}>
        <input
          type="date"
          style={{ ...st.filterInput, minWidth: 120 }}
          value={from}
          onChange={handleFrom}
          placeholder="From"
        />
        <span style={{ fontSize: 11, color: "var(--havn-text-secondary)" }}>/</span>
        <input
          type="date"
          style={{ ...st.filterInput, minWidth: 120 }}
          value={to}
          onChange={handleTo}
          placeholder="To"
        />
      </div>
    </div>
  );
}

function isPresetActive(preset, value) {
  if (preset.label === "All time") return value == null;
  if (!value) return false;
  const calc = preset.calc();
  return calc && calc.from === value.from && calc.to === value.to;
}

// ---------- MultiSelectFilter ----------

function MultiSelectFilter({ filter, options, value, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const selected = Array.isArray(value) ? value : [];

  // Close popover on outside click
  useEffect(() => {
    if (!open) return;
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  const toggle = useCallback((opt) => {
    let next;
    if (selected.includes(opt)) {
      next = selected.filter(v => v !== opt);
    } else {
      next = [...selected, opt];
    }
    onChange(next.length === 0 ? null : next);
  }, [selected, onChange]);

  const selectAll = useCallback(() => {
    onChange(null);
    setOpen(false);
  }, [onChange]);

  const label = !value || selected.length === 0
    ? "All"
    : selected.length === 1
      ? selected[0]
      : `${selected.length} selected`;

  return (
    <div style={st.filterGroup}>
      <label style={st.filterLabel}>{filter.label}</label>
      <div ref={ref} style={{ position: "relative" }}>
        <button
          style={st.multiSelectBtn}
          onClick={() => setOpen(prev => !prev)}
          title={selected.join(", ") || "All"}
        >
          <span style={st.multiSelectLabel}>{label}</span>
          <span style={{ fontSize: 10, marginLeft: 4 }}>{open ? "\u25B2" : "\u25BC"}</span>
        </button>
        {open && (
          <div style={st.multiSelectPopover}>
            <button style={st.multiSelectAllBtn} onClick={selectAll}>
              Select all
            </button>
            <div style={st.multiSelectList}>
              {options.map(opt => (
                <label key={opt} style={st.multiSelectOption}>
                  <input
                    type="checkbox"
                    checked={selected.includes(opt)}
                    onChange={() => toggle(opt)}
                    style={{ marginRight: 6 }}
                  />
                  <span style={{ fontSize: 12 }}>{opt}</span>
                </label>
              ))}
              {options.length === 0 && (
                <span style={{ fontSize: 11, color: "var(--havn-text-secondary)", padding: 4 }}>No options</span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------- NumberRangeFilter ----------

function NumberRangeFilter({ filter, value, onChange }) {
  const min = value?.min ?? "";
  const max = value?.max ?? "";

  const handleMin = useCallback((e) => {
    const raw = e.target.value;
    const v = raw === "" ? null : Number(raw);
    const newMax = max === "" ? null : max;
    if (v == null && newMax == null) { onChange(null); return; }
    onChange({ min: v, max: newMax });
  }, [max, onChange]);

  const handleMax = useCallback((e) => {
    const raw = e.target.value;
    const v = raw === "" ? null : Number(raw);
    const newMin = min === "" ? null : min;
    if (v == null && newMin == null) { onChange(null); return; }
    onChange({ min: newMin, max: v });
  }, [min, onChange]);

  return (
    <div style={st.filterGroup}>
      <label style={st.filterLabel}>{filter.label}</label>
      <div style={st.numberRow}>
        <input
          type="number"
          style={{ ...st.filterInput, width: 80, minWidth: 60 }}
          placeholder="Min"
          value={min ?? ""}
          onChange={handleMin}
        />
        <span style={{ fontSize: 11, color: "var(--havn-text-secondary)" }}>/</span>
        <input
          type="number"
          style={{ ...st.filterInput, width: 80, minWidth: 60 }}
          placeholder="Max"
          value={max ?? ""}
          onChange={handleMax}
        />
      </div>
    </div>
  );
}

// ---------- ParamControl (unchanged) ----------

function ParamControl({ param, value, onChange }) {
  return (
    <div style={st.filterGroup}>
      <label style={st.filterLabel}>{param.label || param.name}</label>
      {param.type === "date" ? (
        <input type="date" style={st.filterInput} value={value || ""} onChange={(e) => onChange(e.target.value)} />
      ) : param.type === "number" ? (
        <input type="number" style={{ ...st.filterInput, width: 80 }} value={value || ""} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input style={st.filterInput} value={value || ""} onChange={(e) => onChange(e.target.value)} placeholder={param.name} />
      )}
    </div>
  );
}

// ---------- Styles ----------

const st = {
  bar: {
    display: "flex",
    alignItems: "flex-end",
    gap: 12,
    padding: "8px 16px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
    flexWrap: "wrap",
    background: "var(--havn-bg)",
  },
  filterGroup: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
  },
  filterLabel: {
    fontSize: 10,
    fontWeight: 600,
    color: "var(--havn-text-secondary)",
    textTransform: "uppercase",
    letterSpacing: 0.3,
  },
  filterSelect: {
    padding: "4px 8px",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    color: "var(--havn-text)",
    fontSize: 12,
    outline: "none",
    minWidth: 100,
  },
  filterInput: {
    padding: "4px 8px",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    color: "var(--havn-text)",
    fontSize: 12,
    outline: "none",
    minWidth: 100,
  },
  crossIndicator: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    padding: "4px 10px",
    background: "var(--havn-accent)",
    color: "#fff",
    borderRadius: 12,
    fontSize: 12,
  },
  clearBtn: {
    background: "none",
    border: "none",
    color: "#fff",
    cursor: "pointer",
    fontSize: 16,
    padding: "0 2px",
    lineHeight: 1,
    opacity: 0.8,
  },

  // Date range
  datePresets: {
    display: "flex",
    gap: 3,
    flexWrap: "wrap",
    marginBottom: 2,
  },
  presetPill: {
    padding: "1px 7px",
    fontSize: 10,
    border: "1px solid var(--havn-border)",
    borderRadius: 10,
    background: "transparent",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    whiteSpace: "nowrap",
    lineHeight: "18px",
  },
  presetPillActive: {
    background: "var(--havn-accent)",
    color: "#fff",
    borderColor: "var(--havn-accent)",
  },
  dateRow: {
    display: "flex",
    alignItems: "center",
    gap: 4,
  },

  // Number range
  numberRow: {
    display: "flex",
    alignItems: "center",
    gap: 4,
  },

  // Multi-select
  multiSelectBtn: {
    display: "flex",
    alignItems: "center",
    padding: "4px 8px",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    color: "var(--havn-text)",
    fontSize: 12,
    cursor: "pointer",
    minWidth: 100,
    outline: "none",
  },
  multiSelectLabel: {
    flex: 1,
    textAlign: "left",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  multiSelectPopover: {
    position: "absolute",
    top: "100%",
    left: 0,
    zIndex: 100,
    marginTop: 2,
    minWidth: 160,
    maxHeight: 240,
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
    display: "flex",
    flexDirection: "column",
  },
  multiSelectAllBtn: {
    padding: "4px 8px",
    fontSize: 11,
    border: "none",
    borderBottom: "1px solid var(--havn-border)",
    background: "transparent",
    color: "var(--havn-accent)",
    cursor: "pointer",
    textAlign: "left",
  },
  multiSelectList: {
    overflowY: "auto",
    padding: 4,
    display: "flex",
    flexDirection: "column",
    gap: 1,
  },
  multiSelectOption: {
    display: "flex",
    alignItems: "center",
    padding: "3px 4px",
    borderRadius: 3,
    cursor: "pointer",
    fontSize: 12,
  },
};

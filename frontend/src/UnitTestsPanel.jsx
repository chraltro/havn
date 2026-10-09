import React, { useState, useEffect, useCallback, useMemo } from "react";
import { api } from "./api";

const STATUS_COLORS = {
  pass: "var(--havn-green)",
  fail: "var(--havn-red)",
  error: "var(--havn-red)",
};

function statusLabel(status) {
  if (status === "pass") return "PASS";
  if (status === "fail") return "FAIL";
  return "ERROR";
}

/** Render a list of row dicts as a table. Returns null for an empty list. */
export function RowDiffTable({ label, rows, total, color }) {
  if (!rows || rows.length === 0) return null;
  const columns = Object.keys(rows[0]);
  return (
    <div style={st.rowDiff}>
      <div style={{ ...st.rowDiffTitle, color }}>
        {label} ({total})
      </div>
      <table style={st.table}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c} style={st.th}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c} style={st.td}>
                  {row[c] === null || row[c] === undefined
                    ? <span style={st.null}>NULL</span>
                    : String(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {total > rows.length && (
        <div style={st.dim}>... and {total - rows.length} more</div>
      )}
    </div>
  );
}

export default function UnitTestsPanel() {
  const [tests, setTests] = useState([]);
  const [loadErrors, setLoadErrors] = useState([]);
  const [result, setResult] = useState(null);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [modelFilter, setModelFilter] = useState("");
  const [expanded, setExpanded] = useState(null);

  const loadTests = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getUnitTests();
      setTests(data.tests || []);
      setLoadErrors(data.errors || []);
    } catch (e) {
      setError(e.message || "Could not load unit tests");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadTests(); }, [loadTests]);

  const runTests = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      const data = await api.runUnitTests(modelFilter || null);
      setResult(data);
      setLoadErrors(data.load_errors || []);
    } catch (e) {
      setError(e.message || "Run failed");
    } finally {
      setRunning(false);
    }
  }, [modelFilter]);

  const models = useMemo(() => {
    const seen = new Set();
    for (const t of tests) seen.add(t.model);
    return [...seen].sort();
  }, [tests]);

  // Before a run, show the declared tests; after one, show their outcomes.
  const rows = useMemo(() => {
    if (result) return result.results || [];
    return tests
      .filter((t) => !modelFilter || t.model === modelFilter)
      .map((t) => ({ ...t, status: null, duration_ms: null, message: "" }));
  }, [result, tests, modelFilter]);

  return (
    <div style={st.container}>
      <div style={st.header}>
        <div style={st.actionBar}>
          <button onClick={runTests} disabled={running} style={st.btn}>
            {running ? "Running..." : "Run tests"}
          </button>
          <select
            aria-label="Model filter"
            value={modelFilter}
            onChange={(e) => { setModelFilter(e.target.value); setResult(null); }}
            style={st.select}
          >
            <option value="">All models</option>
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          {result && (
            <span style={st.summary}>
              <span style={{ color: "var(--havn-green)" }}>{result.passed} passed</span>
              {result.failed > 0 && (
                <span style={{ color: "var(--havn-red)" }}>, {result.failed} failed</span>
              )}
              {result.errored > 0 && (
                <span style={{ color: "var(--havn-red)" }}>, {result.errored} errored</span>
              )}
              <span style={st.dim}> in {result.duration_ms}ms</span>
            </span>
          )}
        </div>
        <span style={st.dim}>
          Fixtures in, rows out. Runs in memory, never reads the warehouse.
        </span>
      </div>

      {error && <div style={st.error}>{error}</div>}
      {loadErrors.map((e, i) => (
        <div key={i} style={st.error}>Definition error: {e}</div>
      ))}

      <div style={st.body}>
        {loading && <div style={st.dim}>Loading...</div>}

        {!loading && rows.length === 0 && (
          <div style={st.empty}>
            <div style={{ fontWeight: 600, marginBottom: 6 }}>No unit tests yet.</div>
            <div style={st.dim}>
              Add <code>tests/unit/&lt;name&gt;.yml</code> with a <code>model:</code> and a
              {" "}<code>tests:</code> list declaring <code>given</code> rows and the
              {" "}<code>expect</code>ed output.
            </div>
          </div>
        )}

        {rows.length > 0 && (
          <table style={st.table}>
            <thead>
              <tr>
                <th style={{ ...st.th, width: 70 }}>Status</th>
                <th style={st.th}>Model</th>
                <th style={st.th}>Test</th>
                <th style={st.th}>Detail</th>
                <th style={{ ...st.th, textAlign: "right" }}>Time</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((res, i) => {
                const key = `${res.model}::${res.name}`;
                const failing = res.status === "fail" || res.status === "error";
                const isOpen = expanded === key;
                return (
                  <React.Fragment key={key}>
                    <tr
                      onClick={() => setExpanded(isOpen ? null : key)}
                      style={{ cursor: failing ? "pointer" : "default" }}
                    >
                      <td style={st.td}>
                        {res.status ? (
                          <span
                            style={{ ...st.badge, background: STATUS_COLORS[res.status] }}
                          >
                            {statusLabel(res.status)}
                          </span>
                        ) : (
                          <span style={st.dim}>not run</span>
                        )}
                      </td>
                      <td style={{ ...st.td, color: "var(--havn-accent)" }}>{res.model}</td>
                      <td style={{ ...st.td, fontWeight: 600 }}>{res.name}</td>
                      <td style={st.td}>{res.message}</td>
                      <td style={{ ...st.td, textAlign: "right" }}>
                        {res.duration_ms == null ? "" : `${res.duration_ms}ms`}
                      </td>
                    </tr>
                    {(res.warnings || []).map((w, j) => (
                      <tr key={`${key}-w-${j}`}>
                        <td style={st.td}></td>
                        <td style={st.warning} colSpan={4}>warning: {w}</td>
                      </tr>
                    ))}
                    {isOpen && failing && (
                      <tr>
                        <td style={st.td}></td>
                        <td style={st.td} colSpan={4}>
                          <RowDiffTable
                            label="Expected, not produced"
                            rows={res.missing_rows}
                            total={res.missing_count}
                            color="var(--havn-red)"
                          />
                          <RowDiffTable
                            label="Produced, not expected"
                            rows={res.unexpected_rows}
                            total={res.unexpected_count}
                            color="var(--havn-yellow)"
                          />
                          {res.source_path && (
                            <div style={st.dim}>tests/unit/{res.source_path}</div>
                          )}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", fontSize: 13, flexWrap: "wrap" },
  actionBar: { display: "flex", alignItems: "center", gap: 12 },
  btn: { padding: "4px 12px", background: "var(--havn-green)", color: "#fff", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11, fontWeight: 500 },
  select: { padding: "3px 6px", fontSize: 11, background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: 4 },
  summary: { fontSize: 12, fontWeight: 500 },
  body: { flex: 1, overflow: "auto", padding: 12 },
  dim: { color: "var(--havn-text-dim)", fontSize: 12 },
  empty: { padding: 12, border: "1px dashed var(--havn-border)", borderRadius: "var(--havn-radius-lg)", fontSize: 12 },
  error: { padding: "8px 12px", background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)", fontSize: 12, borderBottom: "1px solid var(--havn-border)" },
  warning: { padding: "4px 12px", color: "var(--havn-yellow)", fontSize: 11 },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  th: { textAlign: "left", padding: "6px 12px", background: "var(--havn-bg-tertiary)", borderBottom: "1px solid var(--havn-border)", fontWeight: 600, fontSize: 11 },
  td: { padding: "5px 12px", borderBottom: "1px solid var(--havn-border-light)", fontSize: 12, verticalAlign: "top" },
  badge: { color: "#fff", padding: "1px 6px", borderRadius: 3, fontSize: 10, fontWeight: 700 },
  rowDiff: { marginBottom: 10 },
  rowDiffTitle: { fontSize: 11, fontWeight: 600, marginBottom: 4 },
  null: { color: "var(--havn-text-dim)", fontStyle: "italic" },
};

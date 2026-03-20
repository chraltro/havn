import React, { useState, useMemo } from "react";
import SortableTable from "./SortableTable";
import { useHintTriggerFn } from "./HintSystem";
import { useWarehouse } from "./WarehouseContext";

const MODES = [
  { id: "changed", label: "Changed Only" },
  { id: "single", label: "Single Model" },
  { id: "full", label: "Full Database" },
];

export default function DiffPanel({ api, addOutput }) {
  const [results, setResults] = useState(null);
  const [loading, setLoading] = useState(false);
  const [expandedModel, setExpandedModel] = useState(null);
  const [mode, setMode] = useState("changed");
  const [modelFilter, setModelFilter] = useState("");
  const [selectedModel, setSelectedModel] = useState(null);
  const [showSkipped, setShowSkipped] = useState(false);
  const setHintTrigger = useHintTriggerFn();
  const { tables } = useWarehouse();

  // Get model names from warehouse tables, excluding internal schema
  const modelNames = useMemo(() => {
    return tables
      .filter((t) => t.schema !== "_dp_internal")
      .map((t) => `${t.schema}.${t.name}`)
      .sort();
  }, [tables]);

  const filteredModels = useMemo(() => {
    if (!modelFilter.trim()) return modelNames;
    const lower = modelFilter.toLowerCase();
    return modelNames.filter((m) => m.toLowerCase().includes(lower));
  }, [modelNames, modelFilter]);

  const runDiff = async () => {
    setLoading(true);
    setResults(null);
    const modeLabel = mode === "single" ? `single model (${selectedModel})` : mode === "changed" ? "changed models" : "full database";
    addOutput("info", `Running diff (${modeLabel})...`);
    try {
      let targets = null;
      if (mode === "single" && selectedModel) {
        targets = [selectedModel];
      }
      const data = await api.runDiff(targets, null, mode === "full", mode);
      setResults(data);
      const changes = data.filter(
        (r) => r.added || r.removed || r.modified || r.is_new || r.error || (r.schema_changes && r.schema_changes.length)
      );
      const skipped = data.filter(
        (r) => !r.added && !r.removed && !r.modified && !r.is_new && !r.error && (!r.schema_changes || !r.schema_changes.length)
      );
      if (changes.length === 0) {
        addOutput("info", `Diff complete: no changes detected${skipped.length ? ` (${skipped.length} model(s) unchanged)` : ""}.`);
      } else {
        addOutput("info", `Diff complete: ${changes.length} model(s) with changes${skipped.length ? `, ${skipped.length} unchanged` : ""}.`);
        setHintTrigger("hasDiffChanges", true);
      }
    } catch (err) {
      addOutput("error", `Diff failed: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  const schemaLabel = (sc) => {
    if (!sc || !sc.length) return "\u2014";
    const adds = sc.filter((s) => s.change_type === "added").length;
    const removes = sc.filter((s) => s.change_type === "removed").length;
    const changes = sc.filter((s) => s.change_type === "type_changed").length;
    const parts = [];
    if (adds) parts.push(`+${adds} col`);
    if (removes) parts.push(`-${removes} col`);
    if (changes) parts.push(`~${changes} col`);
    return parts.join(", ");
  };

  // Separate changed and skipped results
  const changedResults = results
    ? results.filter((r) => r.added || r.removed || r.modified || r.is_new || r.error || (r.schema_changes && r.schema_changes.length))
    : [];
  const skippedResults = results
    ? results.filter((r) => !r.added && !r.removed && !r.modified && !r.is_new && !r.error && (!r.schema_changes || !r.schema_changes.length))
    : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header with mode selector */}
      <div style={{ display: "flex", alignItems: "center", gap: "12px", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", flexWrap: "wrap" }}>
        {/* Mode selector */}
        <div style={segStyles.group}>
          {MODES.map((m) => (
            <button
              key={m.id}
              onClick={() => { setMode(m.id); setSelectedModel(null); setModelFilter(""); }}
              style={mode === m.id ? segStyles.btnActive : segStyles.btn}
            >
              {m.label}
            </button>
          ))}
        </div>

        {/* Single model filter */}
        {mode === "single" && (
          <div style={{ position: "relative", flex: "0 1 260px" }}>
            <input
              value={modelFilter}
              onChange={(e) => { setModelFilter(e.target.value); setSelectedModel(null); }}
              placeholder="Search model..."
              style={{
                width: "100%",
                padding: "4px 8px",
                fontSize: "11px",
                fontFamily: "var(--havn-font-mono)",
                background: "var(--havn-bg)",
                border: "1px solid var(--havn-border-light)",
                borderRadius: "var(--havn-radius)",
                color: "var(--havn-text)",
                outline: "none",
              }}
            />
            {modelFilter && filteredModels.length > 0 && !selectedModel && (
              <div style={{
                position: "absolute", top: "100%", left: 0, right: 0, marginTop: "2px",
                background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)",
                borderRadius: "var(--havn-radius)", maxHeight: "200px", overflow: "auto", zIndex: 50,
                boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
              }}>
                {filteredModels.slice(0, 20).map((m) => (
                  <button
                    key={m}
                    onClick={() => { setSelectedModel(m); setModelFilter(m); }}
                    style={{
                      display: "block", width: "100%", padding: "4px 8px", background: "none",
                      border: "none", color: "var(--havn-text)", cursor: "pointer", fontSize: "11px",
                      fontFamily: "var(--havn-font-mono)", textAlign: "left",
                    }}
                    onMouseEnter={(e) => e.currentTarget.style.background = "var(--havn-btn-bg)"}
                    onMouseLeave={(e) => e.currentTarget.style.background = "none"}
                  >
                    {m}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        <button
          onClick={runDiff}
          disabled={loading || (mode === "single" && !selectedModel)}
          aria-label="Run diff comparison"
          style={{
            padding: "4px 12px",
            borderRadius: "var(--havn-radius-lg)",
            border: "1px solid var(--havn-green-border)",
            background: "var(--havn-green)",
            color: "#fff",
            cursor: loading || (mode === "single" && !selectedModel) ? "not-allowed" : "pointer",
            fontSize: "11px",
            fontWeight: 500,
            opacity: (mode === "single" && !selectedModel) ? 0.5 : 1,
          }}
        >
          {loading ? "Running..." : "Run Diff"}
        </button>
      </div>

      <p style={{ color: "var(--havn-text-secondary)", fontSize: 13, marginBottom: 0, padding: "8px 12px" }}>
        {mode === "changed" && "Compare changed model SQL output against currently materialized tables."}
        {mode === "single" && "Diff a single model to see what would change."}
        {mode === "full" && "Full database scan \u2014 compare every model against its materialized table."}
      </p>

      {results && (
        <div style={{ flex: 1, overflow: "auto" }} data-havn-hint="diff-results">
          {/* Changed models */}
          {changedResults.length > 0 && (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ borderBottom: "2px solid var(--havn-border)", textAlign: "left" }}>
                  <th style={thStyle} scope="col">Model</th>
                  <th style={{ ...thStyle, textAlign: "right" }} scope="col">Before</th>
                  <th style={{ ...thStyle, textAlign: "right" }} scope="col">After</th>
                  <th style={{ ...thStyle, textAlign: "right" }} scope="col">Added</th>
                  <th style={{ ...thStyle, textAlign: "right" }} scope="col">Removed</th>
                  <th style={{ ...thStyle, textAlign: "right" }} scope="col">Modified</th>
                  <th style={thStyle} scope="col">Schema</th>
                </tr>
              </thead>
              <tbody>
                {changedResults.map((r) => (
                  <React.Fragment key={r.model}>
                    <tr
                      onClick={() =>
                        setExpandedModel(expandedModel === r.model ? null : r.model)
                      }
                      style={{
                        borderBottom: "1px solid var(--havn-border)",
                        cursor: "pointer",
                        background:
                          expandedModel === r.model
                            ? "var(--bg-secondary)"
                            : "transparent",
                      }}
                    >
                      <td style={tdStyle}>
                        <strong>{r.model}</strong>
                      </td>
                      <td style={{ ...tdStyle, textAlign: "right" }}>
                        {r.error ? "" : r.is_new ? "NEW" : r.total_before.toLocaleString()}
                      </td>
                      <td style={{ ...tdStyle, textAlign: "right" }}>
                        {r.error ? "" : r.total_after.toLocaleString()}
                      </td>
                      <td
                        style={{
                          ...tdStyle,
                          textAlign: "right",
                          color: r.added ? "var(--havn-green)" : undefined,
                        }}
                      >
                        {r.error ? "" : r.added ? `+${r.added}` : "0"}
                      </td>
                      <td
                        style={{
                          ...tdStyle,
                          textAlign: "right",
                          color: r.removed ? "var(--havn-red)" : undefined,
                        }}
                      >
                        {r.error ? "" : r.removed || "0"}
                      </td>
                      <td
                        style={{
                          ...tdStyle,
                          textAlign: "right",
                          color: r.modified ? "var(--havn-yellow)" : undefined,
                        }}
                      >
                        {r.error ? "" : r.modified || "0"}
                      </td>
                      <td style={tdStyle}>
                        {r.error ? (
                          <span style={{ color: "var(--havn-red)" }}>ERROR</span>
                        ) : (
                          <span
                            style={{
                              color:
                                r.schema_changes && r.schema_changes.length
                                  ? "var(--havn-accent)"
                                  : undefined,
                            }}
                          >
                            {schemaLabel(r.schema_changes)}
                          </span>
                        )}
                      </td>
                    </tr>
                    {expandedModel === r.model && (
                      <tr>
                        <td colSpan={7} style={{ padding: "8px 12px" }}>
                          <ExpandedDiff result={r} />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          )}

          {changedResults.length === 0 && (
            <div style={{ padding: "16px 12px", color: "var(--havn-text-secondary)", fontSize: 13 }}>
              No changes detected.
            </div>
          )}

          {/* Skipped models toggle */}
          {skippedResults.length > 0 && (
            <div style={{ padding: "8px 12px", borderTop: "1px solid var(--havn-border)" }}>
              <button
                onClick={() => setShowSkipped(!showSkipped)}
                style={{
                  background: "none", border: "none", color: "var(--havn-text-dim)",
                  cursor: "pointer", fontSize: "12px", padding: 0,
                }}
              >
                {showSkipped ? "\u25BE" : "\u25B8"} {skippedResults.length} unchanged model{skippedResults.length !== 1 ? "s" : ""}
              </button>
              {showSkipped && (
                <div style={{ marginTop: "6px", fontSize: "12px", color: "var(--havn-text-dim)", fontFamily: "var(--havn-font-mono)" }}>
                  {skippedResults.map((r) => (
                    <div key={r.model} style={{ padding: "2px 0" }}>{r.model}</div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ExpandedDiff({ result }) {
  if (result.error) {
    return (
      <div style={{ color: "var(--havn-red)", padding: 8 }}>
        Error: {result.error}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {result.schema_changes && result.schema_changes.length > 0 && (
        <div>
          <h4 style={{ margin: "0 0 8px 0", fontSize: 13 }}>Schema Changes</h4>
          {result.schema_changes.map((sc, i) => (
            <div key={i} style={{ fontSize: 12, padding: "2px 0" }}>
              {sc.change_type === "added" && (
                <span style={{ color: "var(--havn-green)" }}>
                  + {sc.column} ({sc.new_type})
                </span>
              )}
              {sc.change_type === "removed" && (
                <span style={{ color: "var(--havn-red)" }}>
                  - {sc.column} ({sc.old_type})
                </span>
              )}
              {sc.change_type === "type_changed" && (
                <span style={{ color: "var(--havn-yellow)" }}>
                  ~ {sc.column}: {sc.old_type} → {sc.new_type}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {result.sample_added && result.sample_added.length > 0 && (
        <div>
          <h4 style={{ margin: "0 0 8px 0", fontSize: 13, color: "var(--havn-green)" }}>
            Added Rows ({result.added})
          </h4>
          <SortableTable
            columns={Object.keys(result.sample_added[0])}
            rows={result.sample_added.map((r) => Object.values(r))}
          />
        </div>
      )}

      {result.sample_removed && result.sample_removed.length > 0 && (
        <div>
          <h4 style={{ margin: "0 0 8px 0", fontSize: 13, color: "var(--havn-red)" }}>
            Removed Rows ({result.removed})
          </h4>
          <SortableTable
            columns={Object.keys(result.sample_removed[0])}
            rows={result.sample_removed.map((r) => Object.values(r))}
          />
        </div>
      )}

      {result.sample_modified && result.sample_modified.length > 0 && (
        <div>
          <h4 style={{ margin: "0 0 8px 0", fontSize: 13, color: "var(--havn-yellow)" }}>
            Modified Rows ({result.modified})
          </h4>
          <SortableTable
            columns={Object.keys(result.sample_modified[0])}
            rows={result.sample_modified.map((r) => Object.values(r))}
          />
        </div>
      )}

      {!result.sample_added?.length &&
        !result.sample_removed?.length &&
        !result.sample_modified?.length &&
        !result.schema_changes?.length && (
          <div style={{ color: "var(--havn-text-secondary)", fontSize: 13 }}>
            No changes detected for this model.
          </div>
        )}
    </div>
  );
}

const thStyle = { padding: "8px 12px", fontWeight: 600 };
const tdStyle = { padding: "8px 12px" };

const segStyles = {
  group: {
    display: "inline-flex",
    background: "var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
    overflow: "hidden",
    gap: "1px",
  },
  btn: {
    padding: "4px 12px",
    background: "var(--havn-btn-bg)",
    border: "none",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: "11px",
    fontWeight: 500,
    whiteSpace: "nowrap",
  },
  btnActive: {
    padding: "4px 12px",
    background: "var(--havn-bg-secondary)",
    border: "none",
    color: "var(--havn-text)",
    cursor: "pointer",
    fontSize: "11px",
    fontWeight: 600,
    whiteSpace: "nowrap",
  },
};

import React, { useState } from "react";
import { api } from "./api";
import FocusTrap from "./FocusTrap";

/**
 * Dialog for creating new SQL or Python models, notebooks, or ingest scripts.
 */

// What each model language can be materialized as. A Python model's rows
// only exist once its function has run, so it cannot be a view.
const MATERIALIZATIONS = {
  model: ["table", "view"],
  python: ["table", "incremental"],
};
export default function NewModelDialog({ onClose, onCreated }) {
  const [type, setType] = useState("model"); // "model", "python", "notebook", "ingest"
  const [name, setName] = useState("");
  const [schema, setSchema] = useState("bronze");
  const [materialized, setMaterialized] = useState("table");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState(null);

  const handleCreate = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      if (type === "model" || type === "python") {
        const language = type === "python" ? "python" : "sql";
        const result = await api.createModel(name, schema, materialized, "", language);
        if (onCreated) onCreated(result);
      } else if (type === "notebook") {
        const result = await api.createNotebook(name, name);
        if (onCreated) onCreated(result);
      } else if (type === "ingest") {
        const path = `ingest/${name}.py`;
        const content = `"""Ingest script: ${name}."""\n\nimport duckdb\n\ndb.execute("CREATE SCHEMA IF NOT EXISTS landing")\n# Add your ingest logic here\nprint("Done")\n`;
        await api.saveFile(path, content);
        if (onCreated) onCreated({ path });
      }
      onClose();
    } catch (e) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <FocusTrap labelledBy="havn-new-model-dialog-title" className="dialog-overlay" onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}>
      <div className="dialog" onClick={(e) => e.stopPropagation()} style={{ background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "8px", padding: "20px", width: "400px", color: "var(--havn-text)" }}>
        <h3 id="havn-new-model-dialog-title" style={{ margin: "0 0 16px" }}>New</h3>

        <div style={{ marginBottom: "12px" }}>
          <label htmlFor="new-model-type" style={{ display: "block", marginBottom: "4px", fontSize: "12px", color: "var(--havn-text-secondary)" }}>Type</label>
          <select id="new-model-type" value={type} onChange={(e) => {
            const next = e.target.value;
            setType(next);
            // Keep the materialization valid for the new kind of model.
            const allowed = MATERIALIZATIONS[next];
            if (allowed && !allowed.includes(materialized)) setMaterialized(allowed[0]);
          }} style={{ width: "100%", padding: "6px", background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "4px" }}>
            <option value="model">SQL Model</option>
            <option value="python">Python Model</option>
            <option value="notebook">Notebook</option>
            <option value="ingest">Ingest Script</option>
          </select>
        </div>

        <div style={{ marginBottom: "12px" }}>
          <label htmlFor="new-model-name" style={{ display: "block", marginBottom: "4px", fontSize: "12px", color: "var(--havn-text-secondary)" }}>Name</label>
          <input
            id="new-model-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={type === "model" || type === "python" ? "my_model" : type === "notebook" ? "my_notebook" : "my_ingest"}
            style={{ width: "100%", padding: "6px", background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "4px", boxSizing: "border-box" }}
            autoFocus
            aria-required="true"
            aria-invalid={error ? "true" : undefined}
            onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
          />
        </div>

        {(type === "model" || type === "python") && (
          <>
            <div style={{ marginBottom: "12px" }}>
              <label htmlFor="new-model-schema" style={{ display: "block", marginBottom: "4px", fontSize: "12px", color: "var(--havn-text-secondary)" }}>Schema</label>
              <select id="new-model-schema" value={schema} onChange={(e) => setSchema(e.target.value)} style={{ width: "100%", padding: "6px", background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "4px" }}>
                <option value="bronze">bronze</option>
                <option value="silver">silver</option>
                <option value="gold">gold</option>
              </select>
            </div>
            <div style={{ marginBottom: "12px" }}>
              <label htmlFor="new-model-materialized" style={{ display: "block", marginBottom: "4px", fontSize: "12px", color: "var(--havn-text-secondary)" }}>Materialization</label>
              <select id="new-model-materialized" value={materialized} onChange={(e) => setMaterialized(e.target.value)} style={{ width: "100%", padding: "6px", background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "4px" }}>
                {MATERIALIZATIONS[type].map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
            {type === "python" && (
              <p style={{ margin: "0 0 12px", fontSize: "12px", color: "var(--havn-text-secondary)", lineHeight: 1.5 }}>
                Creates <code>transform/{schema}/{name.trim() || "my_model"}.py</code> with a <code>@model</code> function.
                Read other models with <code>ref("schema.name")</code> and return a relation or DataFrame.
              </p>
            )}
          </>
        )}

        {error && <div role="alert" style={{ color: "var(--havn-red)", fontSize: "12px", marginBottom: "8px" }}>{error}</div>}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
          <button onClick={onClose} className="btn-sm">Cancel</button>
          <button onClick={handleCreate} disabled={creating} className="btn-sm btn-primary">
            {creating ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </FocusTrap>
  );
}

import React, { useState } from "react";
import { api } from "./api";
import FocusTrap from "./FocusTrap";

/**
 * Dialog for creating new SQL models, notebooks, or ingest scripts.
 */
export default function NewModelDialog({ onClose, onCreated }) {
  const [type, setType] = useState("model"); // "model", "notebook", "ingest"
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
      if (type === "model") {
        const result = await api.createModel(name, schema, materialized);
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
          <select id="new-model-type" value={type} onChange={(e) => setType(e.target.value)} style={{ width: "100%", padding: "6px", background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "4px" }}>
            <option value="model">SQL Model</option>
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
            placeholder={type === "model" ? "my_model" : type === "notebook" ? "my_notebook" : "my_ingest"}
            style={{ width: "100%", padding: "6px", background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "4px", boxSizing: "border-box" }}
            autoFocus
            aria-required="true"
            aria-invalid={error ? "true" : undefined}
            onKeyDown={(e) => { if (e.key === "Enter") handleCreate(); }}
          />
        </div>

        {type === "model" && (
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
                <option value="table">table</option>
                <option value="view">view</option>
              </select>
            </div>
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

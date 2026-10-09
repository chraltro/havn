import React, { useState, useEffect } from "react";
import { api } from "./api";

/*
 * One change set proposed by an agent: its files, the verification report
 * (one row per check) and the data diff, with Apply / Discard. "Ready to
 * apply" only shows once every check passed.
 */

const STATUS_COLOR = {
  pass: "var(--havn-green)",
  fail: "var(--havn-red)",
  warn: "var(--havn-yellow)",
  skip: "var(--havn-text-secondary)",
};

const CHECK_LABELS = {
  safety: "Read-only SQL",
  validate: "Validate",
  bind: "Bind",
  unit_tests: "Unit tests",
  build: "Scratch build",
  assertions: "Assertions",
  contracts: "Contracts",
  data_diff: "Data diff",
  verify: "Verification",
};

function detailText(d) {
  if (typeof d !== "object" || d === null) return String(d);
  if (d.message) return `${d.model ? d.model + ": " : ""}${d.message}`;
  if (d.contract) return `${d.contract} (${d.where}): ${d.passed ? "pass" : d.error || (d.results || []).filter((r) => !r.passed).map((r) => `${r.expression}: ${r.detail}`).join("; ")}`;
  if (d.expression) return `${d.model}: ${d.expression} ${d.passed ? "passed" : `failed: ${d.detail}`}`;
  if (d.status && d.name) return `${d.model} · ${d.name}: ${d.status}${d.message ? ` (${d.message})` : ""}`;
  if (d.status) return `${d.model}: ${d.status}${d.error ? ` (${d.error})` : d.rows != null ? ` (${d.rows} rows)` : ""}`;
  return JSON.stringify(d);
}

function Check({ check }) {
  const [open, setOpen] = useState(check.status === "fail");
  const details = check.details || [];
  return (
    <div style={st.check}>
      <div style={st.checkHead} onClick={() => details.length && setOpen(!open)}>
        <span style={{ ...st.badge, color: STATUS_COLOR[check.status], borderColor: STATUS_COLOR[check.status] }}>
          {check.status}
        </span>
        <span style={st.checkName}>{CHECK_LABELS[check.name] || check.name}</span>
        <span style={st.dim}>{check.summary}</span>
      </div>
      {open && details.length > 0 && (
        <ul style={st.details}>
          {details.slice(0, 12).map((d, i) => <li key={i}>{detailText(d)}</li>)}
        </ul>
      )}
    </div>
  );
}

export default function ChangeSetCard({ changeset, onChanged, onOpenFile }) {
  const [cs, setCs] = useState(changeset);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // A newer report arriving over the socket replaces what this card shows.
  useEffect(() => { setCs(changeset); }, [changeset]);
  const report = cs.report || {};
  const closed = cs.status === "applied" || cs.status === "discarded";
  const stale = cs.stale_files || [];

  const act = async (fn, confirmText) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    setError(null);
    try {
      const next = await fn();
      setCs(next);
      onChanged?.(next);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const headline = {
    verifying: "Verifying…",
    pending: "Not verified yet",
    ready: "Ready to apply",
    failed: "Verification failed",
    applied: "Applied",
    discarded: "Discarded",
  }[cs.status] || cs.status;

  return (
    <div style={st.card} aria-label={`Change set ${cs.id}`}>
      <div style={st.head}>
        <span style={{ ...st.status, color: cs.status === "ready" || cs.status === "applied" ? "var(--havn-green)" : cs.status === "failed" ? "var(--havn-red)" : "var(--havn-text-secondary)" }}>
          {headline}
        </span>
        <span style={st.dim}>rev {cs.revision} · {cs.source}</span>
      </div>
      {cs.title && <div style={st.title}>{cs.title}</div>}
      <div style={st.files}>
        {(cs.files || []).map((f) => (
          <div key={f.path} style={st.file}>
            <span style={st.action}>{f.action}</span>
            <button style={st.fileLink} onClick={() => onOpenFile?.(f.path)} disabled={f.action === "delete"}>{f.path}</button>
          </div>
        ))}
        {(cs.ignored || []).map((p) => (
          <div key={p} style={{ ...st.file, ...st.dim }}>not carried over: {p}</div>
        ))}
      </div>
      {(report.checks || []).map((c) => <Check key={c.name} check={c} />)}
      {(report.diffs || []).length > 0 && (
        <div style={st.diffs}>
          {report.diffs.map((d) => (
            <div key={d.model} style={st.diffRow}>
              <span style={st.mono}>{d.model}</span>
              {d.removed_model ? <span style={st.dim}>model removed</span>
                : d.error ? <span style={{ color: "var(--havn-red)" }}>{d.error}</span>
                : (
                  <span style={st.dim}>
                    {d.is_new ? "new, " : ""}{d.total_before} → {d.total_after} rows
                    <span style={{ color: "var(--havn-green)" }}> +{d.added}</span>
                    <span style={{ color: "var(--havn-red)" }}> −{d.removed}</span>
                    {d.modified ? <span style={{ color: "var(--havn-yellow)" }}> ~{d.modified}</span> : null}
                    {d.schema_changes?.length ? ` · ${d.schema_changes.map((s) => `${s.change_type} ${s.column}`).join(", ")}` : ""}
                  </span>
                )}
            </div>
          ))}
        </div>
      )}
      {stale.length > 0 && !closed && (
        <div style={st.warn}>Changed on disk since proposed: {stale.join(", ")}. Re-verify before applying.</div>
      )}
      {error && <div style={st.err}>{error}</div>}
      {!closed && cs.status !== "verifying" && (
        <div style={st.actions}>
          {cs.status === "ready" && !stale.length && (
            <button style={st.apply} disabled={busy} onClick={() => act(() => api.applyChangeSet(cs.id))}>Apply</button>
          )}
          {cs.status === "failed" && !stale.length && (
            <button style={st.ghost} disabled={busy}
              onClick={() => act(() => api.applyChangeSet(cs.id, true), "Verification failed. Apply these changes anyway?")}>
              Apply anyway
            </button>
          )}
          <button style={st.ghost} disabled={busy} onClick={() => act(() => api.verifyChangeSet(cs.id))}>Re-verify</button>
          <button style={st.ghost} disabled={busy} onClick={() => act(() => api.discardChangeSet(cs.id))}>Discard</button>
        </div>
      )}
    </div>
  );
}

const st = {
  card: { border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius, 6px)", padding: 10, display: "flex", flexDirection: "column", gap: 6, background: "var(--havn-bg-secondary)", fontSize: 12 },
  head: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 },
  status: { fontWeight: 600, fontSize: 13 },
  title: { color: "var(--havn-text)" },
  dim: { color: "var(--havn-text-secondary)" },
  files: { display: "flex", flexDirection: "column", gap: 2 },
  file: { display: "flex", gap: 6, alignItems: "baseline" },
  action: { fontSize: 10, textTransform: "uppercase", color: "var(--havn-text-secondary)", width: 44, flexShrink: 0 },
  fileLink: { background: "none", border: "none", padding: 0, color: "var(--havn-accent)", cursor: "pointer", fontFamily: "var(--havn-font-mono)", fontSize: 12, textAlign: "left" },
  check: { display: "flex", flexDirection: "column" },
  checkHead: { display: "flex", gap: 6, alignItems: "baseline", cursor: "pointer" },
  badge: { fontSize: 9, textTransform: "uppercase", fontWeight: 700, border: "1px solid", borderRadius: 3, padding: "0 4px", width: 30, textAlign: "center", flexShrink: 0 },
  checkName: { color: "var(--havn-text)", fontWeight: 500, flexShrink: 0 },
  details: { margin: "2px 0 2px 40px", padding: "0 0 0 12px", color: "var(--havn-text-secondary)", wordBreak: "break-word" },
  diffs: { borderTop: "1px solid var(--havn-border)", paddingTop: 6, display: "flex", flexDirection: "column", gap: 2 },
  diffRow: { display: "flex", gap: 8, flexWrap: "wrap" },
  mono: { fontFamily: "var(--havn-font-mono)", color: "var(--havn-text)" },
  warn: { color: "var(--havn-yellow)" },
  err: { color: "var(--havn-red)" },
  actions: { display: "flex", gap: 6, marginTop: 2 },
  apply: { padding: "4px 12px", background: "var(--havn-green)", color: "var(--havn-bg)", border: "none", borderRadius: 4, cursor: "pointer", fontWeight: 600, fontSize: 12 },
  ghost: { padding: "4px 10px", background: "transparent", color: "var(--havn-text-secondary)", border: "1px solid var(--havn-border)", borderRadius: 4, cursor: "pointer", fontSize: 12 },
};

import React, { useState, useEffect, useCallback } from "react";
import { api } from "./api";

/*
 * Data changes for the checked-out branch (branch warehouses).
 *
 * The branch warehouse holds only what the branch changed; everything else is
 * read from the base. This view shows what is built locally, builds what is
 * missing (`havn branch build`), and diffs every branch model against the
 * base row by row (`havn branch diff`) -- the same report CI posts on the
 * pull request, which "Copy as markdown" puts on the clipboard.
 */

const STATUS_STYLE = {
  changed: "warn",
  added: "ok",
  removed: "bad",
  not_built: "dim",
  error: "bad",
  unchanged: "dim",
};
const STATUS_LABEL = { not_built: "not built", added: "new" };
const ORDER = { error: 0, changed: 1, added: 2, removed: 3, not_built: 4, unchanged: 5 };

function n(v) {
  return v == null ? "–" : Number(v).toLocaleString();
}

export default function BranchDataChanges({ addOutput }) {
  const [status, setStatus] = useState(null);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null); // "status" | "build" | "diff"
  const [copied, setCopied] = useState(false);

  const loadDiff = useCallback(async () => {
    setBusy("diff");
    try {
      setReport(await api.diffBranch());
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  }, []);

  const loadStatus = useCallback(async () => {
    setBusy("status");
    try {
      const st = await api.getBranchStatus();
      setStatus(st);
      setError(null);
      setBusy(null);
      if (st.active && st.base?.readable) await loadDiff();
    } catch (e) {
      setError(e.message);
      setBusy(null);
    }
  }, [loadDiff]);

  useEffect(() => { loadStatus(); }, [loadStatus]);

  async function build() {
    setBusy("build");
    setError(null);
    try {
      const r = await api.buildBranch();
      const failed = Object.keys(r.failed || {});
      if (failed.length) {
        addOutput?.("error", `Branch build failed: ${failed.map((m) => `${m} (${r.failed[m]})`).join(", ")}`);
      } else {
        addOutput?.("info", `Branch build: ${r.built.length} built, ${r.pruned.length} pruned, everything else read from ${r.base}`);
      }
    } catch (e) {
      setError(e.message);
      setBusy(null);
      return;
    }
    await loadStatus();
  }

  async function copyMarkdown() {
    if (!report?.markdown) return;
    try {
      await navigator.clipboard.writeText(report.markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      addOutput?.("warn", "Could not copy to the clipboard");
    }
  }

  if (!status) {
    return <div style={st.dim}>{error ? <span style={st.bad}>{error}</span> : "Loading the branch…"}</div>;
  }
  if (!status.active) {
    return (
      <div style={{ ...st.card, ...st.pad }}>
        <div style={st.title}>No branch warehouse</div>
        <div style={st.dim}>
          {status.branch ? <>On <span style={st.mono}>{status.branch}</span>: </> : null}
          {status.reason}.
        </div>
        {!status.enabled && (
          <div style={{ ...st.dim, marginTop: 8 }}>
            Add <span style={st.mono}>branches: {"{enabled: true}"}</span> to project.yml to give every git branch its own warehouse.
          </div>
        )}
      </div>
    );
  }

  const models = status.models || {};
  const local = models.local || [];
  const needs = models.needs_build || [];
  const prunable = models.prunable || [];
  const staleModels = local.filter((m) => m.stale);
  const entries = (report?.models || []).slice().sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9));
  const moving = entries.filter((e) => e.status !== "unchanged");
  const still = entries.length - moving.length;
  const outOfDate = needs.length > 0 || prunable.length > 0 || staleModels.length > 0;

  return (
    <div data-testid="branch-data-changes">
      <div style={st.hero}>
        <div style={{ minWidth: 0 }}>
          <h1 style={st.h1}>Data changes</h1>
          <div style={st.refs}>
            <span style={st.ref}>{status.branch}</span><span aria-hidden="true">→</span>
            <span style={st.ref}>{status.base?.label}</span>
            <span>· {local.length} of {models.total ?? 0} models built on this branch, {(models.deferred || []).length} read from {status.base?.label}</span>
          </div>
          <div style={{ ...st.dim, marginTop: 6, fontSize: 12 }}>
            Warehouse <span style={st.mono}>{status.warehouse?.path}</span>
            {status.base?.path ? <> · base <span style={st.mono}>{status.base.path}</span></> : null}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
          <button style={st.btn} onClick={loadStatus} disabled={!!busy}>Refresh</button>
          <button style={outOfDate ? st.btnPrimary : st.btn} onClick={build} disabled={!!busy || !status.base?.readable}>
            {busy === "build" ? "Building…" : "Build branch"}
          </button>
        </div>
      </div>

      {error && <div style={{ ...st.bad, marginBottom: 12 }}>{error}</div>}
      {!status.base?.readable && (
        <div style={{ ...st.card, ...st.pad, marginBottom: 16 }}>
          <span style={st.bad}>The base cannot be read.</span>{" "}
          <span style={st.dim}>{status.base?.reason}</span>
        </div>
      )}
      {outOfDate && status.base?.readable && (
        <div style={{ ...st.notice, marginBottom: 16 }} role="status">
          {needs.length > 0 && <div>{needs.length} model{needs.length === 1 ? "" : "s"} differ from {status.base.label} and are not built yet: <span style={st.mono}>{needs.join(", ")}</span></div>}
          {prunable.length > 0 && <div>{prunable.length} branch cop{prunable.length === 1 ? "y matches" : "ies match"} the base again and will be dropped: <span style={st.mono}>{prunable.join(", ")}</span></div>}
          {staleModels.map((m) => (
            <div key={m.name}><span style={st.mono}>{m.name}</span> is stale: {m.stale_reasons.join("; ")}</div>
          ))}
          <div style={{ marginTop: 4 }}>Build the branch to bring it up to date.</div>
        </div>
      )}

      <div style={st.sectionHead}>
        <h2 style={st.h2}>Rows and schema vs {status.base?.label}</h2>
        {report?.markdown && (
          <button style={st.link} onClick={copyMarkdown}>{copied ? "Copied" : "Copy as markdown"}</button>
        )}
      </div>
      <div style={st.card}>
        {busy === "diff" && !report && <div style={{ ...st.pad, ...st.dim }}>Diffing the branch against the base…</div>}
        {report && (
          <div style={st.meta}>
            {moving.length} model{moving.length === 1 ? "" : "s"} differ{moving.length === 1 ? "s" : ""}
            {still ? `, ${still} rebuilt with identical data` : ""}
            {report.commit ? ` · ${report.commit.slice(0, 7)}` : ""}
          </div>
        )}
        {report && entries.length === 0 && (
          <div style={{ ...st.pad, ...st.dim }}>Nothing on this branch differs from {status.base?.label}: every model reads from it.</div>
        )}
        {moving.map((e) => <DiffRow key={e.model} e={e} />)}
      </div>
    </div>
  );
}

function DiffRow({ e }) {
  const [open, setOpen] = useState(false);
  const tone = st[STATUS_STYLE[e.status] || "dim"];
  const samples = [
    ["Added", e.sample_added, e.added],
    ["Removed", e.sample_removed, e.removed],
    ["Modified (branch values)", e.sample_modified, e.modified],
  ].filter(([, rows]) => rows && rows.length);
  return (
    <div style={st.row} data-testid="branch-diff-row">
      <div style={st.rowTop}>
        <span style={st.mono}>{e.model}</span>
        <span style={{ ...st.badge, color: tone.color, borderColor: tone.color }}>{STATUS_LABEL[e.status] || e.status}</span>
        <span style={st.nums}>
          <span>{n(e.before)} → {n(e.after)} rows</span>
          {e.added ? <span style={st.ok}> +{n(e.added)}</span> : null}
          {e.removed ? <span style={st.bad}> −{n(e.removed)}</span> : null}
          {e.modified ? <span style={st.warn}> ~{n(e.modified)}</span> : null}
        </span>
      </div>
      {e.error && <div style={{ ...st.bad, fontSize: 12, marginTop: 4 }}>{e.error}</div>}
      {e.status === "not_built" && <div style={{ ...st.dim, fontSize: 12, marginTop: 4 }}>Planned for this branch but not built yet.</div>}
      {e.schema_changes?.length > 0 && (
        <div style={st.schema}>
          {e.schema_changes.map((c) => (
            <div key={c.column + c.change}>
              {c.change === "added" && <span style={st.ok}>+ {c.column} <span style={st.dimInline}>{c.new_type}</span></span>}
              {c.change === "removed" && <span style={st.bad}>− {c.column} <span style={st.dimInline}>{c.old_type}</span></span>}
              {c.change === "type_changed" && <span style={st.warn}>~ {c.column} <span style={st.dimInline}>{c.old_type} → {c.new_type}</span></span>}
            </div>
          ))}
        </div>
      )}
      {samples.length > 0 && (
        <button style={{ ...st.link, marginTop: 6 }} onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Hide sample rows" : "Sample rows"}{e.primary_key ? ` (key: ${e.primary_key.join(", ")})` : ""}
        </button>
      )}
      {open && samples.map(([label, rows, total]) => (
        <div key={label} style={{ marginTop: 8 }}>
          <div style={st.sampleLabel}>{label} · {n(total)} row{total === 1 ? "" : "s"}{rows.length < total ? `, showing ${rows.length}` : ""}</div>
          <SampleTable rows={rows.slice(0, 20)} />
        </div>
      ))}
    </div>
  );
}

function SampleTable({ rows }) {
  const cols = Object.keys(rows[0] || {});
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={st.table}>
        <thead>
          <tr>{cols.map((c) => <th key={c} style={st.th}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c} style={st.td}>{r[c] == null ? <i style={st.dimInline}>NULL</i> : String(r[c])}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const btn = {
  border: "1px solid var(--havn-btn-border)", background: "var(--havn-btn-bg)", color: "var(--havn-text)",
  borderRadius: "var(--havn-radius)", padding: "5px 12px", fontSize: 13, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
};

const st = {
  h1: { fontSize: 20, fontWeight: 500, margin: 0 },
  h2: { fontSize: 12, fontWeight: 500, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: ".06em", margin: 0 },
  title: { fontSize: 14, fontWeight: 500, marginBottom: 4 },
  hero: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 18 },
  refs: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginTop: 8, fontSize: 12.5, color: "var(--havn-text-secondary)" },
  ref: { fontFamily: "var(--havn-font-mono)", fontSize: 12, padding: "1px 9px", borderRadius: 20, border: "1px solid var(--havn-border-light)", color: "var(--havn-text)" },
  sectionHead: { display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 },
  dim: { color: "var(--havn-text-secondary)", fontSize: 13, lineHeight: 1.5 },
  dimInline: { color: "var(--havn-text-dim)" },
  ok: { color: "var(--havn-green)" },
  warn: { color: "var(--havn-yellow)" },
  bad: { color: "var(--havn-red)" },
  mono: { fontFamily: "var(--havn-font-mono)", fontSize: 12.5, overflowWrap: "anywhere" },
  card: { background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)" },
  pad: { padding: "14px 16px" },
  notice: {
    fontSize: 12.5, lineHeight: 1.6, color: "var(--havn-text)", padding: "10px 14px",
    border: "1px solid var(--havn-yellow)", borderRadius: "var(--havn-radius)",
    background: "color-mix(in srgb, var(--havn-yellow) 8%, transparent)",
  },
  meta: { fontSize: 12, color: "var(--havn-text-secondary)", padding: "10px 16px", borderBottom: "1px solid var(--havn-border)" },
  row: { padding: "10px 16px", borderBottom: "1px solid var(--havn-border)" },
  rowTop: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" },
  nums: { marginLeft: "auto", fontSize: 12.5, color: "var(--havn-text-secondary)", fontVariantNumeric: "tabular-nums" },
  schema: { fontFamily: "var(--havn-font-mono)", fontSize: 12, marginTop: 6, lineHeight: 1.7 },
  badge: { fontSize: 10.5, padding: "0 7px", borderRadius: 4, border: "1px solid" },
  sampleLabel: { fontSize: 11.5, color: "var(--havn-text-secondary)", marginBottom: 4 },
  table: { borderCollapse: "collapse", fontFamily: "var(--havn-font-mono)", fontSize: 11.5 },
  th: { textAlign: "left", padding: "3px 10px", borderBottom: "1px solid var(--havn-border-light)", color: "var(--havn-text-secondary)", fontWeight: 500, whiteSpace: "nowrap" },
  td: { padding: "3px 10px", borderBottom: "1px solid var(--havn-border)", whiteSpace: "nowrap", maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis" },
  link: { background: "none", border: "none", padding: 0, color: "var(--havn-accent)", cursor: "pointer", fontSize: 12.5, fontFamily: "inherit" },
  btn,
  btnPrimary: { ...btn, background: "var(--havn-accent)", border: "1px solid var(--havn-accent)", color: "#fff", fontWeight: 500 },
};

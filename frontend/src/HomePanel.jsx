import React, { useState, useEffect, useCallback } from "react";
import { api } from "./api";

/*
 * Home: "is my data OK, and if not, what do I click?" Four health tiles, a
 * ranked attention queue with the next action on each row, the last 24 hours
 * of runs, and every model per layer with its status. One GET /api/home.
 * Before the warehouse has any data it shows the first-run panel instead.
 */

export function timeAgo(dateStr) {
  if (!dateStr) return "";
  const d = new Date(/[TZ]/.test(dateStr) ? dateStr : dateStr.replace(" ", "T"));
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (Number.isNaN(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtDuration(ms) {
  if (ms == null) return "–";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return { value: v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1), unit: units[i] };
}

/** A pipeline run's name: its one target, or how many steps it ran. */
export function runLabel(run) {
  if (run.model_count === 1 && run.target) return run.target;
  return `${run.model_count} step${run.model_count === 1 ? "" : "s"}`;
}

const STATUS = {
  failing: { color: "var(--havn-red)", label: "failing" },
  blocked: { color: "var(--havn-red)", label: "blocked" },
  changed: { color: "var(--havn-yellow)", label: "changed" },
  never_built: { color: "var(--havn-text-dim)", label: "not built" },
  fresh: { color: "var(--havn-green)", label: "fresh" },
  source: { color: "var(--havn-accent)", label: "source" },
};

const LAYER_PREVIEW = 8;

export default function HomePanel({
  running, refreshKey, onNavigate, onOpenFile, onRunPipeline, onQuery, onClearSample,
  onAttentionCount, firstRun,
}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const d = await api.getHome();
      setData(d);
      setError(null);
      onAttentionCount?.((d.attention || []).filter((a) => a.severity === "error").length);
    } catch (e) {
      setError(e.message);
    }
  }, [onAttentionCount]);

  // Reload when a run finishes (the parent bumps refreshKey) or stops.
  useEffect(() => { if (!running) load(); }, [load, running, refreshKey]);

  if (error && !data) {
    return (
      <div style={s.page}>
        <div style={s.errorBox}>
          Could not load pipeline health: {error}
          <button style={{ ...s.btn, marginLeft: 12 }} onClick={load}>Retry</button>
        </div>
      </div>
    );
  }
  if (!data) return <div style={s.page}><div style={s.dim}>Loading…</div></div>;
  if (!data.has_data) return firstRun;

  const t = data.tiles;
  const last = t.last_run;
  const totalChecks = t.checks.passed + t.checks.failed + t.checks.warned;
  const size = fmtBytes(t.warehouse.size_bytes);

  return (
    <div style={s.page}>
      {data.is_sample && (
        <div style={s.banner}>
          <span>You're running the sample earthquake project.</span>
          <button style={s.btn} onClick={onClearSample}>Start fresh</button>
        </div>
      )}

      <div style={s.head}>
        <div>
          <h1 style={s.h1}>Pipeline health</h1>
          <div style={s.sub}>
            {last
              ? <>Last run {timeAgo(last.started_at)} · {runLabel(last)}</>
              : "No pipeline runs yet"}
          </div>
        </div>
        <button style={s.btnPrimary} onClick={onRunPipeline} disabled={running}
                title={running ? "A run is already in progress" : "Run every ingest, transform and export step"}>
          {running ? "Running…" : "▶ Run pipeline"}
        </button>
      </div>

      {/* Tiles */}
      <section style={s.tiles} aria-label="Health summary">
        <Tile
          label="Models"
          value={t.models.total}
          detail={t.models.total === 0 ? "No models yet"
            : [
                t.models.changed ? <span key="c" style={s.warn}>{t.models.changed} changed</span> : null,
                t.models.never_built ? <span key="n">{t.models.never_built} not built</span> : null,
                <span key="u">{t.models.up_to_date} up to date</span>,
              ].filter(Boolean).reduce((acc, el, i) => (i ? [...acc, " · ", el] : [el]), [])}
          onClick={() => onNavigate("DAG")}
        />
        <Tile
          label="Checks passing"
          value={t.checks.passed}
          unit={totalChecks ? ` / ${totalChecks}` : null}
          detail={
            t.checks.failed || t.checks.warned || t.checks.contracts_failed
              ? [
                  t.checks.failed ? <span key="f" style={s.bad}>{t.checks.failed} failing</span> : null,
                  t.checks.warned ? <span key="w" style={s.warn}>{t.checks.warned} warning{t.checks.warned === 1 ? "" : "s"}</span> : null,
                  t.checks.contracts_failed ? <span key="k" style={s.bad}>{t.checks.contracts_failed} contract{t.checks.contracts_failed === 1 ? "" : "s"} broken</span> : null,
                ].filter(Boolean).reduce((acc, el, i) => (i ? [...acc, " · ", el] : [el]), [])
              : totalChecks ? <span style={s.ok}>{"✓"} all passing</span> : "No checks yet · add @assert to a model"
          }
          onClick={() => onNavigate("Quality")}
        />
        <Tile
          label="Last run"
          value={last ? fmtDuration(last.duration_ms) : "–"}
          detail={last
            ? <>
                <span style={last.status === "success" ? s.ok : s.bad}>
                  {last.status === "success" ? "✓ success" : `✗ ${last.error_count} failed`}
                </span>
                {last.rows ? ` · ${Number(last.rows).toLocaleString()} rows` : ""}
              </>
            : "Run the pipeline to see results"}
          onClick={() => onNavigate("Runs")}
        />
        <Tile
          label="Warehouse"
          value={typeof size === "string" ? size : size.value}
          unit={typeof size === "string" ? null : ` ${size.unit}`}
          detail={t.warehouse.last_backup
            ? <>last backup {timeAgo(t.warehouse.last_backup.timestamp)} · {t.warehouse.last_backup.verified
                ? <span style={s.ok}>verified</span> : <span style={s.warn}>unverified</span>}</>
            : <span style={s.warn}>no backup yet</span>}
          onClick={() => onNavigate("Settings")}
        />
      </section>

      <div style={s.grid2}>
        <Attention items={data.attention} total={data.attention_total || data.attention.length}
                   onOpenFile={onOpenFile} onQuery={onQuery} onNavigate={onNavigate} />
        <RunsChart runs={data.runs} onNavigate={onNavigate} />
      </div>

      <Layers layers={data.layers} onOpenFile={onOpenFile} />
    </div>
  );
}

function Tile({ label, value, unit, detail, onClick }) {
  return (
    <button type="button" style={s.tile} onClick={onClick}>
      <div style={s.tileLabel}>{label}</div>
      <div style={s.tileValue}>{value}{unit && <small style={s.tileUnit}>{unit}</small>}</div>
      <div style={s.tileDetail}>{detail}</div>
    </button>
  );
}

const KIND_ICON = { build: "!", assertion: "!", contract: "!", freshness: "⏱", anomaly: "~" };

function Attention({ items, total, onOpenFile, onQuery, onNavigate }) {
  return (
    <section aria-labelledby="havn-attention-h">
      <h2 id="havn-attention-h" style={s.h2}>Needs attention</h2>
      {items.length === 0 ? (
        <div style={{ ...s.card, ...s.empty }}>
          <span style={{ ...s.ok, fontSize: 18 }}>{"✓"}</span>
          <div>
            <div style={{ fontWeight: 500 }}>Nothing needs attention</div>
            <div style={s.dim}>No failed builds, checks, contracts, late sources or anomalies.</div>
          </div>
        </div>
      ) : (
        <ul style={{ ...s.card, ...s.list }}>
          {items.map((a, i) => {
            const bad = a.severity === "error";
            return (
              <li key={i} style={s.item}>
                <span
                  style={{ ...s.sev, color: bad ? "var(--havn-red)" : "var(--havn-yellow)",
                           background: bad ? "color-mix(in srgb, var(--havn-red) 13%, transparent)" : "color-mix(in srgb, var(--havn-yellow) 13%, transparent)" }}
                  aria-label={bad ? "error" : "warning"}
                >
                  {KIND_ICON[a.kind] || "!"}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div style={s.itemTitle}>{a.title}</div>
                  <div style={s.itemDetail}>
                    {a.detail}
                    {a.at && <span style={{ color: "var(--havn-text-dim)" }}> · {timeAgo(a.at)}</span>}
                  </div>
                </div>
                <div style={s.acts}>
                  {a.sql && <button style={s.btnSm} onClick={() => onQuery(a.sql)}>See rows</button>}
                  {a.kind === "anomaly" && <button style={s.btnSm} onClick={() => onNavigate("Quality")}>Quality</button>}
                  {a.kind === "build" && !a.path && <button style={s.btnSm} onClick={() => onNavigate("Runs")}>Runs</button>}
                  {a.path && <button style={bad ? s.btnSmPrimary : s.btnSm} onClick={() => onOpenFile(a.path)}>Open</button>}
                </div>
              </li>
            );
          })}
          {total > items.length && (
            <li style={{ ...s.item, gridTemplateColumns: "1fr" }}>
              <button style={s.link} onClick={() => onNavigate("Quality")}>
                {total - items.length} more in Observe
              </button>
            </li>
          )}
        </ul>
      )}
    </section>
  );
}

function RunsChart({ runs, onNavigate }) {
  const [hover, setHover] = useState(null);
  const max = Math.max(1, ...runs.map((r) => r.duration_ms || 0));
  const recent = [...runs].reverse().slice(0, 3);
  return (
    <section aria-labelledby="havn-runs-h">
      <h2 id="havn-runs-h" style={s.h2}>Runs · last 24h</h2>
      <div style={{ ...s.card, padding: 16 }}>
        {runs.length === 0 ? (
          <div style={s.dim}>No pipeline runs in the last 24 hours.</div>
        ) : (
          <>
            <div style={s.chartWrap}>
              <div style={s.bars} role="img"
                   aria-label={`${runs.length} runs, ${runs.filter((r) => r.status !== "success").length} failed. Bar height is duration.`}
                   onMouseLeave={() => setHover(null)}>
                {runs.map((r, i) => {
                  const failed = r.status !== "success";
                  const h = Math.max(4, Math.round(((r.duration_ms || 0) / max) * 100));
                  return (
                    <div key={r.pipeline_run_id} style={s.barSlot} onMouseEnter={() => setHover(i)}>
                      {failed && <span style={s.barMark} aria-hidden="true">{"✗"}</span>}
                      <div style={{
                        ...s.bar, height: `${h}%`,
                        background: failed ? "var(--havn-red)" : "var(--havn-accent)",
                        opacity: hover == null || hover === i ? 1 : 0.55,
                      }} />
                    </div>
                  );
                })}
              </div>
              {hover != null && runs[hover] && (
                <div style={{ ...s.tooltip, left: `${((hover + 0.5) / runs.length) * 100}%` }} role="status">
                  <b>{runs[hover].status === "success" ? "✓ success" : `✗ ${runs[hover].error_count} failed`}</b>
                  <div>{fmtDuration(runs[hover].duration_ms)} · {runs[hover].model_count} steps</div>
                  <div style={{ color: "var(--havn-text-dim)" }}>{timeAgo(runs[hover].started_at)}</div>
                </div>
              )}
            </div>
            <div style={s.axis}>
              <span>{timeAgo(runs[0].started_at)}</span>
              <span>{runs.length > 1 ? timeAgo(runs[runs.length - 1].started_at) : ""}</span>
            </div>
          </>
        )}
        {recent.map((r) => (
          <div key={r.pipeline_run_id} style={s.runRow}>
            <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              <b style={{ fontWeight: 500, color: "var(--havn-text)" }}>{runLabel(r)}</b> · {timeAgo(r.started_at)}
            </span>
            <span style={r.status === "success" ? s.ok : s.bad}>
              {r.status === "success" ? `✓ ${fmtDuration(r.duration_ms)}` : `✗ ${r.error_count} failed`}
            </span>
          </div>
        ))}
        {runs.length > 0 && <button style={{ ...s.link, marginTop: 8 }} onClick={() => onNavigate("Runs")}>All runs</button>}
      </div>
    </section>
  );
}

function Layers({ layers, onOpenFile }) {
  const [expanded, setExpanded] = useState({});
  if (!layers.length) return null;
  return (
    <section aria-labelledby="havn-layers-h">
      <h2 id="havn-layers-h" style={s.h2}>Layers</h2>
      <div style={{ ...s.card, ...s.flow, gridTemplateColumns: `repeat(${Math.min(layers.length, 4)}, minmax(0, 1fr))` }} className="havn-layers">
        {layers.map((layer) => {
          const open = expanded[layer.schema];
          const shown = open ? layer.models : layer.models.slice(0, LAYER_PREVIEW);
          return (
            <div key={layer.schema} style={s.lane}>
              <h3 style={s.h3}>{layer.schema}<span style={s.count}>{layer.models.length}</span></h3>
              {shown.map((m) => {
                const st = STATUS[m.status] || STATUS.fresh;
                const chipStyle = { ...s.chip, ...(m.status === "failing" || m.status === "blocked" ? s.chipBad : null) };
                const title = `${m.full_name} · ${st.label}${m.last_run_at ? ` · built ${timeAgo(m.last_run_at)}` : ""}${m.row_count != null ? ` · ${Number(m.row_count).toLocaleString()} rows` : ""}`;
                const inner = (
                  <>
                    <span style={{ ...s.dot, background: st.color }} aria-hidden="true" />
                    <span style={s.chipName}>{m.name}</span>
                    <em style={s.chipMeta}>{m.status === "fresh" ? timeAgo(m.last_run_at) : st.label}</em>
                  </>
                );
                // Landing tables have no file to open, so they are not buttons.
                return m.path
                  ? <button key={m.full_name} type="button" onClick={() => onOpenFile(m.path)}
                            style={{ ...chipStyle, cursor: "pointer" }} title={title}>{inner}</button>
                  : <div key={m.full_name} style={chipStyle} title={title}>{inner}</div>;
              })}
              {layer.models.length > LAYER_PREVIEW && (
                <button style={s.link} onClick={() => setExpanded((e) => ({ ...e, [layer.schema]: !open }))}>
                  {open ? "Show fewer" : `+ ${layer.models.length - LAYER_PREVIEW} more`}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

const btn = {
  border: "1px solid var(--havn-btn-border)", background: "var(--havn-btn-bg)", color: "var(--havn-text)",
  borderRadius: "var(--havn-radius)", padding: "5px 12px", fontSize: 13, cursor: "pointer",
  fontFamily: "inherit", whiteSpace: "nowrap",
};

const s = {
  page: { padding: "24px 28px 48px", overflow: "auto", height: "100%", boxSizing: "border-box" },
  banner: {
    display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12,
    padding: "10px 14px", marginBottom: 18, fontSize: 13, color: "var(--havn-text-secondary)",
    border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", background: "var(--havn-bg-secondary)",
  },
  head: { display: "flex", justifyContent: "space-between", alignItems: "flex-end", gap: 16, marginBottom: 20, flexWrap: "wrap" },
  h1: { fontSize: 20, fontWeight: 500, margin: 0 },
  h2: { fontSize: 12, fontWeight: 500, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: ".06em", margin: "0 0 10px" },
  h3: { margin: "0 0 10px", fontSize: 13, fontWeight: 500, display: "flex", justifyContent: "space-between" },
  sub: { color: "var(--havn-text-secondary)", fontSize: 13, marginTop: 2 },
  dim: { color: "var(--havn-text-secondary)", fontSize: 13 },
  ok: { color: "var(--havn-green)" },
  warn: { color: "var(--havn-yellow)" },
  bad: { color: "var(--havn-red)" },
  card: { background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)" },
  tiles: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: 12, marginBottom: 24 },
  tile: {
    textAlign: "left", padding: "14px 16px", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontFamily: "inherit",
  },
  tileLabel: { fontSize: 12, color: "var(--havn-text-secondary)" },
  tileValue: { fontSize: 24, fontWeight: 500, marginTop: 2, fontVariantNumeric: "tabular-nums" },
  tileUnit: { fontSize: 13, color: "var(--havn-text-secondary)", fontWeight: 400 },
  tileDetail: { fontSize: 12, marginTop: 4, color: "var(--havn-text-secondary)" },
  grid2: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 20, marginBottom: 24, alignItems: "start" },
  list: { listStyle: "none", margin: 0, padding: 0 },
  item: { display: "grid", gridTemplateColumns: "26px minmax(0, 1fr) auto", gap: 10, padding: "12px 14px", borderBottom: "1px solid var(--havn-border)", alignItems: "start" },
  sev: { width: 22, height: 22, borderRadius: 6, display: "grid", placeItems: "center", fontSize: 12, fontWeight: 600 },
  itemTitle: { fontWeight: 500, fontSize: 13.5, overflowWrap: "anywhere" },
  itemDetail: { fontSize: 12.5, color: "var(--havn-text-secondary)", marginTop: 2, overflowWrap: "anywhere" },
  acts: { display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" },
  empty: { display: "flex", gap: 12, alignItems: "center", padding: "16px 18px" },
  chartWrap: { position: "relative" },
  bars: { display: "flex", alignItems: "flex-end", gap: 2, height: 96, borderBottom: "1px solid var(--havn-border)" },
  barSlot: { flex: 1, height: "100%", display: "flex", flexDirection: "column", justifyContent: "flex-end", alignItems: "center", cursor: "default", minWidth: 0 },
  bar: { width: "100%", maxWidth: 28, borderRadius: "4px 4px 0 0", transition: "opacity .1s" },
  barMark: { fontSize: 10, color: "var(--havn-red)", lineHeight: 1, marginBottom: 2 },
  tooltip: {
    position: "absolute", bottom: "100%", transform: "translate(-50%, -6px)", pointerEvents: "none",
    background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)",
    padding: "6px 9px", fontSize: 12, whiteSpace: "nowrap", boxShadow: "0 4px 14px rgba(0,0,0,.25)", zIndex: 5,
  },
  axis: { display: "flex", justifyContent: "space-between", fontSize: 11, color: "var(--havn-text-dim)", marginTop: 6, fontFamily: "var(--havn-font-mono)" },
  runRow: { display: "flex", justifyContent: "space-between", gap: 12, fontSize: 12.5, padding: "8px 0", borderTop: "1px solid var(--havn-border)", marginTop: 10, color: "var(--havn-text-secondary)" },
  flow: { display: "grid", overflow: "hidden" },
  lane: { padding: 14, borderRight: "1px solid var(--havn-border)", minWidth: 0 },
  count: { color: "var(--havn-text-dim)", fontWeight: 400, fontFamily: "var(--havn-font-mono)", fontSize: 12 },
  chip: {
    display: "flex", alignItems: "center", gap: 7, width: "100%", padding: "5px 8px", marginBottom: 4,
    borderRadius: "var(--havn-radius)", border: "1px solid transparent", background: "var(--havn-bg)",
    color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)", fontSize: 12, textAlign: "left",
  },
  chipBad: { background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", borderColor: "color-mix(in srgb, var(--havn-red) 30%, transparent)" },
  chipName: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 },
  chipMeta: { marginLeft: "auto", fontStyle: "normal", color: "var(--havn-text-dim)", fontSize: 11, whiteSpace: "nowrap", paddingLeft: 6 },
  dot: { width: 7, height: 7, borderRadius: "50%", flexShrink: 0 },
  errorBox: { color: "var(--havn-red)", fontSize: 13 },
  link: { background: "none", border: "none", padding: 0, color: "var(--havn-accent)", cursor: "pointer", fontSize: 12.5, fontFamily: "inherit" },
  btn,
  btnPrimary: { ...btn, background: "var(--havn-accent)", borderColor: "var(--havn-accent)", color: "#fff", fontWeight: 500 },
  btnSm: { ...btn, padding: "3px 9px", fontSize: 12 },
  btnSmPrimary: { ...btn, padding: "3px 9px", fontSize: 12, background: "var(--havn-accent)", borderColor: "var(--havn-accent)", color: "#fff" },
};

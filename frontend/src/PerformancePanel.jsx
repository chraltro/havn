import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import ExplainPanel from "./ExplainPanel";

/**
 * Performance page (Observe): slowest models with their trend, regressions,
 * advice with dismiss / snooze, the critical path of a run, and a per-model
 * drilldown whose build history doubles as a plan timeline.
 *
 * Every number comes from `_havn.model_perf`, which each model build writes
 * (see havn.engine.perf). Plans are DuckDB's own profile of the build
 * statement, rendered with the same ExplainPanel as EXPLAIN ANALYZE.
 */

export function fmtMs(ms) {
  if (ms == null) return "";
  const v = Number(ms);
  if (v >= 60000) return `${(v / 60000).toFixed(1)} min`;
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s`;
  return `${Math.round(v)} ms`;
}

export function fmtRows(n) {
  if (n == null) return "";
  const v = Number(n);
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

function fmtBytes(n) {
  if (!n) return "";
  let v = Number(n);
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (v < 1024 || u === "TB") return u === "B" ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return "";
}

function fmtTime(iso) {
  if (!iso) return "";
  return String(iso).slice(0, 16).replace("T", " ");
}

const SEVERITY = {
  high: { color: "var(--havn-red)", label: "High" },
  medium: { color: "var(--havn-yellow)", label: "Medium" },
  low: { color: "var(--havn-text-secondary)", label: "Low" },
};

const RULE_LABELS = {
  incremental_candidate: "Make incremental",
  join_fanout: "Join fan-out",
  materialize_view: "Materialize view",
  unused_table: "Unused table",
  scan_small_slice: "Big scan, small slice",
  order_by_non_final: "ORDER BY upstream",
  distinct_large: "Large DISTINCT",
  python_udf_hot_path: "Python UDF hot path",
};

/** Single-series sparkline of build durations. Hover reads out the point. */
export function Sparkline({ points, width = 120, height = 28 }) {
  const [hover, setHover] = useState(null);
  if (!points || points.length < 2) return <span style={st.dim}>{points?.length === 1 ? "1 build" : ""}</span>;
  const vals = points.map((p) => Number(p.duration_ms || 0));
  const max = Math.max(...vals) || 1;
  const pad = 4;
  const x = (i) => pad + (i * (width - 2 * pad)) / (vals.length - 1);
  const y = (v) => height - pad - (v / max) * (height - 2 * pad);
  const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = vals.length - 1;
  const h = hover ?? last;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={`Build time trend, last ${fmtMs(vals[last])}`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const i = Math.round(((e.clientX - r.left - pad) / (width - 2 * pad)) * last);
          setHover(Math.max(0, Math.min(last, i)));
        }}
        style={{ display: "block", cursor: "crosshair" }}
      >
        <path d={d} fill="none" stroke="var(--havn-accent)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        {hover != null && <line x1={x(h)} x2={x(h)} y1={0} y2={height} stroke="var(--havn-border-light)" strokeWidth="1" />}
        <circle cx={x(h)} cy={y(vals[h])} r="3.5" fill="var(--havn-accent)" stroke="var(--havn-bg)" strokeWidth="2" />
      </svg>
      <span style={{ ...st.mono, minWidth: 48 }}>{fmtMs(vals[h])}</span>
    </span>
  );
}

/** Bars of a model's builds, oldest left. Click one to load its plan. */
export function BuildBars({ builds, selectedId, onSelect, height = 120 }) {
  const [hover, setHover] = useState(null);
  const ordered = useMemo(() => [...(builds || [])].reverse(), [builds]);
  if (ordered.length === 0) return null;
  const max = Math.max(...ordered.map((b) => Number(b.duration_ms || 0))) || 1;
  const n = ordered.length;
  const gap = 2;
  const width = Math.max(240, Math.min(720, n * 22));
  const bw = Math.max(4, (width - gap * (n - 1)) / n);
  const info = hover != null ? ordered[hover] : ordered.find((b) => b.id === selectedId) || ordered[n - 1];
  return (
    <div>
      <div style={{ ...st.dim, height: 18 }}>
        {info && (
          <>
            <span style={st.mono}>{fmtTime(info.finished_at)}</span>
            {"  "}{fmtMs(info.duration_ms)}
            {info.rows_out != null && `, ${fmtRows(info.rows_out)} rows`}
            {info.status !== "success" && `, ${info.status}`}
            {info.plan_captured ? ", plan captured" : ", no plan"}
          </>
        )}
      </div>
      <svg width={width} height={height} role="img" aria-label="Build durations" onMouseLeave={() => setHover(null)} style={{ display: "block" }}>
        <line x1={0} x2={width} y1={height - 0.5} y2={height - 0.5} stroke="var(--havn-border)" />
        {ordered.map((b, i) => {
          const h = Math.max(2, (Number(b.duration_ms || 0) / max) * (height - 8));
          const xPos = i * (bw + gap);
          const failed = b.status !== "success";
          const selected = b.id === selectedId;
          return (
            <g key={b.id}>
              <rect
                x={xPos}
                y={height - h}
                width={bw}
                height={h}
                rx={Math.min(4, bw / 2)}
                fill={failed ? "var(--havn-red)" : "var(--havn-accent)"}
                opacity={selected || hover === i ? 1 : b.plan_captured ? 0.75 : 0.4}
                stroke={selected ? "var(--havn-text)" : "none"}
                strokeWidth={selected ? 1.5 : 0}
              />
              <rect
                x={xPos - gap / 2}
                y={0}
                width={bw + gap}
                height={height}
                fill="transparent"
                style={{ cursor: b.plan_captured && b.status === "success" ? "pointer" : "default" }}
                onMouseEnter={() => setHover(i)}
                onClick={() => b.plan_captured && b.status === "success" && onSelect && onSelect(b.id)}
              >
                <title>{`${fmtTime(b.finished_at)}: ${fmtMs(b.duration_ms)}`}</title>
              </rect>
            </g>
          );
        })}
      </svg>
      <div style={{ ...st.dim, marginTop: 4 }}>Faded bars have no captured plan; click a solid one to see its plan.</div>
    </div>
  );
}

/** Gantt of one run: when each model built, the critical path in accent. */
function useWidth(ref, fallback) {
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width);
      if (w > 0) setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

export function RunGantt({ cp }) {
  const [hover, setHover] = useState(null);
  const boxRef = React.useRef(null);
  const boxW = useWidth(boxRef, 640);
  if (!cp || !cp.models || cp.models.length === 0) return <div ref={boxRef} />;
  const total = Math.max(cp.wall_ms || 1, 1);
  const rowH = 22;
  const labelW = 170;
  const valueW = 64;
  const fullW = Math.max(360, boxW);
  const chartW = fullW - labelW - valueW;
  const height = cp.models.length * rowH + 22;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * total);
  return (
    <div ref={boxRef}>
      <svg width={fullW} height={height} role="img" aria-label="Run timeline" style={{ display: "block" }}>
        {ticks.map((t, i) => {
          const xPos = labelW + (t / total) * chartW;
          return (
            <g key={i}>
              <line x1={xPos} x2={xPos} y1={0} y2={height - 18} stroke="var(--havn-border)" strokeDasharray="2 3" />
              <text x={xPos} y={height - 4} fontSize="10" fill="var(--havn-text-secondary)" textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"}>{fmtMs(t)}</text>
            </g>
          );
        })}
        {cp.models.map((m, i) => {
          const yPos = i * rowH + 3;
          const xPos = labelW + (m.start_offset_ms / total) * chartW;
          const w = Math.max(3, (m.duration_ms / total) * chartW);
          const active = hover === i;
          return (
            <g key={m.model} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={0} y={yPos - 2} width={fullW} height={rowH} fill={active ? "var(--havn-bg-secondary)" : "transparent"} />
              <text x={labelW - 8} y={yPos + 12} fontSize="11" textAnchor="end" fill={m.on_path ? "var(--havn-text)" : "var(--havn-text-secondary)"} fontWeight={m.on_path ? 600 : 400} style={{ fontFamily: "var(--havn-font-mono)" }}>
                {m.model.length > 24 ? m.model.slice(0, 23) + "…" : m.model}
              </text>
              <rect x={xPos} y={yPos + 2} width={w} height={rowH - 8} rx="3" fill={m.status !== "success" ? "var(--havn-red)" : m.on_path ? "var(--havn-accent)" : "var(--havn-text-dim)"} />
              <text x={Math.min(xPos + w + 6, fullW - valueW + 4)} y={yPos + 12} fontSize="10" fill="var(--havn-text-secondary)">{fmtMs(m.duration_ms)}</text>
              <title>{`${m.model}: started at +${fmtMs(m.start_offset_ms)}, built in ${fmtMs(m.duration_ms)}, tier ${m.tier}${m.on_path ? ", on the critical path" : ""}`}</title>
            </g>
          );
        })}
      </svg>
      <div style={{ display: "flex", gap: 14, marginTop: 6 }}>
        <Legend color="var(--havn-accent)" label="Critical path" />
        <Legend color="var(--havn-text-dim)" label="Other models" />
      </div>
    </div>
  );
}

function Legend({ color, label }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--havn-text-secondary)" }}>
      <span style={{ width: 10, height: 10, borderRadius: 2, background: color }} />
      {label}
    </span>
  );
}

function SeverityBadge({ severity }) {
  const s = SEVERITY[severity] || SEVERITY.low;
  return (
    <span style={{ ...st.badge, color: s.color, borderColor: s.color }}>
      <span style={{ width: 6, height: 6, borderRadius: 3, background: s.color, display: "inline-block" }} />
      {s.label}
    </span>
  );
}

export function AdviceCard({ item, onState, onOpenModel, busy }) {
  const [showEvidence, setShowEvidence] = useState(false);
  return (
    <div style={st.card} data-testid={`advice-${item.rule}-${item.model}`}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <SeverityBadge severity={item.severity} />
        <span style={st.ruleTag}>{RULE_LABELS[item.rule] || item.rule}</span>
        {onOpenModel ? (
          <button style={st.linkBtn} onClick={() => onOpenModel(item.model)}>{item.model}</button>
        ) : (
          <span style={st.mono}>{item.model}</span>
        )}
        {item.status !== "open" && (
          <span style={st.dim}>{item.status === "snoozed" ? `snoozed until ${fmtTime(item.snoozed_until)}` : "dismissed"}</span>
        )}
      </div>
      <div style={st.cardTitle}>{item.title}</div>
      <div style={st.cardText}>{item.explanation}</div>
      {item.suggestion && <pre style={st.suggestion}>{item.suggestion}</pre>}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
        <button style={st.ghostBtn} onClick={() => setShowEvidence(!showEvidence)} aria-expanded={showEvidence}>
          {showEvidence ? "Hide evidence" : "Evidence"}
        </button>
        <span style={{ flex: 1 }} />
        {item.status === "open" ? (
          <>
            <button style={st.ghostBtn} disabled={busy} onClick={() => onState(item, "snoozed", 7)}>Snooze 7 days</button>
            <button style={st.ghostBtn} disabled={busy} onClick={() => onState(item, "dismissed")}>Dismiss</button>
          </>
        ) : (
          <button style={st.ghostBtn} disabled={busy} onClick={() => onState(item, "open")}>Reopen</button>
        )}
      </div>
      {showEvidence && <pre style={st.evidence}>{JSON.stringify(item.evidence, null, 2)}</pre>}
    </div>
  );
}

function PlanDiff({ diff }) {
  if (!diff) return null;
  const ops = (diff.operators || []).filter((o) => Math.abs(o.delta_ms) >= 0.5).slice(0, 8);
  return (
    <div style={{ marginTop: 6 }}>
      {(diff.summary || []).map((s, i) => (
        <div key={i} style={st.cardText}>{s}</div>
      ))}
      {ops.length > 0 && (
        <table style={{ ...st.table, marginTop: 6 }}>
          <thead>
            <tr>
              <th style={st.th}>Operator</th>
              <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Fast build</th>
              <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Slow build</th>
              <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Change</th>
              <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Rows</th>
            </tr>
          </thead>
          <tbody>
            {ops.map((o, i) => (
              <tr key={i}>
                <td style={{ ...st.td, ...st.mono }}>{o.label}</td>
                <td style={{ ...st.td, textAlign: "right", whiteSpace: "nowrap" }}>{fmtMs(o.fast_ms)}</td>
                <td style={{ ...st.td, textAlign: "right", whiteSpace: "nowrap" }}>{fmtMs(o.slow_ms)}</td>
                <td style={{ ...st.td, textAlign: "right", color: o.delta_ms > 0 ? "var(--havn-red)" : "var(--havn-green)" }}>
                  {o.delta_ms > 0 ? "+" : ""}{fmtMs(o.delta_ms)}
                </td>
                <td style={{ ...st.td, textAlign: "right", whiteSpace: "nowrap" }}>
                  {o.fast_rows === o.slow_rows ? fmtRows(o.slow_rows) : `${fmtRows(o.fast_rows)} → ${fmtRows(o.slow_rows)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Regression({ reg, onOpenModel }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={st.regRow}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
        <span style={{ ...st.badge, color: "var(--havn-yellow)", borderColor: "var(--havn-yellow)" }}>{reg.ratio}x</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          {onOpenModel ? (
            <button style={st.linkBtn} onClick={() => onOpenModel(reg.model_path)}>{reg.model_path}</button>
          ) : null}
          <div style={st.cardText}>{reg.message}</div>
        </div>
        <span style={st.dim}>{fmtTime(reg.detected_at)}</span>
      </div>
      {reg.plan_diff && (
        <button style={{ ...st.ghostBtn, marginTop: 4 }} onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Hide plan diff" : "Plan diff"}
        </button>
      )}
      {open && <PlanDiff diff={reg.plan_diff} />}
    </div>
  );
}

function Section({ title, right, children }) {
  return (
    <section style={st.section}>
      <div style={st.sectionHead}>
        <h3 style={st.h3}>{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

function ModelDetail({ model, onBack, onState, busy }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [plan, setPlan] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const d = await api.getPerfModel(model);
      setData(d);
      setSelected(d.plan_build?.id || null);
      setPlan(d.plan || null);
    } catch (e) {
      setError(e.message || "Could not load model");
    }
  }, [model]);

  useEffect(() => { load(); }, [load]);

  const pick = async (id) => {
    setSelected(id);
    try {
      const b = await api.getPerfBuild(id);
      setPlan(b.plan || null);
    } catch (e) {
      setError(e.message || "Could not load plan");
    }
  };

  const stateChange = async (item, status, days) => {
    await onState(item, status, days);
    load();
  };

  if (error) return <div style={st.error}>{error}</div>;
  if (!data) return <div style={st.dim}>Loading...</div>;
  const ok = (data.history || []).filter((h) => h.status === "success");
  const durs = ok.map((h) => Number(h.duration_ms || 0)).sort((a, b) => a - b);
  const median = durs.length ? durs[Math.floor(durs.length / 2)] : null;
  const last = data.history?.[0];
  const selBuild = (data.history || []).find((h) => h.id === selected);

  return (
    <div style={st.col}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button style={st.ghostBtn} onClick={onBack}>&larr; All models</button>
        <h2 style={st.h2}>{model}</h2>
        {last && <span style={st.dim}>{last.materialized}</span>}
      </div>
      {data.history.length === 0 ? (
        <div style={st.empty}>No builds recorded for this model yet.</div>
      ) : (
        <>
          <div style={st.stats}>
            <Stat label="Median build" value={fmtMs(median)} />
            <Stat label="Last build" value={fmtMs(last?.duration_ms)} />
            <Stat label="Rows" value={fmtRows(last?.rows_out)} />
            <Stat label="Rows read" value={fmtRows(last?.rows_scanned ?? last?.rows_in)} />
            <Stat label="Peak memory" value={fmtBytes(last?.peak_memory_bytes) || "–"} />
            <Stat label="Spilled" value={fmtBytes(last?.spill_bytes) || "none"} />
          </div>
          <Section title={`Builds (${data.history.length})`}>
            <BuildBars builds={data.history} selectedId={selected} onSelect={pick} />
          </Section>
          <Section title={selBuild ? `Plan of the build at ${fmtTime(selBuild.finished_at)}` : "Plan"}>
            {plan ? (
              <div style={st.planBox}>
                <ExplainPanel plan={plan} raw={JSON.stringify(plan, null, 2)} isAnalyze />
              </div>
            ) : (
              <div style={st.empty}>No plan captured for this model yet (performance.capture_plans in project.yml).</div>
            )}
          </Section>
        </>
      )}
      {data.regressions.length > 0 && (
        <Section title="Regressions">
          {data.regressions.map((r) => <Regression key={r.id} reg={r} />)}
        </Section>
      )}
      {data.advice.length > 0 && (
        <Section title="Advice">
          {data.advice.map((a) => <AdviceCard key={a.key} item={a} onState={stateChange} busy={busy} />)}
        </Section>
      )}
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div style={st.stat}>
      <div style={st.statLabel}>{label}</div>
      <div style={st.statValue}>{value || "–"}</div>
    </div>
  );
}

export default function PerformancePanel() {
  const [days, setDays] = useState(7);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [model, setModel] = useState(null);
  const [showDismissed, setShowDismissed] = useState(false);
  const [advice, setAdvice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(null);
  const [cp, setCp] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await api.getPerfSummary(days);
      setSummary(s);
      setAdvice(null);
      if (s.runs?.length && !runId) setRunId(s.runs[0].pipeline_run_id);
    } catch (e) {
      setError(e.message || "Could not load performance data");
    } finally {
      setLoading(false);
    }
  }, [days]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    api.getPerfCriticalPath(runId).then((d) => { if (!cancelled) setCp(d); }).catch(() => { if (!cancelled) setCp(null); });
    return () => { cancelled = true; };
  }, [runId]);

  useEffect(() => {
    if (!showDismissed) { setAdvice(null); return; }
    api.getPerfAdvice(true).then(setAdvice).catch(() => setAdvice(null));
  }, [showDismissed]);

  const onState = useCallback(async (item, status, daysN = null) => {
    setBusy(true);
    try {
      await api.setPerfAdviceState(item.model, item.rule, status, daysN);
      const fresh = await api.getPerfAdvice(showDismissed);
      if (showDismissed) setAdvice(fresh);
      setSummary((s) => (s ? { ...s, advice: showDismissed ? fresh.filter((a) => a.status === "open") : fresh } : s));
    } catch (e) {
      setError(e.message || "Could not update advice");
    } finally {
      setBusy(false);
    }
  }, [showDismissed]);

  const adviceList = advice || summary?.advice || [];

  return (
    <div style={st.container}>
      <div style={st.header}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <label style={st.dim} htmlFor="perf-days">Window</label>
          <select id="perf-days" value={days} onChange={(e) => setDays(Number(e.target.value))} style={st.select}>
            <option value={1}>24 hours</option>
            <option value={7}>7 days</option>
            <option value={30}>30 days</option>
            <option value={90}>90 days</option>
          </select>
          <button style={st.ghostBtn} onClick={load} disabled={loading}>{loading ? "Loading..." : "Refresh"}</button>
        </div>
        {summary?.settings && (
          <span style={st.dim}>
            {summary.settings.enabled
              ? `Plans: ${summary.settings.capture_plans === "sampled" ? `sampled (${Math.round(summary.settings.sample_rate * 100)}%)` : summary.settings.capture_plans === "true" ? "every build" : "off"}`
              : "Performance capture is off (performance.enabled)"}
          </span>
        )}
      </div>
      {error && <div style={st.error}>{error}</div>}

      <div style={st.body}>
        {model ? (
          <ModelDetail model={model} onBack={() => { setModel(null); load(); }} onState={onState} busy={busy} />
        ) : !summary ? (
          <div style={st.dim}>{loading ? "Loading..." : ""}</div>
        ) : (
          <div style={st.grid}>
            <div style={st.col}>
              <Section title="Slowest models" right={<span style={st.dim}>median build time</span>}>
                {summary.slowest.length === 0 ? (
                  <div style={st.empty}>
                    No builds recorded in this window. Every <code>havn transform</code> run records each model's
                    build time, rows and plan here.
                  </div>
                ) : (
                  <table style={st.table}>
                    <thead>
                      <tr>
                        <th style={st.th}>Model</th>
                        <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Median</th>
                        <th style={st.th}>Trend</th>
                        <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Rows</th>
                        <th style={{ ...st.th, textAlign: "right", whiteSpace: "nowrap" }}>Peak mem</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.slowest.map((s) => (
                        <tr key={s.model_path} style={{ cursor: "pointer" }} onClick={() => setModel(s.model_path)}>
                          <td style={st.td}>
                            <button style={st.linkBtn} onClick={(e) => { e.stopPropagation(); setModel(s.model_path); }}>{s.model_path}</button>
                            <div style={st.dim}>{s.materialized} &middot; {s.builds} build{s.builds === 1 ? "" : "s"}</div>
                          </td>
                          <td style={{ ...st.td, textAlign: "right", fontWeight: 600, whiteSpace: "nowrap" }}>{fmtMs(s.median_ms)}</td>
                          <td style={st.td}>
                            {summary.trend?.[s.model_path] ? <Sparkline points={summary.trend[s.model_path]} /> : <span style={st.dim}>{fmtMs(s.last_ms)}</span>}
                          </td>
                          <td style={{ ...st.td, textAlign: "right", whiteSpace: "nowrap" }}>{fmtRows(s.last_rows)}</td>
                          <td style={{ ...st.td, textAlign: "right", whiteSpace: "nowrap" }}>{fmtBytes(s.peak_memory_bytes)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Section>

              <Section
                title="Critical path"
                right={summary.runs.length > 0 && (
                  <select aria-label="Run" value={runId || ""} onChange={(e) => setRunId(e.target.value)} style={st.select}>
                    {summary.runs.map((r) => (
                      <option key={r.pipeline_run_id} value={r.pipeline_run_id}>
                        {fmtTime(r.started_at)} &middot; {r.builds} models &middot; {fmtMs(r.wall_ms)}
                      </option>
                    ))}
                  </select>
                )}
              >
                {!cp ? (
                  <div style={st.empty}>No runs recorded yet.</div>
                ) : (
                  <>
                    <div style={st.cardText}>
                      The run took <b>{fmtMs(cp.wall_ms)}</b> for {fmtMs(cp.busy_ms)} of builds
                      {cp.parallelism > 1.05 ? `, ${cp.parallelism}x in parallel` : ""}. It waited on{" "}
                      <b>{cp.path.map((p) => p.model).join(" → ")}</b>
                      {cp.wait_ms ? `, including ${fmtMs(cp.wait_ms)} between steps` : ""}. No number of workers gets it under{" "}
                      <b>{fmtMs(cp.longest_chain_ms)}</b>, the longest dependency chain.
                    </div>
                    <div style={{ marginTop: 10 }}><RunGantt cp={cp} /></div>
                  </>
                )}
              </Section>
            </div>

            <div style={st.col}>
              <Section title="Regressions">
                {summary.regressions.length === 0 ? (
                  <div style={st.empty}>No model got slower than its own history in this window.</div>
                ) : (
                  summary.regressions.map((r) => <Regression key={r.id} reg={r} onOpenModel={setModel} />)
                )}
              </Section>

              <Section
                title={`Advice${adviceList.length ? ` (${adviceList.length})` : ""}`}
                right={
                  <label style={{ ...st.dim, display: "flex", gap: 6, alignItems: "center" }}>
                    <input type="checkbox" checked={showDismissed} onChange={(e) => setShowDismissed(e.target.checked)} />
                    Show dismissed
                  </label>
                }
              >
                {adviceList.length === 0 ? (
                  <div style={st.empty}>Nothing to suggest. Rules stay quiet below the thresholds in performance.advice.</div>
                ) : (
                  adviceList.map((a) => (
                    <AdviceCard key={a.key} item={a} onState={onState} onOpenModel={setModel} busy={busy} />
                  ))
                )}
              </Section>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", fontSize: 13, flexWrap: "wrap" },
  body: { flex: 1, overflow: "auto", padding: 16 },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 20, alignItems: "start" },
  col: { display: "flex", flexDirection: "column", gap: 20, minWidth: 0 },
  section: { marginBottom: 4, minWidth: 0 },
  sectionHead: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, marginBottom: 8 },
  h2: { margin: 0, fontSize: 16, fontWeight: 600, fontFamily: "var(--havn-font-mono)", color: "var(--havn-text)" },
  h3: { margin: 0, fontSize: 13, fontWeight: 600, color: "var(--havn-text)" },
  dim: { color: "var(--havn-text-secondary)", fontSize: 11 },
  mono: { fontFamily: "var(--havn-font-mono)", fontSize: 11, color: "var(--havn-text-secondary)" },
  select: { padding: "3px 6px", fontSize: 11, background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: 4, maxWidth: 320 },
  ghostBtn: { padding: "3px 10px", background: "var(--havn-btn-bg)", color: "var(--havn-text)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius)", cursor: "pointer", fontSize: 11 },
  linkBtn: { padding: 0, background: "none", border: "none", color: "var(--havn-accent)", cursor: "pointer", fontSize: 12, fontFamily: "var(--havn-font-mono)", textAlign: "left" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  th: { textAlign: "left", padding: "6px 10px", background: "var(--havn-bg-tertiary)", borderBottom: "1px solid var(--havn-border)", fontWeight: 600, fontSize: 11, color: "var(--havn-text-secondary)", whiteSpace: "nowrap" },
  td: { padding: "6px 10px", borderBottom: "1px solid var(--havn-border)", fontSize: 12, verticalAlign: "middle", color: "var(--havn-text)" },
  empty: { padding: 12, border: "1px dashed var(--havn-border)", borderRadius: "var(--havn-radius-lg)", fontSize: 12, color: "var(--havn-text-secondary)" },
  error: { padding: "8px 12px", background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)", fontSize: 12, borderBottom: "1px solid var(--havn-border)" },
  card: { padding: "10px 12px", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", background: "var(--havn-bg-secondary)", marginBottom: 10 },
  cardTitle: { fontSize: 13, fontWeight: 600, margin: "6px 0 4px", color: "var(--havn-text)" },
  cardText: { fontSize: 12, lineHeight: 1.5, color: "var(--havn-text)" },
  suggestion: { margin: "8px 0 0", padding: "6px 8px", background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", fontFamily: "var(--havn-font-mono)", fontSize: 11, whiteSpace: "pre-wrap", color: "var(--havn-text)" },
  evidence: { margin: "8px 0 0", padding: "6px 8px", background: "var(--havn-bg-tertiary)", borderRadius: "var(--havn-radius)", fontFamily: "var(--havn-font-mono)", fontSize: 10, whiteSpace: "pre-wrap", color: "var(--havn-text-secondary)", maxHeight: 220, overflow: "auto" },
  badge: { display: "inline-flex", alignItems: "center", gap: 5, padding: "1px 7px", border: "1px solid", borderRadius: 10, fontSize: 10, fontWeight: 600 },
  ruleTag: { fontSize: 11, color: "var(--havn-text-secondary)" },
  regRow: { padding: "8px 0", borderBottom: "1px solid var(--havn-border)" },
  stats: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 10 },
  stat: { padding: "8px 10px", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", background: "var(--havn-bg-secondary)" },
  statLabel: { fontSize: 10, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: "0.04em" },
  statValue: { fontSize: 16, fontWeight: 600, marginTop: 2, color: "var(--havn-text)" },
  planBox: { height: 420, border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", overflow: "hidden" },
};

import React, { useState, useEffect, useRef, useMemo } from "react";
import { api } from "./api";
import ChartPanel from "./ChartPanel";
import SortableTable from "./SortableTable";

/*
 * Ask: a question box over the semantic layer. The model only picks metrics,
 * dimensions, grain, filters and a range from metrics/*.yml; havn compiles
 * and runs the query. Every answer shows how it was produced.
 */

const EXAMPLES = ["Revenue by month this year", "Top 5 regions by revenue", "Now only Norway"];

function specSummary(spec) {
  if (!spec) return "";
  const parts = [(spec.metrics || []).join(", ")];
  if (spec.dimensions?.length) parts.push(`by ${spec.dimensions.join(", ")}`);
  if (spec.grain) parts.push(`per ${spec.grain}`);
  for (const f of spec.filters || []) {
    const v = Array.isArray(f.value) ? f.value.join(", ") : String(f.value);
    parts.push(`${f.dimension} ${f.op} ${v}`);
  }
  if (spec.start || spec.end) parts.push(`${spec.start || "…"} to ${spec.end || "…"}`);
  if (spec.limit) parts.push(`limit ${spec.limit}`);
  return parts.join(" · ");
}

/** Wide rows for a chart with a series dimension: one column per series value. */
export function pivotForChart(result, chart) {
  const { columns, rows } = result;
  const xi = columns.indexOf(chart.x);
  if (!chart.series || chart.y.length !== 1) {
    const yi = chart.y.map((y) => columns.indexOf(y));
    return { columns: [chart.x, ...chart.y], rows: rows.map((r) => [r[xi], ...yi.map((i) => r[i])]) };
  }
  const si = columns.indexOf(chart.series);
  const vi = columns.indexOf(chart.y[0]);
  const seriesValues = [...new Set(rows.map((r) => String(r[si])))];
  const byX = new Map();
  for (const r of rows) {
    const key = String(r[xi]);
    if (!byX.has(key)) byX.set(key, { x: r[xi], vals: {} });
    byX.get(key).vals[String(r[si])] = r[vi];
  }
  return {
    columns: [chart.x, ...seriesValues],
    rows: [...byX.values()].map((e) => [e.x, ...seriesValues.map((s) => e.vals[s] ?? null)]),
  };
}

function AnswerChart({ result, chart }) {
  const data = useMemo(
    () => (chart && (chart.type === "bar" || chart.type === "line") ? pivotForChart(result, chart) : null),
    [result, chart],
  );
  if (!chart || !result?.rows?.length) return null;
  if (chart.type === "number") {
    const i = result.columns.indexOf(chart.y[0]);
    const v = result.rows[0][i];
    return (
      <div style={st.kpi}>
        <div style={st.kpiValue}>{typeof v === "number" ? v.toLocaleString() : String(v ?? "–")}</div>
        <div style={st.dim}>{chart.y[0]}</div>
      </div>
    );
  }
  if (!data) return null;
  return (
    <div style={st.chartBox}>
      <ChartPanel columns={data.columns} rows={data.rows} forcedType={chart.type} compact />
    </div>
  );
}

function Provenance({ answer }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={st.prov}>
      <button style={st.linkBtn} onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "▾" : "▸"} How this was answered
      </button>
      {open && (
        <div style={st.provBody}>
          <div style={st.provLabel}>Query spec</div>
          <pre style={st.pre}>{JSON.stringify(answer.spec, null, 2)}</pre>
          <div style={st.provLabel}>Compiled SQL</div>
          <pre style={st.pre}>{answer.sql}</pre>
          <div style={st.provLabel}>Metrics</div>
          {(answer.metrics || []).map((m) => (
            <div key={m.name} style={st.mono}>
              {m.name} = {m.measure} on {m.model}
              {m.filters?.length ? ` where ${m.filters.join(" and ")}` : ""}
              <span style={st.dim}> ({m.source_path})</span>
            </div>
          ))}
          <div style={st.provLabel}>Lineage</div>
          {(answer.lineage || []).map((l) => (
            <div key={l.root} style={st.mono}>
              {l.nodes.map((n) => n.name).join(" ← ")}
              {l.sources?.length ? <span style={st.dim}>  sources: {l.sources.join(", ")}</span> : null}
            </div>
          ))}
          <div style={st.provLabel}>Freshness</div>
          {(answer.freshness || []).map((f) => (
            <div key={f.model} style={st.mono}>
              <span style={{ color: f.is_stale ? "var(--havn-yellow)" : f.never_built ? "var(--havn-red)" : "var(--havn-green)" }}>●</span>{" "}
              {f.model}: {f.never_built ? "never built" : `built ${f.last_run_at} (${f.hours_since_run}h ago)${f.is_stale ? ", stale" : ""}`}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Unanswerable({ answer, onAsk, onAccepted }) {
  const [saved, setSaved] = useState(null);
  const [error, setError] = useState(null);
  const sug = answer.suggested_metric;
  const accept = async () => {
    setError(null);
    try {
      const r = await api.acceptSuggestedMetric(sug.definition, sug.path);
      setSaved(r.path);
      onAccepted?.();
    } catch (e) {
      setError(e.message);
    }
  };
  return (
    <div>
      <div style={st.notice}>No defined metric answers this. {answer.explanation}</div>
      {answer.closest_metrics?.length > 0 && (
        <div style={st.row}>
          <span style={st.dim}>Closest metrics:</span>
          {answer.closest_metrics.map((c) => (
            <span key={c.name} style={st.chip} title={c.description}>{c.name}</span>
          ))}
        </div>
      )}
      {sug?.yaml && (
        <div style={st.suggestion}>
          <div style={st.provLabel}>A metric you could add ({sug.path})</div>
          <pre style={st.pre}>{sug.yaml}</pre>
          {saved ? (
            <div style={{ color: "var(--havn-green)" }}>Saved {saved}. Ask again to use it.</div>
          ) : (
            <button style={st.btn} onClick={accept}>Add to metrics/</button>
          )}
          {error && <div style={st.err}>{error}</div>}
        </div>
      )}
      {answer.exploratory_available && !answer.exploratory && (
        <button style={st.btnGhost} onClick={() => onAsk(answer.question, { exploratory: true })}>
          Try exploratory SQL (unverified)
        </button>
      )}
      {answer.exploratory && (
        <div style={st.exploratory}>
          <div style={st.unverified}>Unverified exploratory SQL — not from the semantic layer</div>
          <pre style={st.pre}>{answer.exploratory.sql}</pre>
          {answer.exploratory.error && <div style={st.err}>{answer.exploratory.error}</div>}
          {answer.exploratory.result && (
            <SortableTable columns={answer.exploratory.result.columns} rows={answer.exploratory.result.rows} />
          )}
        </div>
      )}
    </div>
  );
}

function Answer({ turn, onAsk, onAccepted }) {
  const a = turn.answer;
  if (!a) return <div style={st.dim}>Thinking…</div>;
  if (a.status === "error") return <div style={st.err}>{a.error || "Something went wrong."}</div>;
  return (
    <div style={st.answer}>
      {a.status === "answered" && (
        <>
          <div style={st.row}>
            <span style={st.verified}>Verified</span>
            <span style={st.specLine}>{specSummary(a.spec)}</span>
          </div>
          {a.explanation && <div style={st.explain}>{a.explanation}</div>}
          {a.summary && <div style={st.explain}>{a.summary}</div>}
          <AnswerChart result={a.result} chart={a.chart} />
          <div style={st.tableBox}>
            <SortableTable columns={a.result.columns} rows={a.result.rows} columnTypes={a.result.column_types} />
          </div>
          {a.result.truncated && <div style={st.dim}>Result capped at ai.max_rows.</div>}
          <Provenance answer={a} />
        </>
      )}
      {a.status === "clarify" && <div style={st.explain}>{a.clarification}</div>}
      {(a.status === "unanswerable" || a.status === "exploratory") && (
        <Unanswerable answer={a} onAsk={onAsk} onAccepted={onAccepted} />
      )}
      {(a.warnings || []).map((w, i) => (
        <div key={i} style={st.warn}>⚠ {w}</div>
      ))}
    </div>
  );
}

export default function AskPanel() {
  const [status, setStatus] = useState(null);
  const [turns, setTurns] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  const loadStatus = () => api.getAskStatus().then(setStatus).catch((e) => setStatus({ configured: false, error: e.message }));
  useEffect(() => { loadStatus(); }, []);
  useEffect(() => { endRef.current?.scrollIntoView?.({ behavior: "smooth" }); }, [turns]);

  const ask = async (question, options = {}) => {
    const q = (question ?? input).trim();
    if (!q || busy) return;
    const history = turns
      .filter((t) => t.answer && t.answer.status !== "error")
      .map((t) => ({ question: t.question, spec: t.answer.spec || null }))
      .slice(-10);
    const idx = turns.length;
    setTurns((prev) => [...prev, { question: q, answer: null }]);
    setInput("");
    setBusy(true);
    try {
      const answer = await api.ask(q, history, options);
      setTurns((prev) => prev.map((t, i) => (i === idx ? { ...t, answer } : t)));
    } catch (e) {
      setTurns((prev) => prev.map((t, i) => (i === idx ? { ...t, answer: { status: "error", error: e.message } } : t)));
    } finally {
      setBusy(false);
    }
  };

  const local = status?.is_local;
  return (
    <div style={st.container}>
      <div style={st.header}>
        <div>
          <div style={st.title}>Ask the warehouse</div>
          <div style={st.dim}>
            Answers come from the {status?.metrics ?? "…"} metrics in metrics/*.yml, never from free-form SQL.
          </div>
        </div>
        {turns.length > 0 && (
          <button style={st.btnGhost} onClick={() => setTurns([])}>New conversation</button>
        )}
      </div>

      {status && !status.configured && (
        <div style={st.notice}>
          Ask is not configured: {status.error} Set up the <code>ai:</code> section of project.yml (see the Ask wiki page).
        </div>
      )}

      <div style={st.thread}>
        {turns.length === 0 && (
          <div style={st.empty}>
            <div style={st.dim}>Try:</div>
            {EXAMPLES.map((e) => (
              <button key={e} style={st.chip} onClick={() => setInput(e)}>{e}</button>
            ))}
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} style={st.turn}>
            <div style={st.question}>{t.question}</div>
            <Answer turn={t} onAsk={ask} onAccepted={loadStatus} />
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <div style={st.inputRow}>
        <input
          aria-label="Question"
          style={st.input}
          value={input}
          placeholder={turns.length ? "Refine: “now by month”, “only Norway”…" : "Ask a question about your metrics"}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") ask(); }}
          disabled={busy || (status && !status.configured)}
        />
        <button style={st.btn} onClick={() => ask()} disabled={busy || !input.trim()}>
          {busy ? "Asking…" : "Ask"}
        </button>
      </div>
      {status?.configured && (
        <div style={st.privacy}>
          {status.provider}:{status.model} {local ? "(on this machine)" : `at ${status.base_url}`} receives catalog metadata only
          {status.share_dimension_values ? " plus distinct dimension values" : ""}
          {status.summarize_results ? "; result rows only when you ask for a summary" : ", never row data"}.
        </div>
      )}
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", padding: "16px 20px", gap: 12, overflow: "hidden", boxSizing: "border-box" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 },
  title: { fontSize: 16, fontWeight: 600, color: "var(--havn-text)" },
  dim: { color: "var(--havn-text-secondary)", fontSize: 12 },
  thread: { flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: 18, paddingRight: 4 },
  empty: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 8 },
  turn: { display: "flex", flexDirection: "column", gap: 8 },
  question: { alignSelf: "flex-start", background: "var(--havn-bg-tertiary, var(--havn-bg-secondary))", color: "var(--havn-text)", padding: "6px 10px", borderRadius: "var(--havn-radius, 6px)", fontSize: 13, fontWeight: 500 },
  answer: { display: "flex", flexDirection: "column", gap: 8, borderLeft: "2px solid var(--havn-border)", paddingLeft: 12 },
  row: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
  verified: { fontSize: 10, fontWeight: 700, letterSpacing: 0.5, textTransform: "uppercase", color: "var(--havn-green)", border: "1px solid var(--havn-green)", borderRadius: 4, padding: "1px 6px" },
  unverified: { fontSize: 11, fontWeight: 700, color: "var(--havn-yellow)", marginBottom: 4 },
  specLine: { fontFamily: "var(--havn-font-mono)", fontSize: 12, color: "var(--havn-text-secondary)" },
  explain: { fontSize: 13, color: "var(--havn-text)" },
  chartBox: { height: 260, border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius, 6px)", overflow: "hidden" },
  tableBox: { maxHeight: 280, overflow: "auto", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius, 6px)" },
  kpi: { padding: "10px 0" },
  kpiValue: { fontSize: 32, fontWeight: 600, color: "var(--havn-text)", fontVariantNumeric: "tabular-nums" },
  prov: { fontSize: 12 },
  provBody: { display: "flex", flexDirection: "column", gap: 4, marginTop: 6 },
  provLabel: { fontSize: 11, fontWeight: 600, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: 0.4, marginTop: 6 },
  pre: { margin: 0, padding: 8, background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: 4, fontFamily: "var(--havn-font-mono)", fontSize: 12, color: "var(--havn-text)", whiteSpace: "pre-wrap", overflowX: "auto" },
  mono: { fontFamily: "var(--havn-font-mono)", fontSize: 12, color: "var(--havn-text)" },
  notice: { fontSize: 13, color: "var(--havn-text)", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius, 6px)", padding: "8px 10px" },
  suggestion: { display: "flex", flexDirection: "column", gap: 6, marginTop: 6 },
  exploratory: { border: "1px dashed var(--havn-yellow)", borderRadius: "var(--havn-radius, 6px)", padding: 8, marginTop: 6 },
  chip: { fontSize: 12, padding: "3px 8px", borderRadius: 12, border: "1px solid var(--havn-border)", background: "transparent", color: "var(--havn-text)", cursor: "pointer" },
  warn: { fontSize: 12, color: "var(--havn-yellow)" },
  err: { fontSize: 13, color: "var(--havn-red)" },
  inputRow: { display: "flex", gap: 8 },
  input: { flex: 1, padding: "8px 10px", fontSize: 14, background: "var(--havn-bg-secondary)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius, 6px)", outline: "none" },
  btn: { padding: "6px 14px", fontSize: 13, background: "var(--havn-accent)", color: "var(--havn-bg)", border: "none", borderRadius: "var(--havn-radius, 6px)", cursor: "pointer", alignSelf: "flex-start" },
  btnGhost: { padding: "4px 10px", fontSize: 12, background: "transparent", color: "var(--havn-text-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius, 6px)", cursor: "pointer", alignSelf: "flex-start" },
  linkBtn: { background: "none", border: "none", padding: 0, color: "var(--havn-accent)", cursor: "pointer", fontSize: 12 },
  privacy: { fontSize: 11, color: "var(--havn-text-secondary)" },
};

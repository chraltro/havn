import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";

/*
 * Observe > Live: which models refresh continuously, how far behind each one
 * is, and the controls to pause, resume or retry them. Status comes from
 * GET /api/live/status; the runner's SSE stream (refreshes, advances, state
 * changes) drives an activity feed and prompts a refetch, with a slow poll
 * underneath in case the stream drops.
 */

export const STATUS_META = {
  live: { color: "var(--havn-green)", label: "live" },
  behind: { color: "var(--havn-yellow)", label: "behind" },
  waiting: { color: "var(--havn-accent)", label: "waiting" },
  failing: { color: "var(--havn-red)", label: "failing" },
  paused: { color: "var(--havn-purple)", label: "paused" },
};

export function fmtLag(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds <= 0) return "0s";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 120) return `${seconds.toFixed(1)}s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function fmtAgo(iso, nowMs) {
  if (!iso) return "never";
  const t = Date.parse(iso.endsWith("Z") ? iso : `${iso}Z`);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (nowMs - t) / 1000);
  if (s < 2) return "just now";
  return `${fmtLag(s)} ago`;
}

function secondsUntil(iso, nowMs) {
  if (!iso) return null;
  const t = Date.parse(iso.endsWith("Z") ? iso : `${iso}Z`);
  return Number.isNaN(t) ? null : Math.max(0, (t - nowMs) / 1000);
}

function kindLabel(m) {
  const parts = [m.materialized];
  if (m.strategy) parts.push(m.strategy);
  if (m.cdc) parts.push("cdc");
  if (m.live_interval) parts.push(`every ${fmtLag(m.live_interval)}`);
  return parts.join(" · ");
}

function describeEvent(evt) {
  const d = evt.data || {};
  switch (evt.type) {
    case "advance":
      return { tone: "dim", text: `${d.source} +${d.rows} row${d.rows === 1 ? "" : "s"} (watermark ${d.watermark})` };
    case "refresh":
      if (d.status === "built") {
        const lag = d.lag_ms !== null && d.lag_ms !== undefined ? ` · lag ${fmtLag(d.lag_ms / 1000)}` : "";
        return { tone: "ok", text: `${d.model} refreshed · ${d.events} event${d.events === 1 ? "" : "s"} · ${d.duration_ms}ms${lag}` };
      }
      return { tone: "bad", text: `${d.model} ${d.status === "assertion_failed" ? "failed a check" : "failed"}: ${d.error || ""}` };
    case "state":
      if (d.status === "failing") return { tone: "bad", text: `${d.model} paused by failure, retry in ${fmtLag(d.retry_in_s)}` };
      return { tone: "info", text: `${d.model} ${d.status === "paused" ? "paused" : "active"}` };
    case "runner":
      return { tone: "info", text: d.running ? "runner started" : "runner stopped" };
    default:
      return null;
  }
}

function StatusCell({ m, nowMs }) {
  const meta = STATUS_META[m.status] || STATUS_META.live;
  let sub = null;
  if (m.status === "failing") {
    const s = secondsUntil(m.next_retry_at, nowMs);
    sub = s === null ? `${m.consecutive_failures || 1} failure(s)` : `retry in ${fmtLag(s)}`;
  } else if (m.status === "waiting" && m.waiting_on) {
    sub = `on ${m.waiting_on}`;
  } else if (m.refreshing) {
    sub = "refreshing…";
  }
  return (
    <div>
      <span style={st.statusWord}>
        <span style={{ ...st.dot, background: meta.color, boxShadow: m.status === "live" ? `0 0 0 3px color-mix(in srgb, ${meta.color} 25%, transparent)` : "none" }} aria-hidden />
        <span style={{ color: meta.color }}>{meta.label}</span>
      </span>
      {sub && <div style={st.sub}>{sub}</div>}
    </div>
  );
}

function LagCell({ m, maxLag }) {
  const pct = maxLag > 0 ? Math.min(100, (m.lag_seconds / maxLag) * 100) : 0;
  const color = m.lag_seconds <= 0 ? "var(--havn-green)" : pct >= 100 ? "var(--havn-red)" : "var(--havn-yellow)";
  return (
    <div style={st.lagCell} title={`Behind by ${fmtLag(m.lag_seconds)}; stale above ${fmtLag(maxLag)}`}>
      <span style={st.mono}>{fmtLag(m.lag_seconds)}</span>
      <span style={st.lagTrack} aria-hidden>
        <span style={{ ...st.lagFill, width: `${Math.max(pct, m.lag_seconds > 0 ? 4 : 0)}%`, background: color }} />
      </span>
    </div>
  );
}

function EmptyState({ runner }) {
  return (
    <div style={st.empty}>
      <div style={st.emptyTitle}>No live models yet</div>
      <p style={st.emptyText}>
        A live model refreshes within seconds of new data landing from a webhook, CDC or API-poll
        source, instead of waiting for the next transform run. Opt a model in from its header:
      </p>
      <pre style={st.snippet}>{`@config materialized=incremental, live=true, incremental_strategy=merge,
        unique_key=order_id, incremental_filter=WHERE _havn_seq > {watermark}

SELECT order_id, amount, _havn_seq FROM landing.orders`}</pre>
      <p style={st.emptyText}>
        Views are always live. See the <b>Live models</b> wiki page for CDC deletes, lag and the
        settings under <code>live:</code> in project.yml.
        {!runner?.running && " The runner starts on its own once the project has a live model."}
      </p>
    </div>
  );
}

export default function LivePanel() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [feed, setFeed] = useState([]);
  const [busy, setBusy] = useState({});
  const [nowMs, setNowMs] = useState(() => Date.now());
  const refetchTimer = useRef(null);

  const load = useCallback(() => {
    api.getLiveStatus()
      .then((s) => { setStatus(s); setError(null); })
      .catch((e) => setError(e.message || "Failed to load live status"));
  }, []);

  const scheduleLoad = useCallback(() => {
    if (refetchTimer.current) return;
    refetchTimer.current = setTimeout(() => {
      refetchTimer.current = null;
      load();
    }, 300);
  }, [load]);

  useEffect(() => {
    load();
    const poll = setInterval(load, 5000);
    const tick = setInterval(() => setNowMs(Date.now()), 1000);
    const stop = api.streamLiveEvents((type, data, id) => {
      if (type === "hello") return;
      // Newest first by the runner's event id; a reconnect that replays
      // events already shown does not show them twice.
      setFeed((prev) => {
        if (id !== undefined && prev.some((e) => e.key === id)) return prev;
        const next = [{ type, data, at: data?.ts ? data.ts * 1000 : Date.now(), key: id ?? -Date.now() }, ...prev];
        next.sort((a, b) => b.key - a.key);
        return next.slice(0, 60);
      });
      // Coalesced: a burst of events costs one status read.
      scheduleLoad();
    });
    return () => {
      clearInterval(poll);
      clearInterval(tick);
      stop();
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
    };
  }, [load, scheduleLoad]);

  const act = useCallback(async (key, fn) => {
    setBusy((b) => ({ ...b, [key]: true }));
    try {
      await fn();
      load();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  }, [load]);

  const models = status?.models || [];
  const runner = status?.runner || { running: false };
  const maxLag = Number(status?.settings?.max_lag) || 300;
  const counts = useMemo(() => {
    const c = { live: 0, behind: 0, waiting: 0, failing: 0, paused: 0 };
    for (const m of models) c[m.status] = (c[m.status] || 0) + 1;
    return c;
  }, [models]);

  return (
    <div style={st.container}>
      <div style={st.header}>
        <div style={st.titleRow}>
          <span style={st.title}>Live models</span>
          <span style={{ ...st.pill, ...(runner.running ? st.pillOn : st.pillOff) }}>
            <span style={{ ...st.dot, background: runner.running ? "var(--havn-green)" : "var(--havn-text-dim)" }} aria-hidden />
            {runner.running ? "runner up" : "runner stopped"}
          </span>
          {runner.running && (
            <span style={st.dim}>
              {runner.refreshes || 0} refreshes · {runner.cycles || 0} cycles
              {runner.last_cycle_at ? ` · last ${fmtAgo(runner.last_cycle_at, nowMs)}` : ""}
            </span>
          )}
        </div>
        <div style={st.titleRow}>
          {models.length > 0 && (
            <span style={st.counts}>
              {Object.entries(counts).filter(([, n]) => n > 0).map(([k, n]) => (
                <span key={k} style={st.count}>
                  <span style={{ ...st.dot, background: STATUS_META[k].color }} aria-hidden />
                  {n} {STATUS_META[k].label}
                </span>
              ))}
            </span>
          )}
          <button
            style={runner.running ? st.btnGhost : st.btn}
            disabled={busy.runner}
            onClick={() => act("runner", runner.running ? api.stopLiveRunner : api.startLiveRunner)}
          >
            {runner.running ? "Stop runner" : "Start runner"}
          </button>
        </div>
      </div>

      {error && <div style={st.error}>{error}</div>}
      {runner.discovery_error && (
        <div style={st.error}>Model discovery failed; the runner keeps its last good model set: {runner.discovery_error}</div>
      )}

      <div style={st.body}>
        {!status ? (
          <div style={st.dim}>Loading…</div>
        ) : models.length === 0 ? (
          <EmptyState runner={runner} />
        ) : (
          <>
            <div style={st.card}>
              <table style={st.table}>
                <thead>
                  <tr>
                    <th style={st.th}>Model</th>
                    <th style={st.th}>Status</th>
                    <th style={st.th}>Lag</th>
                    <th style={{ ...st.th, ...st.num }}>Events/s</th>
                    <th style={st.th}>Last refresh</th>
                    <th style={{ ...st.th, ...st.num }}>Refreshes</th>
                    <th style={{ ...st.th, textAlign: "right" }} aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <React.Fragment key={m.model}>
                      <tr style={m.status === "failing" ? st.rowFailing : undefined}>
                        <td style={st.td}>
                          <div style={st.modelName}>{m.model}</div>
                          <div style={st.sub}>
                            {kindLabel(m)}
                            {m.inputs?.length > 0 && ` ← ${m.inputs.map((i) => i.source).join(", ")}`}
                          </div>
                        </td>
                        <td style={st.td}><StatusCell m={m} nowMs={nowMs} /></td>
                        <td style={st.td}>{m.materialized === "view" ? <span style={st.dim}>reads live</span> : <LagCell m={m} maxLag={maxLag} />}</td>
                        <td style={{ ...st.td, ...st.num, ...st.mono }}>{m.materialized === "view" ? "–" : (m.events_per_second || 0).toLocaleString()}</td>
                        <td style={st.td}>
                          {m.materialized === "view" ? <span style={st.dim}>–</span> : (
                            <>
                              <div>{fmtAgo(m.last_refresh_at, nowMs)}</div>
                              {m.last_refresh_at && (
                                <div style={st.sub}>
                                  {m.last_duration_ms}ms{m.last_lag_ms !== null && m.last_lag_ms !== undefined ? ` · lag ${fmtLag(m.last_lag_ms / 1000)}` : ""}
                                </div>
                              )}
                            </>
                          )}
                        </td>
                        <td style={{ ...st.td, ...st.num, ...st.mono }}>{m.materialized === "view" ? "–" : (m.refreshes || 0).toLocaleString()}</td>
                        <td style={{ ...st.td, textAlign: "right", whiteSpace: "nowrap" }}>
                          {m.materialized !== "view" && (
                            <>
                              {m.status === "failing" && runner.running && (
                                <button style={st.btnSmall} disabled={busy[`r:${m.model}`]}
                                        onClick={() => act(`r:${m.model}`, () => api.refreshLiveModel(m.model))}>
                                  Retry now
                                </button>
                              )}
                              {m.paused ? (
                                <button style={st.btnSmall} disabled={busy[m.model]}
                                        onClick={() => act(m.model, () => api.resumeLiveModel(m.model))}>
                                  Resume
                                </button>
                              ) : (
                                <button style={st.btnSmallGhost} disabled={busy[m.model]}
                                        onClick={() => act(m.model, () => api.pauseLiveModel(m.model))}>
                                  Pause
                                </button>
                              )}
                            </>
                          )}
                        </td>
                      </tr>
                      {m.status === "failing" && m.last_error && (
                        <tr style={st.rowFailing}>
                          <td colSpan={7} style={{ ...st.td, paddingTop: 0 }}>
                            <div style={st.errorLine}>{m.last_error}</div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  ))}
                </tbody>
              </table>
            </div>

            <div style={st.grid}>
              <div style={st.card}>
                <div style={st.cardTitle}>Sources</div>
                {(status.sources || []).length === 0 ? (
                  <div style={{ ...st.dim, padding: "8px 12px" }}>
                    No source has advanced yet. Webhook, CDC and API-poll ingest advance their landing
                    tables on every commit; other loads call <code>advance_source</code>.
                  </div>
                ) : (
                  <table style={st.table}>
                    <thead>
                      <tr>
                        <th style={st.th}>Source</th>
                        <th style={{ ...st.th, ...st.num }}>Watermark</th>
                        <th style={{ ...st.th, ...st.num }}>Events/s</th>
                        <th style={st.th}>Last advance</th>
                      </tr>
                    </thead>
                    <tbody>
                      {status.sources.map((s) => (
                        <tr key={s.source}>
                          <td style={st.td}>
                            <div style={st.modelName}>{s.source}</div>
                            <div style={st.sub}>{s.kind === "model" ? `live model · ${s.rows_total.toLocaleString()} events applied` : `landing · ${s.rows_total.toLocaleString()} rows`}</div>
                          </td>
                          <td style={{ ...st.td, ...st.num, ...st.mono }}>{s.watermark.toLocaleString()}</td>
                          <td style={{ ...st.td, ...st.num, ...st.mono }}>{s.events_per_second.toLocaleString()}</td>
                          <td style={st.td}>{fmtAgo(s.advanced_at, nowMs)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              <div style={st.card}>
                <div style={st.cardTitle}>Activity</div>
                {feed.length === 0 ? (
                  <div style={{ ...st.dim, padding: "8px 12px" }}>Waiting for the next advance or refresh…</div>
                ) : (
                  <ul style={st.feed}>
                    {feed.map((evt) => {
                      const d = describeEvent(evt);
                      if (!d) return null;
                      const color = d.tone === "ok" ? "var(--havn-green)" : d.tone === "bad" ? "var(--havn-red)" : d.tone === "info" ? "var(--havn-accent)" : "var(--havn-text-dim)";
                      return (
                        <li key={evt.key} style={st.feedItem}>
                          <span style={st.feedTime}>{new Date(evt.at).toLocaleTimeString()}</span>
                          <span style={{ ...st.feedMark, background: color }} aria-hidden />
                          <span style={{ color: d.tone === "bad" ? "var(--havn-red)" : "var(--havn-text)" }}>{d.text}</span>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

const st = {
  container: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", fontSize: 13, flexWrap: "wrap" },
  titleRow: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" },
  title: { fontWeight: 600, fontSize: 13 },
  pill: { display: "inline-flex", alignItems: "center", gap: 6, padding: "2px 8px", borderRadius: 999, fontSize: 11, border: "1px solid var(--havn-border)" },
  pillOn: { color: "var(--havn-green)", borderColor: "color-mix(in srgb, var(--havn-green) 45%, transparent)", background: "color-mix(in srgb, var(--havn-green) 10%, transparent)" },
  pillOff: { color: "var(--havn-text-dim)" },
  counts: { display: "inline-flex", gap: 10, fontSize: 11, color: "var(--havn-text-secondary)" },
  count: { display: "inline-flex", alignItems: "center", gap: 5 },
  dot: { display: "inline-block", width: 7, height: 7, borderRadius: "50%", flex: "none" },
  btn: { padding: "4px 12px", background: "var(--havn-green)", color: "#fff", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11, fontWeight: 500 },
  btnGhost: { padding: "4px 12px", background: "transparent", color: "var(--havn-text-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11 },
  btnSmall: { padding: "2px 10px", marginLeft: 6, background: "var(--havn-btn-bg)", color: "var(--havn-text)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius)", cursor: "pointer", fontSize: 11 },
  btnSmallGhost: { padding: "2px 10px", marginLeft: 6, background: "transparent", color: "var(--havn-text-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", cursor: "pointer", fontSize: 11 },
  body: { flex: 1, overflow: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 12 },
  card: { border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", background: "var(--havn-bg-secondary)", overflowX: "auto" },
  cardTitle: { padding: "8px 12px", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em", color: "var(--havn-text-secondary)", borderBottom: "1px solid var(--havn-border)" },
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12, alignItems: "start" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  th: { textAlign: "left", padding: "6px 12px", background: "var(--havn-bg-tertiary)", borderBottom: "1px solid var(--havn-border)", fontWeight: 600, fontSize: 11, whiteSpace: "nowrap" },
  td: { padding: "7px 12px", borderBottom: "1px solid var(--havn-border-light)", fontSize: 12, verticalAlign: "top" },
  num: { textAlign: "right" },
  mono: { fontFamily: "var(--havn-font-mono)", fontVariantNumeric: "tabular-nums" },
  modelName: { fontFamily: "var(--havn-font-mono)", fontWeight: 600, fontSize: 12 },
  sub: { color: "var(--havn-text-dim)", fontSize: 11, marginTop: 2 },
  statusWord: { display: "inline-flex", alignItems: "center", gap: 6, fontWeight: 500 },
  lagCell: { display: "flex", flexDirection: "column", gap: 4, minWidth: 70 },
  lagTrack: { display: "block", width: 64, height: 3, borderRadius: 2, background: "var(--havn-border)", overflow: "hidden" },
  lagFill: { display: "block", height: "100%", borderRadius: 2 },
  rowFailing: { background: "color-mix(in srgb, var(--havn-red) 6%, transparent)" },
  errorLine: { fontFamily: "var(--havn-font-mono)", fontSize: 11, color: "var(--havn-red)", whiteSpace: "pre-wrap", wordBreak: "break-word" },
  dim: { color: "var(--havn-text-dim)", fontSize: 12 },
  error: { padding: "8px 12px", background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)", fontSize: 12, borderBottom: "1px solid var(--havn-border)" },
  feed: { listStyle: "none", margin: 0, padding: "4px 0", maxHeight: 320, overflowY: "auto" },
  feedItem: { display: "flex", alignItems: "baseline", gap: 8, padding: "3px 12px", fontSize: 12 },
  feedTime: { color: "var(--havn-text-dim)", fontFamily: "var(--havn-font-mono)", fontSize: 10, flex: "none" },
  feedMark: { display: "inline-block", width: 6, height: 6, borderRadius: 1, flex: "none", transform: "translateY(-1px)" },
  empty: { maxWidth: 680, padding: 16, border: "1px dashed var(--havn-border)", borderRadius: "var(--havn-radius-lg)" },
  emptyTitle: { fontWeight: 600, fontSize: 14, marginBottom: 6 },
  emptyText: { fontSize: 12, color: "var(--havn-text-secondary)", lineHeight: 1.6, margin: "6px 0" },
  snippet: { fontFamily: "var(--havn-font-mono)", fontSize: 11, background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", padding: 10, overflowX: "auto", margin: "8px 0" },
};

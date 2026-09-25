import React, { useState, useEffect, useCallback, useRef } from "react";
import { api } from "./api";
import { useAuth } from "./AuthContext";
import { timeAgo } from "./HomePanel";

/*
 * Ship: should this change go in? A change is a havn PR (a git branch with a
 * build, data diff and reviews). The page shows what the change touches, how
 * the data moves, and a merge gate that mirrors exactly what merge enforces.
 */

const POLL_MS = 2000;

/** Column for each node: upstream 0, changed from 1, downstream after its parents. */
export function layoutColumns(nodes, edges) {
  const role = Object.fromEntries(nodes.map((n) => [n.name, n.role]));
  const depth = Object.fromEntries(nodes.map((n) => [n.name, n.role === "upstream" ? 0 : 1]));
  // Longest-path relaxation; the graph is a DAG, so |nodes| passes suffice.
  for (let pass = 0; pass < nodes.length; pass++) {
    let moved = false;
    for (const [from, to] of edges) {
      if (role[to] === "upstream" || !(from in depth) || !(to in depth)) continue;
      if (depth[to] < depth[from] + 1) { depth[to] = depth[from] + 1; moved = true; }
    }
    if (!moved) break;
  }
  const cols = [];
  for (const n of nodes) (cols[depth[n.name]] ||= []).push(n);
  return cols.filter(Boolean);
}

export default function ShipPanel({ running, showConfirm, addOutput, onOpenFile, onNavigate, onRunPipeline, onMerged }) {
  const auth = useAuth();
  const user = auth?.currentUser?.username || "local";
  const [prs, setPrs] = useState(null);
  const [listError, setListError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [review, setReview] = useState(null);
  const [reviewError, setReviewError] = useState(null);
  const [busy, setBusy] = useState(null); // "build" | "approve" | "changes" | "merge"
  const [merged, setMerged] = useState(null);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const loadList = useCallback(async () => {
    try {
      const list = await api.listPrs();
      setPrs(list);
      setListError(null);
      setSelected((cur) => cur || list.find((p) => p.status === "open")?.id || null);
    } catch (e) {
      setListError(e.message);
    }
  }, []);

  const loadReview = useCallback(async (id) => {
    if (!id) return;
    try {
      const r = await api.getPrReview(id);
      if (selectedRef.current !== id) return;
      setReview(r);
      setReviewError(null);
    } catch (e) {
      if (selectedRef.current !== id) return;
      setReview(null);
      setReviewError(e.message);
    }
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => {
    setReview(null);
    setReviewError(null);
    setMerged(null);
    setReasonOpen(false);
    loadReview(selected);
  }, [selected, loadReview]);

  // Poll while a build is running.
  const buildRunning = review?.build?.status === "running" || busy === "build";
  useEffect(() => {
    if (!buildRunning || !selected) return;
    const t = setInterval(() => loadReview(selected), POLL_MS);
    return () => clearInterval(t);
  }, [buildRunning, selected, loadReview]);
  // The build starts on a background thread, so the first poll can still
  // return the previous record; stay busy until a different one has finished.
  const buildBeforeRef = useRef(null);
  useEffect(() => {
    const b = review?.build;
    if (busy === "build" && b && b.status !== "running" && b.started_at !== buildBeforeRef.current) setBusy(null);
  }, [busy, review]);

  async function act(kind, fn, done) {
    setBusy(kind);
    try {
      await fn();
      if (done) done();
      await Promise.all([loadReview(selected), loadList()]);
    } catch (e) {
      addOutput?.("error", e.message);
      setReviewError(e.message);
      setBusy(null);
      return;
    }
    if (kind !== "build") setBusy(null);
  }

  async function waiveApproval() {
    const ok = await showConfirm(
      "Merge without review",
      "Turn off the approval requirement for this change? It can then merge without anyone else reviewing it. With sign-in on, only an admin can do this.",
      "Turn off approval",
      true,
    );
    if (!ok) return;
    await act("approve", () => api.updatePr(review.pr.id, { require_approval: false }));
  }

  async function merge() {
    if (!review) return;
    const title = "Merge change";
    const msg = review.build_current
      ? `Merge "${review.pr.title}" into ${review.pr.base_ref}?`
      : `The latest commit on ${review.pr.head_ref} has no passing build, so its checks have not run. Merge "${review.pr.title}" into ${review.pr.base_ref} anyway?`;
    const ok = await showConfirm(title, msg, review.build_current ? "Merge" : "Merge anyway", !review.build_current);
    if (!ok) return;
    setBusy("merge");
    try {
      const res = await api.mergePr(review.pr.id, user);
      setMerged(res);
      addOutput?.("info", `Merged ${review.pr.head_ref} into ${review.pr.base_ref} (${(res.merge_commit || "").slice(0, 7)})`);
      onMerged?.();
      await Promise.all([loadReview(review.pr.id), loadList()]);
    } catch (e) {
      setReviewError(e.message);
    } finally {
      setBusy(null);
    }
  }

  const isAuthor = !!review && user.toLowerCase() === (review.pr.author || "").toLowerCase();
  const open = (prs || []).filter((p) => p.status === "open");
  const done = (prs || []).filter((p) => p.status !== "open").slice(0, 8);

  return (
    <div style={s.layout} className="havn-ship">
      <aside style={s.list} aria-label="Changes">
        <div style={s.listHead}>
          <h2 style={s.h2}>Changes</h2>
          <button style={s.link} onClick={() => onNavigate("Git:Reviews")}>+ New</button>
        </div>
        {listError && <div style={s.err}>{listError}</div>}
        {prs && open.length === 0 && (
          <div style={s.dim}>No open changes. Create one from a branch in <button style={s.link} onClick={() => onNavigate("Git:Reviews")}>Git → Reviews</button>.</div>
        )}
        {open.map((p) => <ListItem key={p.id} pr={p} selected={selected === p.id} onClick={() => setSelected(p.id)} />)}
        {done.length > 0 && <h2 style={{ ...s.h2, marginTop: 18 }}>Recent</h2>}
        {done.map((p) => <ListItem key={p.id} pr={p} selected={selected === p.id} onClick={() => setSelected(p.id)} />)}
      </aside>

      <section style={s.center} aria-label="Change">
        {!selected && prs && (
          <div style={s.emptyHero}>
            <h1 style={s.h1}>Ship a change</h1>
            <p style={s.dim}>
              A change is a branch you want to merge. havn builds it in isolation, shows how the
              data moves, and checks it can merge cleanly before anything reaches {`main`}.
            </p>
            <button style={s.btnPrimary} onClick={() => onNavigate("Git:Reviews")}>Create a change</button>
          </div>
        )}
        {reviewError && <div style={{ ...s.err, marginBottom: 12 }}>{reviewError}</div>}
        {selected && !review && !reviewError && <div style={s.dim}>Loading…</div>}
        {review && (
          <ChangeDetail review={review} busy={busy} buildRunning={buildRunning} onOpenFile={onOpenFile}
                        onBuild={() => {
                          buildBeforeRef.current = review.build?.started_at ?? null;
                          act("build", () => api.buildPr(review.pr.id));
                        }} />
        )}
      </section>

      {review && (
        <aside style={s.gate} aria-label="Merge gate">
          <h2 style={s.h2}>{review.pr.status === "open" ? "Ready to ship?" : "Shipped?"}</h2>
          {review.pr.status === "open" && review.gate.map((g) => <GateRow key={g.key} g={g} />)}
          {review.pr.status === "closed" && (
            <div style={s.dim}>Closed{review.pr.closed_by ? ` by ${review.pr.closed_by}` : ""}{review.pr.closed_at ? ` ${timeAgo(review.pr.closed_at)}` : ""} without merging.</div>
          )}

          {review.pr.status === "open" && isAuthor && review.pr.require_approval && (
            <div style={s.authorNote}>
              You opened this change, so someone else has to approve it.
              {" "}
              <button style={s.link} disabled={!!busy} onClick={waiveApproval}>Merge without review</button>
            </div>
          )}
          {review.pr.status === "open" && (
            <div style={s.reviewActs}>
              <button style={s.btn} disabled={!!busy || isAuthor}
                      title={isAuthor ? "You can't approve your own change" : undefined}
                      onClick={() => act("approve", () => api.approvePr(review.pr.id, user))}>
                Approve
              </button>
              <button style={s.btn} disabled={!!busy} onClick={() => setReasonOpen((v) => !v)} aria-expanded={reasonOpen}>
                Request changes
              </button>
            </div>
          )}
          {reasonOpen && (
            <div style={{ marginTop: 8 }}>
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="What needs to change?"
                aria-label="What needs to change"
                style={s.textarea}
              />
              <button style={{ ...s.btn, marginTop: 6 }} disabled={!!busy || !reason.trim()}
                      onClick={() => act("changes", () => api.requestPrChanges(review.pr.id, user, reason.trim()),
                                         () => { setReason(""); setReasonOpen(false); })}>
                Send
              </button>
            </div>
          )}

          {review.pr.status === "open" && (
            <>
              <button
                style={review.ready ? s.merge : s.mergeOff}
                disabled={!review.ready || !!busy || running}
                onClick={merge}
              >
                {busy === "merge" ? "Merging…" : `Merge into ${review.pr.base_ref}`}
              </button>
              <div style={s.why}>
                {!review.ready
                  ? `Waiting on: ${review.gate.filter((g) => g.required && g.state !== "pass").map((g) => g.label.toLowerCase()).join(", ")}`
                  : !review.build_current ? "Ready, but the latest commit has no passing build" : "Ready to merge"}
              </div>
              <div style={s.plan}>
                <b style={{ fontWeight: 500, color: "var(--havn-text)" }}>On merge</b>
                <ol style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {review.plan.map((step) => <li key={step}>{step}</li>)}
                </ol>
                <div style={{ marginTop: 6 }}>{review.after_merge}</div>
              </div>
            </>
          )}
          {(merged || review.pr.status === "merged") && (
            <div style={{ ...s.mergedBox, marginTop: 0 }} role="status">
              <div><span style={s.ok}>{"✓"}</span> Merged{review.pr.merged_by ? ` by ${review.pr.merged_by}` : ""}{review.pr.merged_at ? ` ${timeAgo(review.pr.merged_at)}` : ""}.</div>
              <div style={{ marginTop: 4 }}>{review.after_merge}</div>
              <button style={{ ...s.btnPrimary, marginTop: 8 }} onClick={onRunPipeline} disabled={running}>
                {running ? "Running…" : "▶ Run pipeline"}
              </button>
            </div>
          )}
        </aside>
      )}
    </div>
  );
}

function ListItem({ pr, selected, onClick }) {
  const tone = pr.status === "merged" ? "var(--havn-purple)" : pr.status === "closed" ? "var(--havn-text-dim)" : "var(--havn-green)";
  return (
    <button type="button" style={{ ...s.item, ...(selected ? s.itemOn : null) }} onClick={onClick} aria-current={selected ? "true" : undefined}>
      <span style={s.itemTitle}>{pr.title}</span>
      <span style={s.itemMeta}>
        <span style={{ color: tone }}>{pr.status}</span> · <span style={{ fontFamily: "var(--havn-font-mono)" }}>{pr.head_ref}</span>
        {pr.approvers?.length ? ` · ${pr.approvers.length} ✓` : ""}
      </span>
    </button>
  );
}

function ChangeDetail({ review, busy, buildRunning, onOpenFile, onBuild }) {
  const { pr, build, impact, files } = review;
  const changedModels = impact.nodes.filter((n) => n.role === "changed").length;
  const downstream = impact.nodes.filter((n) => n.role === "impacted").length;
  return (
    <div>
      <div style={s.hero}>
        <div style={{ minWidth: 0 }}>
          <h1 style={s.h1}>{pr.title}</h1>
          <div style={s.refs}>
            <span style={s.ref}>{pr.head_ref}</span><span aria-hidden="true">→</span><span style={s.ref}>{pr.base_ref}</span>
            <span>· {changedModels} model{changedModels === 1 ? "" : "s"} changed, {downstream} downstream · {files.length} file{files.length === 1 ? "" : "s"}</span>
            <span>· opened by {pr.author} {timeAgo(pr.created_at)}</span>
          </div>
          {pr.description && <p style={s.desc}>{pr.description}</p>}
        </div>
        {pr.status === "open" && (
          <button style={s.btn} onClick={onBuild} disabled={buildRunning || !!busy}>
            {buildRunning ? "Building…" : build ? "Rebuild" : "Build"}
          </button>
        )}
      </div>

      {impact.nodes.length > 0 && <ImpactGraph impact={impact} onOpenFile={onOpenFile} />}

      <h2 style={s.h2}>Data changes</h2>
      <DataDiff build={build} buildRunning={buildRunning} onBuild={pr.status === "open" ? onBuild : null} />

      <h2 style={{ ...s.h2, marginTop: 20 }}>Files</h2>
      <div style={s.card}>
        {files.length === 0 && <div style={{ ...s.dim, padding: 12 }}>No file changes between the branches.</div>}
        {files.map((f) => (
          <div key={f} style={s.fileRow}>
            <span style={s.mono}>{f}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const NODE_H = 28;
const ROW_GAP = 14;
const COL_GAP = 48;
const PAD = 12;
const CHAR_W = 7.2;

function ImpactGraph({ impact, onOpenFile }) {
  const cols = layoutColumns(impact.nodes, impact.edges);
  const colW = cols.map((c) => Math.max(...c.map((n) => n.name.length * CHAR_W + 24), 90));
  const xs = [];
  cols.forEach((_, i) => { xs[i] = i === 0 ? PAD : xs[i - 1] + colW[i - 1] + COL_GAP; });
  const tallest = Math.max(...cols.map((c) => c.length));
  const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * ROW_GAP;
  const width = xs[xs.length - 1] + colW[colW.length - 1] + PAD;
  const pos = {};
  cols.forEach((c, i) => {
    const colH = c.length * NODE_H + (c.length - 1) * ROW_GAP;
    const y0 = (height - colH) / 2;
    c.forEach((n, j) => { pos[n.name] = { x: xs[i], y: y0 + j * (NODE_H + ROW_GAP), w: colW[i], node: n }; });
  });
  return (
    <div style={{ ...s.card, padding: 12, marginBottom: 20, overflowX: "auto" }}>
      {/* Scales down to fit the card; scrolls only once it would get too small to read. */}
      <svg viewBox={`0 0 ${width} ${height}`} role="img"
           aria-label={`Impact: ${impact.nodes.filter((n) => n.role === "changed").map((n) => n.name).join(", ")} changed; ${impact.nodes.filter((n) => n.role === "impacted").length} downstream models rebuild`}
           style={{ display: "block", width: "100%", minWidth: Math.min(width, 520), maxWidth: width, height: "auto" }}>
        {impact.edges.map(([a, b]) => {
          const p = pos[a], q = pos[b];
          if (!p || !q) return null;
          const x1 = p.x + p.w, y1 = p.y + NODE_H / 2, x2 = q.x, y2 = q.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          const hot = q.node.role !== "upstream" && p.node.role !== "upstream";
          return <path key={`${a}>${b}`} d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`}
                       fill="none" stroke={hot ? "var(--havn-accent)" : "var(--havn-border-light)"} strokeWidth="1.5" />;
        })}
        {Object.values(pos).map(({ x, y, w, node }) => {
          const changed = node.role === "changed";
          const impacted = node.role === "impacted";
          return (
            <g key={node.name} style={{ cursor: node.path ? "pointer" : "default" }}
               onClick={() => node.path && onOpenFile(node.path)}>
              <title>{`${node.name} · ${changed ? "changed in this branch" : impacted ? "rebuilt downstream" : "upstream (unchanged)"}`}</title>
              <rect x={x} y={y} width={w} height={NODE_H} rx="6"
                    fill="var(--havn-bg)"
                    stroke={changed ? "var(--havn-yellow)" : impacted ? "var(--havn-accent)" : "var(--havn-border-light)"}
                    strokeDasharray={impacted ? "4 3" : undefined} strokeWidth={changed ? 1.6 : 1.2} />
              <text x={x + 12} y={y + NODE_H / 2 + 4} fontFamily="var(--havn-font-mono)" fontSize="11.5"
                    fill={changed ? "var(--havn-text)" : "var(--havn-text-secondary)"}>{node.name}</text>
            </g>
          );
        })}
      </svg>
      <div style={s.legend}>
        <span><i style={{ ...s.key, borderColor: "var(--havn-yellow)" }} />changed</span>
        <span><i style={{ ...s.key, borderColor: "var(--havn-accent)", borderStyle: "dashed" }} />rebuilt downstream</span>
        <span><i style={s.key} />upstream</span>
      </div>
    </div>
  );
}

const DIFF_ORDER = { modified: 0, added: 1, removed: 2, unchanged: 3 };

function DataDiff({ build, buildRunning, onBuild }) {
  if (buildRunning) return <div style={{ ...s.card, ...s.pad, ...s.dim }}>Building the branch in an isolated copy of the warehouse…</div>;
  if (!build) {
    return (
      <div style={{ ...s.card, ...s.pad }}>
        <div style={s.dim}>Build this change to see how its data differs from the base branch.</div>
        {onBuild && <button style={{ ...s.btn, marginTop: 10 }} onClick={onBuild}>Build</button>}
      </div>
    );
  }
  if (build.status === "error") {
    return <div style={{ ...s.card, ...s.pad }}><div style={s.bad}>Build failed</div><pre style={s.pre}>{build.error}</pre></div>;
  }
  const rows = Object.entries(build.data_diff || {})
    .map(([table, d]) => ({ table, ...d }))
    .sort((a, b) => (DIFF_ORDER[a.status] ?? 9) - (DIFF_ORDER[b.status] ?? 9) || a.table.localeCompare(b.table));
  const moving = rows.filter((r) => r.status !== "unchanged");
  const still = rows.length - moving.length;
  return (
    <div style={s.card}>
      <div style={s.diffMeta}>
        Built {timeAgo(build.finished_at)} · {build.branch_head ? build.branch_head.slice(0, 7) : ""} · {moving.length} table{moving.length === 1 ? "" : "s"} differ{moving.length === 1 ? "s" : ""}, {still} unchanged
      </div>
      {moving.length === 0 && <div style={{ ...s.pad, ...s.dim }}>No table's data differs from the base branch.</div>}
      {moving.map((r) => (
        <div key={r.table} style={s.diffRow}>
          <div style={s.diffTop}>
            <span style={s.mono}>{r.table}</span>
            <span style={{ ...s.badge, ...(r.status === "removed" ? s.badgeBad : r.status === "added" ? s.badgeOk : s.badgeWarn) }}>{r.status}</span>
            <span style={s.diffNums}>
              {r.main_rows != null && r.pr_rows != null && <span>{Number(r.main_rows).toLocaleString()} → {Number(r.pr_rows).toLocaleString()} rows</span>}
              {r.added_rows ? <span style={s.ok}> +{Number(r.added_rows).toLocaleString()}</span> : null}
              {r.removed_rows ? <span style={s.bad}> −{Number(r.removed_rows).toLocaleString()}</span> : null}
            </span>
          </div>
          {(r.schema_changes || []).length > 0 && (
            <div style={s.schema}>
              {r.schema_changes.map((c, i) => (
                <div key={i}>
                  {c.type === "added" && <span style={s.ok}>+ {c.column} <span style={s.dimInline}>{c.data_type}</span></span>}
                  {c.type === "removed" && <span style={s.bad}>− {c.column} <span style={s.dimInline}>{c.data_type}</span></span>}
                  {c.type === "type_changed" && <span style={s.warn}>~ {c.column} <span style={s.dimInline}>{c.from} → {c.to}</span></span>}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

const GATE_ICON = { pass: "✓", fail: "✗", warn: "!", pending: "○" };
const GATE_COLOR = { pass: "var(--havn-green)", fail: "var(--havn-red)", warn: "var(--havn-yellow)", pending: "var(--havn-text-dim)" };

function GateRow({ g }) {
  return (
    <div style={s.gateRow}>
      <span style={{ color: GATE_COLOR[g.state], fontWeight: 600, textAlign: "center" }} aria-label={g.state}>{GATE_ICON[g.state]}</span>
      <div style={{ minWidth: 0 }}>
        <div>{g.label}{!g.required && <span style={s.optional}>recommended</span>}</div>
        <div style={s.gateDetail}>{g.detail}</div>
      </div>
    </div>
  );
}

const btn = {
  border: "1px solid var(--havn-btn-border)", background: "var(--havn-btn-bg)", color: "var(--havn-text)",
  borderRadius: "var(--havn-radius)", padding: "5px 12px", fontSize: 13, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
};

const s = {
  layout: { display: "grid", gridTemplateColumns: "250px minmax(0, 1fr) 300px", height: "100%", minHeight: 0 },
  list: { borderRight: "1px solid var(--havn-border)", padding: "16px 10px", overflow: "auto" },
  listHead: { display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "0 6px" },
  center: { padding: "20px 24px 40px", overflow: "auto", minWidth: 0 },
  gate: { borderLeft: "1px solid var(--havn-border)", padding: "18px 16px", background: "var(--havn-bg-tertiary)", overflow: "auto" },
  h1: { fontSize: 20, fontWeight: 500, margin: 0 },
  h2: { fontSize: 12, fontWeight: 500, color: "var(--havn-text-secondary)", textTransform: "uppercase", letterSpacing: ".06em", margin: "0 0 10px" },
  dim: { color: "var(--havn-text-secondary)", fontSize: 13, lineHeight: 1.5 },
  dimInline: { color: "var(--havn-text-dim)" },
  ok: { color: "var(--havn-green)" },
  warn: { color: "var(--havn-yellow)" },
  bad: { color: "var(--havn-red)" },
  err: { color: "var(--havn-red)", fontSize: 13 },
  mono: { fontFamily: "var(--havn-font-mono)", fontSize: 12.5, overflowWrap: "anywhere" },
  card: { background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)" },
  pad: { padding: "14px 16px" },
  item: {
    display: "flex", flexDirection: "column", gap: 2, width: "100%", textAlign: "left", padding: "8px 10px",
    border: "none", background: "none", borderRadius: "var(--havn-radius)", cursor: "pointer", color: "var(--havn-text)", fontFamily: "inherit",
  },
  itemOn: { background: "color-mix(in srgb, var(--havn-accent) 13%, transparent)" },
  itemTitle: { fontSize: 13, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  itemMeta: { fontSize: 11.5, color: "var(--havn-text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  emptyHero: { maxWidth: 520, padding: "40px 0" },
  hero: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, marginBottom: 18 },
  refs: { display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginTop: 8, fontSize: 12.5, color: "var(--havn-text-secondary)" },
  ref: { fontFamily: "var(--havn-font-mono)", fontSize: 12, padding: "1px 9px", borderRadius: 20, border: "1px solid var(--havn-border-light)", color: "var(--havn-text)" },
  desc: { fontSize: 13, color: "var(--havn-text-secondary)", margin: "10px 0 0", whiteSpace: "pre-wrap" },
  legend: { display: "flex", gap: 16, fontSize: 11.5, color: "var(--havn-text-secondary)", marginTop: 8 },
  key: { display: "inline-block", width: 14, height: 10, borderRadius: 3, border: "1.5px solid var(--havn-border-light)", marginRight: 6, verticalAlign: "-1px" },
  diffMeta: { fontSize: 12, color: "var(--havn-text-secondary)", padding: "10px 16px", borderBottom: "1px solid var(--havn-border)" },
  diffRow: { padding: "10px 16px", borderBottom: "1px solid var(--havn-border)" },
  diffTop: { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" },
  diffNums: { marginLeft: "auto", fontSize: 12.5, color: "var(--havn-text-secondary)", fontVariantNumeric: "tabular-nums" },
  schema: { fontFamily: "var(--havn-font-mono)", fontSize: 12, marginTop: 6, lineHeight: 1.7 },
  badge: { fontSize: 10.5, padding: "0 7px", borderRadius: 4, border: "1px solid" },
  badgeOk: { color: "var(--havn-green)", borderColor: "var(--havn-green)" },
  badgeBad: { color: "var(--havn-red)", borderColor: "var(--havn-red)" },
  badgeWarn: { color: "var(--havn-yellow)", borderColor: "var(--havn-yellow)" },
  pre: { fontFamily: "var(--havn-font-mono)", fontSize: 12, whiteSpace: "pre-wrap", color: "var(--havn-text-secondary)", margin: "8px 0 0" },
  fileRow: { padding: "7px 14px", borderBottom: "1px solid var(--havn-border)" },
  gateRow: { display: "grid", gridTemplateColumns: "18px minmax(0, 1fr)", gap: 8, padding: "10px 0", borderBottom: "1px solid var(--havn-border)", fontSize: 13 },
  gateDetail: { fontSize: 12, color: "var(--havn-text-secondary)", marginTop: 1, overflowWrap: "anywhere" },
  optional: { marginLeft: 6, fontSize: 10.5, color: "var(--havn-text-dim)", border: "1px solid var(--havn-border-light)", borderRadius: 4, padding: "0 5px" },
  authorNote: { fontSize: 12.5, color: "var(--havn-text-secondary)", marginTop: 14, lineHeight: 1.5 },
  reviewActs: { display: "flex", gap: 8, marginTop: 14 },
  textarea: {
    width: "100%", boxSizing: "border-box", minHeight: 64, padding: 8, fontSize: 13, fontFamily: "inherit",
    background: "var(--havn-bg)", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)",
  },
  merge: { ...btn, width: "100%", marginTop: 16, padding: "9px 12px", background: "var(--havn-accent)", borderColor: "var(--havn-accent)", color: "#fff", fontWeight: 500, fontSize: 14 },
  mergeOff: { ...btn, width: "100%", marginTop: 16, padding: "9px 12px", background: "none", borderStyle: "dashed", color: "var(--havn-text-dim)", cursor: "not-allowed", fontSize: 14 },
  why: { fontSize: 12, color: "var(--havn-text-secondary)", marginTop: 8, textAlign: "center" },
  plan: { marginTop: 16, fontSize: 12.5, lineHeight: 1.6, color: "var(--havn-text-secondary)", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", padding: "10px 12px" },
  mergedBox: { marginTop: 16, fontSize: 13, color: "var(--havn-text-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", padding: "10px 12px" },
  link: { background: "none", border: "none", padding: 0, color: "var(--havn-accent)", cursor: "pointer", fontSize: 12.5, fontFamily: "inherit" },
  btn,
  btnPrimary: { ...btn, background: "var(--havn-accent)", borderColor: "var(--havn-accent)", color: "#fff", fontWeight: 500 },
};

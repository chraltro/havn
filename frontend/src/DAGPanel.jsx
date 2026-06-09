import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { api } from "./api";
import { useHintTriggerFn } from "./HintSystem";

const SCHEMA_COLORS = {
  landing: "#7c8fa0",
  bronze: "#cd6445",
  silver: "#a8b4c4",
  gold: "#e3b341",
  source: "#484f58",
  ingest: "#58a6ff",
  import: "#bc8cff",
  seed: "#16a34a",
  exposure: "#7c3aed",
};

const NODE_W = 160;
const NODE_H = 56;
const LAYER_GAP_X = 220;
const NODE_GAP_Y = 78;

function getCV(prop) {
  return getComputedStyle(document.documentElement).getPropertyValue(prop).trim();
}

function formatRowDelta(current, previous) {
  if (previous == null || current == null) return null;
  const delta = current - previous;
  if (delta === 0) return null;
  const sign = delta > 0 ? "+" : "";
  return `${sign}${delta.toLocaleString()}`;
}

function layoutDAG(nodes, edges) {
  const adj = {};
  const radj = {};
  const inDeg = {};
  for (const n of nodes) {
    adj[n.id] = [];
    radj[n.id] = [];
    inDeg[n.id] = 0;
  }
  for (const e of edges) {
    if (adj[e.source]) adj[e.source].push(e.target);
    if (radj[e.target]) radj[e.target].push(e.source);
    if (inDeg[e.target] !== undefined) inDeg[e.target]++;
  }

  const layer = {};
  const queue = nodes.filter((n) => inDeg[n.id] === 0).map((n) => n.id);
  for (const id of queue) layer[id] = 0;

  const visited = new Set();
  const stack = [...queue];
  while (stack.length > 0) {
    const id = stack.shift();
    if (visited.has(id)) continue;
    visited.add(id);
    for (const next of adj[id] || []) {
      layer[next] = Math.max(layer[next] || 0, (layer[id] || 0) + 1);
      inDeg[next]--;
      if (inDeg[next] === 0) stack.push(next);
    }
  }

  const layers = {};
  for (const n of nodes) {
    const l = layer[n.id] || 0;
    if (!layers[l]) layers[l] = [];
    layers[l].push(n);
  }

  const maxLayer = Math.max(...Object.keys(layers).map(Number), 0);

  const posIndex = {};
  for (let l = 0; l <= maxLayer; l++) {
    const group = layers[l] || [];
    group.forEach((n, i) => { posIndex[n.id] = i; });
  }

  for (let sweep = 0; sweep < 4; sweep++) {
    if (sweep % 2 === 0) {
      for (let l = 1; l <= maxLayer; l++) {
        const group = layers[l] || [];
        for (const n of group) {
          const preds = radj[n.id] || [];
          if (preds.length > 0) {
            posIndex[n.id] = preds.reduce((s, p) => s + (posIndex[p] || 0), 0) / preds.length;
          }
        }
        group.sort((a, b) => (posIndex[a.id] || 0) - (posIndex[b.id] || 0));
        group.forEach((n, i) => { posIndex[n.id] = i; });
        layers[l] = group;
      }
    } else {
      for (let l = maxLayer - 1; l >= 0; l--) {
        const group = layers[l] || [];
        for (const n of group) {
          const succs = adj[n.id] || [];
          if (succs.length > 0) {
            posIndex[n.id] = succs.reduce((s, c) => s + (posIndex[c] || 0), 0) / succs.length;
          }
        }
        group.sort((a, b) => (posIndex[a.id] || 0) - (posIndex[b.id] || 0));
        group.forEach((n, i) => { posIndex[n.id] = i; });
        layers[l] = group;
      }
    }
  }

  const positions = {};
  const maxNodes = Math.max(...Object.values(layers).map((g) => g.length), 1);
  const canvasH = 80 + maxNodes * (NODE_H + NODE_GAP_Y);

  for (let l = 0; l <= maxLayer; l++) {
    const group = layers[l] || [];
    const totalH = group.length * (NODE_H + NODE_GAP_Y) - NODE_GAP_Y;
    const startY = Math.max(40, (canvasH - totalH) / 2);
    group.forEach((n, i) => {
      positions[n.id] = {
        x: 60 + l * LAYER_GAP_X,
        y: startY + i * (NODE_H + NODE_GAP_Y),
      };
    });
  }

  const edgeRoutes = {};
  const ROUTE_MARGIN = 20;
  for (const e of edges) {
    const srcLayer = layer[e.source] || 0;
    const tgtLayer = layer[e.target] || 0;
    const span = tgtLayer - srcLayer;
    if (span < 2) continue;

    const from = positions[e.source];
    const to = positions[e.target];
    if (!from || !to) continue;

    const y1 = from.y + NODE_H / 2;
    const y2 = to.y + NODE_H / 2;
    const waypoints = [];

    for (let il = srcLayer + 1; il < tgtLayer; il++) {
      const layerX = 60 + il * LAYER_GAP_X;
      const wpXBefore = layerX - (LAYER_GAP_X - NODE_W) / 2;
      const wpXAfter = layerX + NODE_W + (LAYER_GAP_X - NODE_W) / 2;

      const t = (il - srcLayer) / span;
      const naturalY = y1 + (y2 - y1) * t;
      const group = layers[il] || [];

      let blocked = false;
      for (const n of group) {
        const np = positions[n.id];
        if (np && naturalY >= np.y - ROUTE_MARGIN && naturalY <= np.y + NODE_H + ROUTE_MARGIN) {
          blocked = true;
          break;
        }
      }

      if (!blocked) {
        waypoints.push({ x: wpXBefore, y: naturalY });
        waypoints.push({ x: wpXAfter, y: naturalY });
        continue;
      }

      const ys = group.map((n) => positions[n.id].y).sort((a, b) => a - b);
      let bestGapY = null;
      let bestDist = Infinity;

      const aboveY = ys[0] - ROUTE_MARGIN - 5;
      if (aboveY > 0) {
        const d = Math.abs(naturalY - aboveY);
        if (d < bestDist) { bestDist = d; bestGapY = aboveY; }
      }
      for (let i = 0; i < ys.length - 1; i++) {
        const gapTop = ys[i] + NODE_H + ROUTE_MARGIN;
        const gapBot = ys[i + 1] - ROUTE_MARGIN;
        if (gapBot > gapTop) {
          const mid = (gapTop + gapBot) / 2;
          const d = Math.abs(naturalY - mid);
          if (d < bestDist) { bestDist = d; bestGapY = mid; }
        }
      }
      const belowY = ys[ys.length - 1] + NODE_H + ROUTE_MARGIN + 5;
      const d = Math.abs(naturalY - belowY);
      if (d < bestDist) { bestDist = d; bestGapY = belowY; }

      const safeY = bestGapY ?? naturalY;
      waypoints.push({ x: wpXBefore, y: safeY });
      waypoints.push({ x: wpXAfter, y: safeY });
    }

    if (waypoints.length > 0) {
      edgeRoutes[e.source + "|" + e.target] = waypoints;
    }
  }

  const width = 120 + (maxLayer + 1) * LAYER_GAP_X;

  return { positions, width, height: canvasH, edgeRoutes };
}

// ---------------------------------------------------------------------------
// Detail Panel (shown when a node is clicked in rewind mode)
// ---------------------------------------------------------------------------

function DetailPanel({ modelName, runId, runs, snapshotsByRun, onClose, onRestore, showConfirm }) {
  const [sample, setSample] = useState(null);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState(false);

  const snapshot = snapshotsByRun?.[runId]?.[modelName];

  useEffect(() => {
    if (!runId || !modelName || !snapshot?.file_path) { setSample(null); return; }
    setLoading(true);
    api.getSnapshotSample(runId, modelName, 50)
      .then(setSample)
      .catch(() => setSample(null))
      .finally(() => setLoading(false));
  }, [runId, modelName]);

  // Row count history across runs
  const history = useMemo(() => {
    if (!runs || !snapshotsByRun) return [];
    return runs.slice().reverse().map(r => {
      const s = snapshotsByRun[r.run_id]?.[modelName];
      return s ? { run_id: r.run_id, ts: r.started_at, row_count: s.row_count } : null;
    }).filter(Boolean);
  }, [runs, snapshotsByRun, modelName]);

  async function handleRestore() {
    if (showConfirm && !(await showConfirm("Restore Snapshot", `Restore ${modelName} from this run? Downstream models will be re-built.`, "Restore", true))) return;
    if (!showConfirm && !confirm(`Restore ${modelName} from this run? Downstream models will be re-built.`)) return;
    setRestoring(true);
    try {
      await onRestore(runId, modelName);
    } finally {
      setRestoring(false);
    }
  }

  return (
    <div style={ds.panel}>
      <div style={ds.panelHeader}>
        <span style={ds.panelTitle}>{modelName}</span>
        <button onClick={onClose} style={ds.closeBtn}>{"\u00D7"}</button>
      </div>

      {snapshot ? (
        <div style={ds.panelBody}>
          <div style={ds.statRow}>
            <span style={ds.statLabel}>Rows</span>
            <span style={ds.statValue}>{snapshot.row_count?.toLocaleString()}</span>
          </div>
          <div style={ds.statRow}>
            <span style={ds.statLabel}>Columns</span>
            <span style={ds.statValue}>{snapshot.col_count}</span>
          </div>
          <div style={ds.statRow}>
            <span style={ds.statLabel}>Size</span>
            <span style={ds.statValue}>
              {snapshot.size_bytes < 1048576
                ? `${(snapshot.size_bytes / 1024).toFixed(1)} KB`
                : `${(snapshot.size_bytes / 1048576).toFixed(1)} MB`}
            </span>
          </div>
          <div style={ds.statRow}>
            <span style={ds.statLabel}>Status</span>
            <span style={{ ...ds.statValue, color: snapshot.file_path ? "var(--havn-green)" : "var(--havn-text-dim)" }}>
              {snapshot.file_path ? "Restorable" : "Expired"}
            </span>
          </div>

          {/* Row count history sparkline */}
          {history.length > 1 && (
            <div style={ds.histSection}>
              <span style={ds.statLabel}>Row count history</span>
              <div style={ds.sparkContainer}>
                {(() => {
                  const counts = history.map(h => h.row_count);
                  const max = Math.max(...counts, 1);
                  const w = 200, h = 40;
                  return (
                    <svg width={w} height={h} style={{ display: "block" }}>
                      <polyline
                        fill="none"
                        stroke="var(--havn-accent, #3ECFB4)"
                        strokeWidth="1.5"
                        points={counts.map((c, i) =>
                          `${(i / (counts.length - 1)) * w},${h - (c / max) * (h - 4) - 2}`
                        ).join(" ")}
                      />
                    </svg>
                  );
                })()}
              </div>
            </div>
          )}

          {/* Sample data preview */}
          {loading && <div style={ds.loadingText}>Loading sample...</div>}
          {sample && sample.columns && sample.rows && sample.rows.length > 0 && (
            <div style={ds.sampleSection}>
              <span style={ds.statLabel}>Sample data</span>
              <div style={ds.sampleTable}>
                <table style={ds.table}>
                  <thead>
                    <tr>{sample.columns.map((c, i) => <th key={i} style={ds.th}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {sample.rows.slice(0, 10).map((row, ri) => (
                      <tr key={ri}>
                        {row.map((v, ci) => <td key={ci} style={ds.td}>{v ?? ""}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Restore button */}
          {snapshot.file_path && (
            <button
              onClick={handleRestore}
              disabled={restoring}
              style={ds.restoreBtn}
            >
              {restoring ? "Restoring..." : "Restore to this point"}
            </button>
          )}
        </div>
      ) : (
        <div style={ds.panelBody}>
          <div style={ds.loadingText}>No snapshot for this model at this run.</div>
        </div>
      )}
    </div>
  );
}

// Detail panel styles
const ds = {
  panel: { width: 320, borderLeft: "1px solid var(--havn-border)", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--havn-bg-secondary)", flexShrink: 0 },
  panelHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 12px", borderBottom: "1px solid var(--havn-border)" },
  panelTitle: { fontWeight: 600, fontSize: 13, fontFamily: "var(--havn-font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  closeBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 16, padding: "2px 6px", lineHeight: 1 },
  panelBody: { flex: 1, overflow: "auto", padding: "12px", fontSize: 12 },
  statRow: { display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "5px 0", borderBottom: "1px solid var(--havn-border-light)" },
  statLabel: { color: "var(--havn-text-secondary)", fontSize: 11, fontWeight: 500 },
  statValue: { fontWeight: 600, fontSize: 12, fontFamily: "var(--havn-font-mono)" },
  histSection: { marginTop: 14 },
  sparkContainer: { marginTop: 6 },
  sampleSection: { marginTop: 14 },
  sampleTable: { marginTop: 6, overflow: "auto", maxHeight: 200, border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 10, fontFamily: "var(--havn-font-mono)" },
  th: { textAlign: "left", padding: "4px 6px", background: "var(--havn-bg-tertiary)", borderBottom: "1px solid var(--havn-border-light)", position: "sticky", top: 0, fontWeight: 600, fontSize: 10 },
  td: { padding: "3px 6px", borderBottom: "1px solid var(--havn-border-light)", maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  restoreBtn: { marginTop: 14, padding: "6px 12px", background: "var(--havn-green)", color: "#fff", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", cursor: "pointer", fontSize: 11, fontWeight: 500, width: "100%" },
  loadingText: { color: "var(--havn-text-dim)", fontStyle: "italic", padding: "8px 0", fontSize: 11 },
};

// ---------------------------------------------------------------------------
// Main DAG Panel
// ---------------------------------------------------------------------------

export default function DAGPanel({ onOpenFile, showConfirm }) {
  const canvasRef = useRef(null);
  const [dag, setDag] = useState(null);
  const [hovered, setHovered] = useState(null);
  const setHintTrigger = useHintTriggerFn();
  const [error, setError] = useState(null);
  const [dagSearch, setDagSearch] = useState("");
  const [selectedNode, setSelectedNode] = useState(null);

  // Column lineage state
  const [columnLineage, setColumnLineage] = useState(null); // {model, columns, depends_on}
  const [highlightedColumn, setHighlightedColumn] = useState(null); // column name being traced
  const [columnEdgeSet, setColumnEdgeSet] = useState(null); // Set of "source|target" edge keys
  const [allLineage, setAllLineage] = useState(null); // cached result of getAllLineage

  // Zoom & pan state
  const [scale, setScale] = useState(1.0);
  const [offsetX, setOffsetX] = useState(0);
  const [offsetY, setOffsetY] = useState(0);
  const isPanning = useRef(false);
  const panStart = useRef({ x: 0, y: 0, ox: 0, oy: 0 });
  const needsFit = useRef(true);

  // Rewind state
  const [runs, setRuns] = useState([]);
  const [snapshots, setSnapshots] = useState([]);
  const [sliderIndex, setSliderIndex] = useState(-1);
  const [rewindMode, setRewindMode] = useState(false);

  const loadDAG = useCallback(() => {
    setError(null);
    api.getDAG().then(setDag).catch((e) => setError(e.message || "Failed to load DAG"));
  }, []);

  useEffect(() => {
    loadDAG();
    setHintTrigger("dagOpened", true);
  }, []);

  // Auto-refresh when pipeline completes
  useEffect(() => {
    const handler = () => loadDAG();
    window.addEventListener("havn-data-changed", handler);
    return () => window.removeEventListener("havn-data-changed", handler);
  }, [loadDAG]);

  // Fetch column lineage when a node is selected (non-rewind mode)
  useEffect(() => {
    if (!selectedNode || rewindMode) { setColumnLineage(null); setHighlightedColumn(null); setColumnEdgeSet(null); return; }
    // Only fetch for transform models (not ingest/export scripts)
    const node = dag?.nodes?.find(n => n.id === selectedNode);
    if (!node || node.id.startsWith("script:")) { setColumnLineage(null); return; }
    api.getLineage(selectedNode).then(setColumnLineage).catch(() => setColumnLineage(null));
  }, [selectedNode, rewindMode, dag]);

  // When a column is highlighted, compute which edges carry it
  useEffect(() => {
    if (!highlightedColumn || !columnLineage || !dag) { setColumnEdgeSet(null); return; }
    // Find which source tables this column comes from
    const sources = columnLineage.columns?.[highlightedColumn] || [];
    const sourceTables = new Set(sources.map(s => s.source_table));
    // Highlight edges from those source tables to this model
    const edgeKeys = new Set();
    const modelId = columnLineage.model;
    // Direct edges from sources to this model
    for (const e of dag.edges || []) {
      if (e.target === modelId && sourceTables.has(e.source)) {
        edgeKeys.add(`${e.source}|${e.target}`);
      }
    }
    // Also trace upstream: for each source table, find edges that feed into it
    // Use allLineage if available for deeper tracing
    if (allLineage) {
      const visited = new Set([modelId]);
      const queue = [...sourceTables];
      while (queue.length > 0) {
        const table = queue.shift();
        if (visited.has(table)) continue;
        visited.add(table);
        const tLineage = allLineage.find(l => l.model === table);
        if (!tLineage) continue;
        // Find columns in this table that feed into our highlighted column
        for (const [col, srcs] of Object.entries(tLineage.columns || {})) {
          // Check if this column is one of the source columns for our highlighted column
          const isRelevant = sources.some(s => s.source_table === table && s.source_column === col) ||
            [...sourceTables].some(st => st === table);
          if (isRelevant) {
            for (const s of srcs) {
              for (const e of dag.edges || []) {
                if (e.target === table && e.source === s.source_table) {
                  edgeKeys.add(`${e.source}|${e.target}`);
                  if (!visited.has(s.source_table)) queue.push(s.source_table);
                }
              }
            }
          }
        }
      }
    }
    setColumnEdgeSet(edgeKeys.size > 0 ? edgeKeys : null);
  }, [highlightedColumn, columnLineage, dag, allLineage]);

  // Lazy-load all lineage when a column is first highlighted
  useEffect(() => {
    if (highlightedColumn && !allLineage) {
      api.getAllLineage().then(setAllLineage).catch(() => {});
    }
  }, [highlightedColumn, allLineage]);

  // Load rewind data when entering rewind mode
  useEffect(() => {
    if (!rewindMode) return;
    Promise.all([
      api.getRewindRuns(),
      api.getRewindSnapshots(),
    ]).then(([r, s]) => {
      setRuns(r);
      setSnapshots(s);
      if (r.length > 0) setSliderIndex(0);
    }).catch(() => {});
  }, [rewindMode]);

  // Index snapshots by run_id -> model_name
  const snapshotsByRun = useMemo(() => {
    const map = {};
    for (const s of snapshots) {
      if (!map[s.run_id]) map[s.run_id] = {};
      map[s.run_id][s.model_name] = s;
    }
    return map;
  }, [snapshots]);

  // Previous run snapshots (for delta calculation)
  const currentRunId = runs[sliderIndex]?.run_id;
  const prevRunId = runs[sliderIndex + 1]?.run_id;

  const currentSnaps = snapshotsByRun[currentRunId] || {};
  const prevSnaps = snapshotsByRun[prevRunId] || {};

  // Memoize layout
  const layout = useMemo(() => {
    if (!dag || dag.nodes.length === 0) return null;
    return layoutDAG(dag.nodes, dag.edges);
  }, [dag]);

  // Fit-to-view: calculate scale and offset to show entire DAG
  const fitToView = useCallback(() => {
    if (!layout || !canvasRef.current) return;
    const container = canvasRef.current.parentElement;
    if (!container) return;
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    const { width: dagW, height: dagH } = layout;
    const pad = 40;
    const sx = (cw - pad * 2) / dagW;
    const sy = (ch - pad * 2) / dagH;
    const s = Math.min(sx, sy, 3.0);
    const clampedScale = Math.max(0.2, Math.min(3.0, s));
    setScale(clampedScale);
    setOffsetX((cw - dagW * clampedScale) / 2);
    setOffsetY((ch - dagH * clampedScale) / 2);
  }, [layout]);

  // Auto fit-to-view on initial render and DAG data changes
  useEffect(() => {
    if (layout && needsFit.current) {
      // Small delay to ensure container has been laid out
      requestAnimationFrame(() => {
        fitToView();
        needsFit.current = false;
      });
    }
  }, [layout, fitToView]);

  // Mark that we need to re-fit only on first load (not mode toggles)
  useEffect(() => {
    if (!dag) needsFit.current = true;
  }, [dag]);

  const draw = useCallback(() => {
    if (!layout || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    const { nodes, edges } = dag;
    const { positions, edgeRoutes } = layout;

    // Size canvas to fill container
    const container = canvas.parentElement;
    const cw = container ? container.clientWidth : 800;
    const ch = container ? container.clientHeight : 600;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = cw * dpr;
    canvas.height = ch * dpr;
    canvas.style.width = cw + "px";
    canvas.style.height = ch + "px";
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cw, ch);

    // Apply pan and zoom
    ctx.save();
    ctx.translate(offsetX, offsetY);
    ctx.scale(scale, scale);

    // Compute full transitive lineage for hovered node
    let lineageSet = null;
    if (hovered) {
      lineageSet = new Set([hovered]);
      // Upstream (ancestors): follow edges backwards
      const upQueue = [hovered];
      while (upQueue.length > 0) {
        const cur = upQueue.pop();
        for (const e of edges) {
          if (e.target === cur && !lineageSet.has(e.source)) {
            lineageSet.add(e.source);
            upQueue.push(e.source);
          }
        }
      }
      // Downstream (descendants): follow edges forwards
      const downQueue = [hovered];
      while (downQueue.length > 0) {
        const cur = downQueue.pop();
        for (const e of edges) {
          if (e.source === cur && !lineageSet.has(e.target)) {
            lineageSet.add(e.target);
            downQueue.push(e.target);
          }
        }
      }
    }

    // Draw edges
    for (const e of edges) {
      const from = positions[e.source];
      const to = positions[e.target];
      if (!from || !to) continue;

      const edgeKey = `${e.source}|${e.target}`;
      const isColumnHighlighted = columnEdgeSet ? columnEdgeSet.has(edgeKey) : false;
      const isHighlighted = isColumnHighlighted || (lineageSet ? (lineageSet.has(e.source) && lineageSet.has(e.target)) : false);
      ctx.strokeStyle = isColumnHighlighted ? getCV("--havn-purple") : isHighlighted ? getCV("--havn-accent") : getCV("--havn-border-light");
      ctx.lineWidth = isColumnHighlighted ? 3 : isHighlighted ? 2 : 1.5;
      ctx.globalAlpha = isColumnHighlighted ? 1 : isHighlighted ? 1 : (columnEdgeSet ? 0.15 : (hovered ? 0.3 : 0.8));

      const x1 = from.x + NODE_W;
      const y1 = from.y + NODE_H / 2;
      const x2 = to.x;
      const y2 = to.y + NODE_H / 2;

      const routeKey = e.source + "|" + e.target;
      const waypoints = edgeRoutes[routeKey];

      if (waypoints && waypoints.length > 0) {
        const pts = [{ x: x1, y: y1 }, ...waypoints, { x: x2, y: y2 }];
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        const tension = 0.5;
        for (let i = 0; i < pts.length - 1; i++) {
          const p0 = pts[Math.max(i - 1, 0)];
          const p1 = pts[i];
          const p2 = pts[i + 1];
          const p3 = pts[Math.min(i + 2, pts.length - 1)];
          const cp1x = p1.x + (p2.x - p0.x) / (6 / tension);
          const cp1y = p1.y + (p2.y - p0.y) / (6 / tension);
          const cp2x = p2.x - (p3.x - p1.x) / (6 / tension);
          const cp2y = p2.y - (p3.y - p1.y) / (6 / tension);
          ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
        }
        ctx.stroke();
      } else {
        ctx.beginPath();
        const cpx = (x1 + x2) / 2;
        ctx.moveTo(x1, y1);
        ctx.bezierCurveTo(cpx, y1, cpx, y2, x2, y2);
        ctx.stroke();
      }

      // Arrowhead — orient along the edge direction
      const arrowLen = 8;
      const arrowW = 4;
      // Get the direction from the last segment of the edge
      let dx, dy;
      if (waypoints && waypoints.length > 0) {
        const prev = waypoints[waypoints.length - 1];
        dx = x2 - prev.x;
        dy = y2 - prev.y;
      } else {
        // Bezier: approximate tangent at endpoint
        const cpx = (x1 + x2) / 2;
        dx = x2 - cpx;
        dy = y2 - y2; // horizontal tangent for simple bezier
        dx = 1; dy = 0; // fallback horizontal
      }
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(x2, y2);
      ctx.lineTo(x2 - ux * arrowLen + uy * arrowW, y2 - uy * arrowLen - ux * arrowW);
      ctx.lineTo(x2 - ux * arrowLen - uy * arrowW, y2 - uy * arrowLen + ux * arrowW);
      ctx.closePath();
      ctx.fill();
    }

    ctx.globalAlpha = 1;

    // Draw nodes
    const fontFamily = getCV("--havn-font") || "-apple-system, sans-serif";
    const monoFamily = getCV("--havn-font-mono") || "monospace";

    for (const n of nodes) {
      const pos = positions[n.id];
      if (!pos) continue;

      const color = SCHEMA_COLORS[n.schema] || getCV("--havn-accent");
      const isHovered = hovered === n.id;
      const isSelected = selectedNode === n.id;
      const isSearchMatch = dagSearch && n.id.toLowerCase().includes(dagSearch.toLowerCase());
      const isTable = n.type === "table";
      const snap = rewindMode ? currentSnaps[n.id] : null;
      const prevSnap = rewindMode ? prevSnaps[n.id] : null;

      // Compute if node is in the column trace path
      const isInColumnTrace = columnEdgeSet ? (
        n.id === selectedNode ||
        [...columnEdgeSet].some(k => { const [s, t] = k.split("|"); return s === n.id || t === n.id; })
      ) : false;

      if (columnEdgeSet && !isInColumnTrace && !isHovered) {
        ctx.globalAlpha = 0.15;
      } else if (dagSearch && !isSearchMatch) {
        ctx.globalAlpha = 0.15;
      } else if (lineageSet && !isHovered) {
        ctx.globalAlpha = lineageSet.has(n.id) ? 1 : 0.25;
      } else {
        ctx.globalAlpha = 1;
      }

      // Node background. Active nodes (hovered/selected) get the schema color
      // for both border and glow so the highlight matches the box itself,
      // rather than a generic theme accent. Search matches stay accent-colored
      // so they read as a distinct "found it" affordance.
      ctx.fillStyle = isHovered || isSelected ? getCV("--havn-bg") : getCV("--havn-bg-secondary");
      ctx.strokeStyle = isSearchMatch ? getCV("--havn-accent") : color;
      ctx.lineWidth = isHovered || isSelected || isSearchMatch ? 2.5 : (isTable ? 2 : 1.5);

      const r = 7;
      if (isHovered || isSelected) {
        ctx.shadowColor = color;
        ctx.shadowBlur = 12;
        ctx.shadowOffsetX = 0;
        ctx.shadowOffsetY = 2;
      }
      ctx.beginPath();
      ctx.roundRect(pos.x, pos.y, NODE_W, NODE_H, r);
      ctx.fill();
      ctx.stroke();
      ctx.shadowColor = "transparent";
      ctx.shadowBlur = 0;

      // Schema accent stripe (left edge)
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.roundRect(pos.x, pos.y, 3, NODE_H, [r, 0, 0, r]);
      ctx.fill();

      // Label — active nodes get the schema color to match their border/glow.
      ctx.fillStyle = isHovered || isSelected ? color : getCV("--havn-text");
      ctx.font = `600 11px ${fontFamily}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(n.label, pos.x + NODE_W / 2, pos.y + 16, NODE_W - 20);

      // Rewind mode: show row count + delta
      if (rewindMode && snap) {
        const rowStr = snap.row_count?.toLocaleString() ?? "?";
        ctx.fillStyle = getCV("--havn-text-secondary") || "#8b949e";
        ctx.font = `500 10px ${monoFamily}`;
        ctx.textAlign = "center";
        ctx.fillText(`${rowStr} rows`, pos.x + NODE_W / 2, pos.y + NODE_H - 14, NODE_W - 16);

        // Delta from previous run
        const delta = formatRowDelta(snap.row_count, prevSnap?.row_count);
        if (delta) {
          ctx.fillStyle = delta.startsWith("+") ? "#3fb950" : "#f85149";
          ctx.font = `bold 9px ${monoFamily}`;
          ctx.textAlign = "right";
          ctx.fillText(delta, pos.x + NODE_W - 6, pos.y + NODE_H - 4);
        }

        // Schema change indicator
        if (prevSnap && snap.schema_hash && prevSnap.schema_hash && snap.schema_hash !== prevSnap.schema_hash) {
          ctx.fillStyle = "#d29922";
          ctx.beginPath();
          ctx.arc(pos.x + 10, pos.y + 10, 4, 0, Math.PI * 2);
          ctx.fill();
        }

        // Restorable indicator
        if (!snap.file_path) {
          ctx.globalAlpha = 0.5;
          ctx.fillStyle = "#8b949e";
          ctx.font = `9px ${monoFamily}`;
          ctx.textAlign = "left";
          ctx.fillText("expired", pos.x + 4, pos.y + NODE_H - 4);
          ctx.globalAlpha = 1;
        }
      } else if (!rewindMode) {
        // Type badge
        const badge = n.type === "ingest" ? "I" : n.type === "import" ? "\u2191" : n.type === "source" ? "S" : n.type === "seed" ? "D" : n.type === "exposure" ? "E" : n.type === "table" ? "T" : "V";
        ctx.fillStyle = color;
        ctx.font = `bold 9px ${monoFamily}`;
        ctx.textAlign = "right";
        ctx.fillText(badge, pos.x + NODE_W - 6, pos.y + 12);
      }
    }

    ctx.globalAlpha = 1;
    ctx.restore();
  }, [dag, layout, hovered, rewindMode, currentSnaps, prevSnaps, selectedNode, scale, offsetX, offsetY, dagSearch, columnEdgeSet]);

  useEffect(() => {
    draw();
  }, [draw]);

  // Redraw on container resize
  useEffect(() => {
    if (!canvasRef.current) return;
    const container = canvasRef.current.parentElement;
    if (!container || !window.ResizeObserver) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(container);
    return () => ro.disconnect();
  }, [draw]);

  // Convert screen coordinates to DAG coordinates (accounting for pan/zoom)
  function screenToDAG(e) {
    const rect = canvasRef.current.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const mx = (sx - offsetX) / scale;
    const my = (sy - offsetY) / scale;
    return { mx, my, sx, sy };
  }

  function findNodeAt(mx, my) {
    const { positions } = layout;
    for (const n of dag.nodes) {
      const p = positions[n.id];
      if (p && mx >= p.x && mx <= p.x + NODE_W && my >= p.y && my <= p.y + NODE_H) {
        return n;
      }
    }
    return null;
  }

  function handleMouseDown(e) {
    if (!layout || !canvasRef.current) return;
    const { mx, my, sx, sy } = screenToDAG(e);
    const node = findNodeAt(mx, my);
    if (!node) {
      isPanning.current = true;
      panStart.current = { x: sx, y: sy, ox: offsetX, oy: offsetY };
      canvasRef.current.style.cursor = "grabbing";
    }
  }

  function handleMouseMove(e) {
    if (!layout || !canvasRef.current) return;

    if (isPanning.current) {
      const rect = canvasRef.current.getBoundingClientRect();
      const sx = e.clientX - rect.left;
      const sy = e.clientY - rect.top;
      setOffsetX(panStart.current.ox + (sx - panStart.current.x));
      setOffsetY(panStart.current.oy + (sy - panStart.current.y));
      return;
    }

    const { mx, my } = screenToDAG(e);
    const node = findNodeAt(mx, my);
    setHovered(node ? node.id : null);
    canvasRef.current.style.cursor = node ? "pointer" : "grab";
  }

  function handleMouseUp() {
    isPanning.current = false;
    if (canvasRef.current) {
      canvasRef.current.style.cursor = "grab";
    }
  }

  const lastClickRef = useRef({ time: 0, nodeId: null });

  function handleClick(e) {
    if (!layout || !canvasRef.current) return;
    const { mx, my } = screenToDAG(e);
    const node = findNodeAt(mx, my);

    if (node) {
      const now = Date.now();
      const last = lastClickRef.current;
      // Double-click: open file
      if (last.nodeId === node.id && now - last.time < 400) {
        if (onOpenFile && node.path) onOpenFile(node.path);
        lastClickRef.current = { time: 0, nodeId: null };
        return;
      }
      lastClickRef.current = { time: now, nodeId: node.id };

      if (rewindMode) {
        setSelectedNode(selectedNode === node.id ? null : node.id);
      } else {
        // Single click: select node to show column lineage panel
        setSelectedNode(selectedNode === node.id ? null : node.id);
        setHighlightedColumn(null);
      }
      return;
    }
    setSelectedNode(null);
    setHighlightedColumn(null);
  }

  function handleWheel(e) {
    if (!canvasRef.current) return;
    e.preventDefault();
    const rect = canvasRef.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9;
    const newScale = Math.max(0.2, Math.min(3.0, scale * zoomFactor));

    // Zoom toward cursor position
    const ratio = newScale / scale;
    setOffsetX(mx - (mx - offsetX) * ratio);
    setOffsetY(my - (my - offsetY) * ratio);
    setScale(newScale);
  }

  async function handleRestore(runId, modelName) {
    try {
      const result = await api.restoreSnapshot(runId, modelName, true);
      if (result.status === "success") {
        alert(`Restored ${modelName}. ${result.cascade_results ? Object.keys(result.cascade_results).length + " downstream models rebuilt." : ""}`);
        // Refresh rewind data
        const [r, s] = await Promise.all([api.getRewindRuns(), api.getRewindSnapshots()]);
        setRuns(r);
        setSnapshots(s);
      }
    } catch (err) {
      alert("Restore failed: " + (err.message || err));
    }
  }

  if (error) {
    return <div style={styles.errorState}>{error}</div>;
  }

  if (!dag) {
    return <div style={styles.loading}>Loading dependency graph...</div>;
  }

  if (dag.nodes.length === 0) {
    return (
      <div style={styles.empty}>
        <div style={styles.emptyTitle}>No models in the DAG</div>
        <div style={styles.emptyHint}>
          Add <code style={styles.emptyCode}>*.sql</code> files to the <code style={styles.emptyCode}>transform/</code> directory to build your dependency graph.
        </div>
      </div>
    );
  }

  const currentRun = runs[sliderIndex];

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <div style={styles.headerControls}>
          {!rewindMode && (
            <div style={styles.legend}>
              <span style={styles.legendItem}>
                <span style={{ ...styles.legendDot, background: SCHEMA_COLORS.import }} />imported
              </span>
              <span style={styles.legendItem}>
                <span style={{ ...styles.legendDot, background: SCHEMA_COLORS.ingest }} />ingest
              </span>
              <span style={styles.legendItem}>
                <span style={{ ...styles.legendDot, background: SCHEMA_COLORS.bronze }} />bronze
              </span>
              <span style={styles.legendItem}>
                <span style={{ ...styles.legendDot, background: SCHEMA_COLORS.silver }} />silver
              </span>
              <span style={styles.legendItem}>
                <span style={{ ...styles.legendDot, background: SCHEMA_COLORS.gold }} />gold
              </span>
              <span style={styles.legendItem}>
                <span style={{ ...styles.legendDot, background: SCHEMA_COLORS.seed }} />seed
              </span>
            </div>
          )}
          {!rewindMode && (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                value={dagSearch}
                onChange={(e) => setDagSearch(e.target.value)}
                placeholder="Search models..."
                style={{ padding: "3px 8px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "11px", fontFamily: "var(--havn-font-mono)", outline: "none", width: 140 }}
              />
              {dagSearch && <button onClick={() => setDagSearch("")} style={{ background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", padding: 0, lineHeight: 1 }}>&times;</button>}
            </div>
          )}
          <button
            onClick={() => { setRewindMode(!rewindMode); setSelectedNode(null); }}
            style={{
              ...styles.rewindBtn,
              background: rewindMode ? "var(--havn-accent, #3ECFB4)" : "transparent",
              color: rewindMode ? "var(--havn-bg, #0B0E14)" : "var(--havn-text-secondary)",
            }}
          >
            Rewind
          </button>
        </div>
      </div>

      {/* Timeline — placed above the canvas so it's always visible */}
      {rewindMode && runs.length > 0 && (() => {
        const sorted = runs.slice().reverse(); // oldest first
        const earliest = new Date(sorted[0].started_at).getTime();
        const latest = new Date(sorted[sorted.length - 1].started_at).getTime();
        const span = latest - earliest || 1;
        const fmtDate = (s) => s?.slice(0, 10) || "";
        const fmtTime = (s) => s?.slice(11, 16) || "";
        // Map sliderIndex (0=newest in runs[]) to position in sorted array
        const selectedSortedIdx = sorted.length - 1 - sliderIndex;

        return (
          <div style={styles.sliderContainer}>
            <span style={{ fontSize: 10, color: "var(--havn-text-dim)", whiteSpace: "nowrap", flexShrink: 0 }}>
              {fmtDate(sorted[0].started_at)}<br />{fmtTime(sorted[0].started_at)}
            </span>
            <div
              style={{ flex: 1, position: "relative", height: 32, cursor: "pointer", margin: "0 8px" }}
              onClick={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                const pct = (e.clientX - rect.left) / rect.width;
                // Find closest run by position
                let bestIdx = 0;
                let bestDist = Infinity;
                sorted.forEach((r, i) => {
                  const t = new Date(r.started_at).getTime();
                  const pos = sorted.length === 1 ? 0.5 : (t - earliest) / span;
                  const d = Math.abs(pos - pct);
                  if (d < bestDist) { bestDist = d; bestIdx = i; }
                });
                setSliderIndex(sorted.length - 1 - bestIdx);
              }}
            >
              {/* Track line */}
              <div style={{ position: "absolute", top: 14, left: 0, right: 0, height: 2, background: "var(--havn-border)", borderRadius: 1 }} />
              {/* Run dots */}
              {sorted.map((r, i) => {
                const t = new Date(r.started_at).getTime();
                const pct = sorted.length === 1 ? 50 : ((t - earliest) / span) * 100;
                const isSelected = i === selectedSortedIdx;
                const statusColor = r.status === "success" ? "var(--havn-green)" : r.status === "failed" ? "var(--havn-red)" : "var(--havn-yellow)";
                return (
                  <div
                    key={r.run_id}
                    title={`${r.started_at?.slice(0, 19)} — ${r.status} (${r.trigger})`}
                    style={{
                      position: "absolute",
                      left: `${pct}%`,
                      top: isSelected ? 7 : 10,
                      width: isSelected ? 14 : 8,
                      height: isSelected ? 14 : 8,
                      borderRadius: "50%",
                      background: isSelected ? "var(--havn-accent, #3ECFB4)" : statusColor,
                      border: isSelected ? "2px solid #fff" : "1px solid var(--havn-bg)",
                      transform: "translateX(-50%)",
                      transition: "all 0.15s ease",
                      zIndex: isSelected ? 2 : 1,
                      boxShadow: isSelected ? "0 0 6px rgba(62,207,180,0.45)" : "none",
                    }}
                  />
                );
              })}
            </div>
            <span style={{ fontSize: 10, color: "var(--havn-text-dim)", whiteSpace: "nowrap", flexShrink: 0, textAlign: "right" }}>
              {fmtDate(sorted[sorted.length - 1].started_at)}<br />{fmtTime(sorted[sorted.length - 1].started_at)}
            </span>
            {/* Selected run info */}
            {currentRun && (
              <div style={{ marginLeft: 12, fontSize: 11, whiteSpace: "nowrap", flexShrink: 0 }}>
                <span style={{ fontWeight: 600 }}>{currentRun.started_at?.slice(11, 19)}</span>
                <span style={{
                  marginLeft: 6, fontSize: 10, fontWeight: 600,
                  color: currentRun.status === "success" ? "var(--havn-green)" : currentRun.status === "failed" ? "var(--havn-red)" : "var(--havn-yellow)",
                }}>
                  {currentRun.status}
                </span>
                <span style={{ color: "var(--havn-text-dim)", marginLeft: 6, fontSize: 10 }}>
                  {currentRun.trigger}
                </span>
              </div>
            )}
          </div>
        );
      })()}

      {rewindMode && runs.length === 0 && (
        <div style={styles.sliderContainer}>
          <span style={{ color: "var(--havn-text-dim)", fontSize: 12 }}>
            No pipeline runs recorded. Run a transform to start capturing snapshots.
          </span>
        </div>
      )}

      <div style={styles.mainArea}>
        <div style={{ flex: 1, overflow: "hidden", background: "var(--havn-bg-tertiary)", position: "relative" }} data-havn-hint="dag-canvas">
          <canvas
            ref={canvasRef}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={() => { setHovered(null); isPanning.current = false; }}
            onClick={handleClick}
            onWheel={handleWheel}
            style={{ ...styles.canvas, cursor: "grab", width: "100%", height: "100%" }}
          />
          {/* Zoom controls */}
          <div style={styles.zoomControls}>
            <button
              onClick={() => {
                const newScale = Math.min(3.0, scale * 1.2);
                const canvas = canvasRef.current;
                if (canvas) {
                  const cw = canvas.parentElement.clientWidth / 2;
                  const ch = canvas.parentElement.clientHeight / 2;
                  const ratio = newScale / scale;
                  setOffsetX(cw - (cw - offsetX) * ratio);
                  setOffsetY(ch - (ch - offsetY) * ratio);
                }
                setScale(newScale);
              }}
              style={styles.zoomBtn}
              title="Zoom in"
            >+</button>
            <button
              onClick={() => {
                const newScale = Math.max(0.2, scale / 1.2);
                const canvas = canvasRef.current;
                if (canvas) {
                  const cw = canvas.parentElement.clientWidth / 2;
                  const ch = canvas.parentElement.clientHeight / 2;
                  const ratio = newScale / scale;
                  setOffsetX(cw - (cw - offsetX) * ratio);
                  setOffsetY(ch - (ch - offsetY) * ratio);
                }
                setScale(newScale);
              }}
              style={styles.zoomBtn}
              title="Zoom out"
            >-</button>
            <button
              onClick={fitToView}
              style={{ ...styles.zoomBtn, fontSize: 10, letterSpacing: "0.02em" }}
              title="Fit to view"
            >Fit</button>
            <span style={styles.zoomPct}>{Math.round(scale * 100)}%</span>
          </div>
        </div>

        {/* Column lineage panel */}
        {!rewindMode && selectedNode && columnLineage && (
          <div style={styles.lineagePanel}>
            <div style={styles.lineagePanelHeader}>
              <span style={styles.lineagePanelTitle}>{selectedNode}</span>
              <div style={{ display: "flex", gap: 4 }}>
                {dag?.nodes?.find(n => n.id === selectedNode)?.path && (
                  <button onClick={() => onOpenFile(dag.nodes.find(n => n.id === selectedNode).path)} style={styles.lineageOpenBtn}>Open</button>
                )}
                <button onClick={() => { setSelectedNode(null); setHighlightedColumn(null); }} style={styles.lineageCloseBtn}>&times;</button>
              </div>
            </div>
            <div style={styles.lineagePanelLabel}>Column Lineage</div>
            <div style={styles.lineagePanelBody}>
              {Object.keys(columnLineage.columns || {}).length === 0 && (
                <div style={styles.lineageEmpty}>No column lineage data available</div>
              )}
              {Object.entries(columnLineage.columns || {}).map(([col, sources]) => (
                <div key={col}>
                  <button
                    onClick={() => setHighlightedColumn(highlightedColumn === col ? null : col)}
                    style={{
                      ...styles.lineageColBtn,
                      background: highlightedColumn === col ? "color-mix(in srgb, var(--havn-purple) 15%, transparent)" : "none",
                      color: highlightedColumn === col ? "var(--havn-purple)" : "var(--havn-text)",
                      borderLeft: highlightedColumn === col ? "2px solid var(--havn-purple)" : "2px solid transparent",
                    }}
                  >
                    {col}
                    <span style={styles.lineageColArrow}>{highlightedColumn === col ? "\u25BC" : "\u25B8"}</span>
                  </button>
                  {highlightedColumn === col && sources.length > 0 && (
                    <div style={styles.lineageSources}>
                      {sources.map((s, i) => (
                        <div key={i} style={styles.lineageSourceRow}>
                          <span style={styles.lineageSourceArrow}>&larr;</span>
                          <span style={styles.lineageSourceTable}>{s.source_table}</span>
                          <span style={styles.lineageSourceDot}>.</span>
                          <span style={styles.lineageSourceCol}>{s.source_column}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Rewind detail panel */}
        {rewindMode && selectedNode && currentRun && (
          <DetailPanel
            modelName={selectedNode}
            runId={currentRun.run_id}
            runs={runs}
            snapshotsByRun={snapshotsByRun}
            onClose={() => setSelectedNode(null)}
            onRestore={handleRestore}
            showConfirm={showConfirm}
          />
        )}
      </div>
    </div>
  );
}

const styles = {
  container: { display: "flex", flexDirection: "column", flex: 1, height: "100%", minHeight: 0, overflow: "hidden" },
  header: { display: "flex", alignItems: "center", justifyContent: "flex-end", padding: "8px 12px", borderBottom: "1px solid var(--havn-border)", fontSize: "13px", flexShrink: 0 },
  headerControls: { display: "flex", alignItems: "center", gap: 12 },
  legend: { display: "flex", gap: "10px", fontSize: "11px", color: "var(--havn-text-secondary)", alignItems: "center", flexWrap: "wrap" },
  legendItem: { display: "flex", alignItems: "center", gap: "4px" },
  legendDot: { width: "7px", height: "7px", borderRadius: "50%", display: "inline-block", flexShrink: 0 },
  mainArea: { flex: 1, display: "flex", overflow: "hidden", minHeight: 0 },
  canvas: { display: "block" },
  loading: { display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--havn-text-secondary)", fontSize: 13 },
  errorState: { display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--havn-red)", fontSize: 13, padding: 24 },
  empty: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 8, padding: 32 },
  emptyTitle: { fontSize: 14, fontWeight: 600, color: "var(--havn-text-secondary)" },
  emptyHint: { fontSize: 12, color: "var(--havn-text-dim)", lineHeight: 1.6, textAlign: "center" },
  emptyCode: { fontFamily: "var(--havn-font-mono)", fontSize: 11, background: "var(--havn-bg-secondary)", padding: "1px 5px", borderRadius: "var(--havn-radius)", border: "1px solid var(--havn-border-light)" },
  rewindBtn: { border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", padding: "4px 12px", fontSize: 11, fontWeight: 500, cursor: "pointer" },
  sliderContainer: { display: "flex", alignItems: "center", gap: 10, padding: "8px 16px", borderBottom: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)", fontSize: 12, flexShrink: 0 },
  zoomControls: { position: "absolute", top: 8, right: 8, display: "flex", gap: 3, alignItems: "center", background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius-lg)", padding: "3px 4px", zIndex: 10 },
  zoomBtn: { background: "none", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 12, fontWeight: 600, width: 28, height: 24, display: "flex", alignItems: "center", justifyContent: "center" },
  zoomPct: { fontSize: 10, color: "var(--havn-text-dim)", minWidth: 34, textAlign: "center", fontWeight: 500, fontFamily: "var(--havn-font-mono)" },

  // Column lineage panel
  lineagePanel: { width: 360, minWidth: 260, borderLeft: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)", display: "flex", flexDirection: "column", overflow: "hidden", flexShrink: 0, resize: "horizontal", direction: "rtl" },
  lineagePanelHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 12px", borderBottom: "1px solid var(--havn-border)", gap: 8, direction: "ltr" },
  lineagePanelTitle: { fontSize: 12, fontWeight: 600, color: "var(--havn-accent)", fontFamily: "var(--havn-font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  lineagePanelLabel: { fontSize: 10, fontWeight: 600, color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: "0.6px", padding: "10px 12px 4px", direction: "ltr" },
  lineagePanelBody: { flex: 1, overflow: "auto", padding: "0 0 8px", direction: "ltr" },
  lineageEmpty: { padding: "16px 12px", color: "var(--havn-text-dim)", fontSize: 11, fontStyle: "italic" },
  lineageColBtn: { display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%", padding: "4px 12px", border: "none", cursor: "pointer", fontSize: 12, fontFamily: "var(--havn-font-mono)", fontWeight: 500, textAlign: "left" },
  lineageColArrow: { fontSize: 9, color: "var(--havn-text-dim)", flexShrink: 0, marginLeft: 4 },
  lineageSources: { padding: "2px 12px 6px 24px" },
  lineageSourceRow: { display: "flex", alignItems: "baseline", gap: 4, fontSize: 11, fontFamily: "var(--havn-font-mono)", padding: "2px 0", color: "var(--havn-text-secondary)", flexWrap: "wrap" },
  lineageSourceArrow: { color: "var(--havn-text-dim)", fontSize: 10, flexShrink: 0 },
  lineageSourceTable: { color: "var(--havn-purple)", fontWeight: 500 },
  lineageSourceDot: { color: "var(--havn-text-dim)" },
  lineageSourceCol: { color: "var(--havn-text-secondary)" },
  lineageOpenBtn: { background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 11, padding: "2px 8px", fontWeight: 500 },
  lineageCloseBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: "0 2px" },
};

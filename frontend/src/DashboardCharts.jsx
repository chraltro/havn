/**
 * Extended chart renderers for dashboards.
 * All charts are pure SVG, no external libraries.
 * Renders: gauge, treemap, heatmap, funnel, waterfall, histogram,
 *          radar, bubble, sparkline, progress, bullet, sankey.
 * Basic types (bar/line/area/scatter/pie/donut) are handled by ChartPanel.
 */

import React, { useState, useRef, useEffect, useMemo } from "react";
import { niceScale, fmtNum, fmtAxis, parseNum, trunc, donutArc, COLORS } from "./chartUtils";
import { getSeriesColor } from "./chartStyleDefaults";

const PAD = { top: 28, right: 24, bottom: 44, left: 56 };

// ---------------------------------------------------------------------------
// Tooltip helper
// ---------------------------------------------------------------------------

function Tooltip({ x, y, children, visible, svgWidth, svgHeight }) {
  if (!visible) return null;
  // Bounds checking: flip tooltip if near edges
  const tipW = 200;
  const flipX = svgWidth && (x + tipW + 16) > svgWidth;
  const flipY = y < 40;
  const tx = flipX ? x - tipW - 8 : x + 8;
  const ty = flipY ? y + 10 : y - 30;
  return (
    <g>
      <foreignObject x={tx} y={ty} width={tipW} height={60} style={{ overflow: "visible", pointerEvents: "none" }}>
        <div style={{
          background: "var(--havn-bg)", border: "1px solid var(--havn-border)",
          borderRadius: 6, padding: "4px 8px", fontSize: 12, color: "var(--havn-text)",
          whiteSpace: "nowrap", boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
        }}>
          {children}
        </div>
      </foreignObject>
    </g>
  );
}

// ---------------------------------------------------------------------------
// GAUGE
// ---------------------------------------------------------------------------

// Find first numeric column index
function findNumericCol(columns, rows) {
  for (let ci = 0; ci < (columns || []).length; ci++) {
    const sample = (rows || []).slice(0, 10).map(r => parseNum(r[ci])).filter(v => !isNaN(v));
    if (sample.length > 0) return ci;
  }
  return 0;
}

export function Gauge({ columns, rows, width, height, config }) {
  const numIdx = findNumericCol(columns, rows);
  const value = rows?.[0] ? parseNum(rows[0][numIdx]) : 0;
  // Auto-scale if min/max not configured
  const allVals = rows?.map(r => parseNum(r[numIdx])).filter(v => !isNaN(v)) || [0];
  const dataMin = Math.min(...allVals, 0);
  const dataMax = Math.max(...allVals, 100);
  const min = config?.min ?? dataMin;
  const max = config?.max ?? (dataMax + (dataMax - dataMin) * 0.1 || 100);
  const pct = Math.max(0, Math.min(1, (value - min) / (max - min)));

  const cx = width / 2;
  const cy = height * 0.65;
  const r = Math.min(cx - 20, cy - 20);
  const startAngle = Math.PI;
  const endAngle = 0;
  const valueAngle = startAngle + (endAngle - startAngle + 2 * Math.PI) % (2 * Math.PI) * pct;
  // Actually: semicircle from PI to 2PI
  const sweepAngle = Math.PI + pct * Math.PI;

  const bgArc = donutArc(cx, cy, r, r * 0.7, Math.PI, 2 * Math.PI);
  const valueArc = donutArc(cx, cy, r, r * 0.7, Math.PI, Math.PI + pct * Math.PI);

  // Needle
  const needleAngle = Math.PI + pct * Math.PI;
  const nx = cx + (r * 0.6) * Math.cos(needleAngle);
  const ny = cy + (r * 0.6) * Math.sin(needleAngle);

  // Color zones
  const color = pct < 0.33 ? "var(--havn-red)" : pct < 0.66 ? "var(--havn-yellow)" : "var(--havn-green)";

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <path d={bgArc} fill="var(--havn-border)" opacity={0.3} />
      <path d={valueArc} fill={color} opacity={0.8} />
      <line x1={cx} y1={cy} x2={nx} y2={ny} stroke="var(--havn-text)" strokeWidth={2.5} strokeLinecap="round" />
      <circle cx={cx} cy={cy} r={4} fill="var(--havn-text)" />
      <text x={cx} y={cy + 24} textAnchor="middle" fill="var(--havn-text)" fontSize={20} fontWeight={700}>
        {fmtNum(value)}
      </text>
      <text x={cx - r} y={cy + 14} textAnchor="start" fill="var(--havn-text-secondary, #888)" fontSize={11}>
        {fmtNum(min)}
      </text>
      <text x={cx + r} y={cy + 14} textAnchor="end" fill="var(--havn-text-secondary, #888)" fontSize={11}>
        {fmtNum(max)}
      </text>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// TREEMAP (squarified)
// ---------------------------------------------------------------------------

export function Treemap({ columns, rows, width, height, config = {} }) {
  const [tip, setTip] = useState(null);
  const labelIdx = 0;
  const valueIdx = columns.length > 1 ? 1 : 0;

  const items = useMemo(() => {
    const raw = rows.map(r => ({ label: String(r[labelIdx] ?? ""), value: Math.max(0, parseNum(r[valueIdx]) || 0) }));
    return raw.sort((a, b) => b.value - a.value);
  }, [rows, labelIdx, valueIdx]);

  const total = items.reduce((s, i) => s + i.value, 0) || 1;

  // Simple slice-and-dice layout
  const rects = useMemo(() => {
    const result = [];
    let x = PAD.left / 2, y = PAD.top / 2;
    let w = width - PAD.left, h = height - PAD.top - PAD.bottom / 2;
    let remaining = [...items];
    let totalRemaining = total;

    while (remaining.length > 0) {
      const isHorizontal = w >= h;
      const item = remaining.shift();
      const frac = item.value / totalRemaining;
      totalRemaining -= item.value;

      if (remaining.length === 0) {
        result.push({ ...item, x, y, w, h, color: getSeriesColor(config, result.length, item.label) });
      } else if (isHorizontal) {
        const itemW = w * frac;
        result.push({ ...item, x, y, w: itemW, h, color: getSeriesColor(config, result.length, item.label) });
        x += itemW;
        w -= itemW;
      } else {
        const itemH = h * frac;
        result.push({ ...item, x, y, w, h: itemH, color: getSeriesColor(config, result.length, item.label) });
        y += itemH;
        h -= itemH;
      }
    }
    return result;
  }, [items, total, width, height]);

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {rects.map((r, i) => (
        <g key={i}
          onMouseEnter={() => setTip({ x: r.x + r.w / 2, y: r.y + r.h / 2, label: r.label, value: r.value })}
          onTouchStart={() => setTip({ x: r.x + r.w / 2, y: r.y + r.h / 2, label: r.label, value: r.value })}
          onMouseLeave={() => setTip(null)}
        >
          <rect x={r.x + 1} y={r.y + 1} width={Math.max(0, r.w - 2)} height={Math.max(0, r.h - 2)}
            fill={r.color} rx={3} opacity={0.85} />
          {r.w > 40 && r.h > 20 && (
            <text x={r.x + r.w / 2} y={r.y + r.h / 2 - 4} textAnchor="middle" fill="#fff" fontSize={11} fontWeight={600}>
              {trunc(r.label, Math.floor(r.w / 7))}
            </text>
          )}
          {r.w > 40 && r.h > 34 && (
            <text x={r.x + r.w / 2} y={r.y + r.h / 2 + 12} textAnchor="middle" fill="#fff" fontSize={10} opacity={0.8}>
              {fmtNum(r.value)}
            </text>
          )}
        </g>
      ))}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label}: {fmtNum(tip.value)}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// HEATMAP
// ---------------------------------------------------------------------------

export function Heatmap({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);

  // Expect: col0 = Y category, col1 = X category, col2 = value
  const yLabels = useMemo(() => [...new Set(rows.map(r => String(r[0])))], [rows]);
  const xLabels = useMemo(() => [...new Set(rows.map(r => String(r[1])))], [rows]);

  const values = useMemo(() => {
    const map = {};
    let min = Infinity, max = -Infinity;
    for (const r of rows) {
      const key = `${r[0]}|${r[1]}`;
      const v = parseNum(r[2]) || 0;
      map[key] = v;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    return { map, min, max };
  }, [rows]);

  const leftPad = 80;
  const topPad = 30;
  const cellW = Math.max(10, (width - leftPad - 20) / Math.max(1, xLabels.length));
  const cellH = Math.max(10, (height - topPad - 30) / Math.max(1, yLabels.length));

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {/* X labels */}
      {xLabels.map((label, xi) => (
        <text key={xi} x={leftPad + xi * cellW + cellW / 2} y={topPad - 6}
          textAnchor="middle" fill="var(--havn-text-secondary, #888)" fontSize={10}>
          {trunc(label, 8)}
        </text>
      ))}
      {/* Y labels */}
      {yLabels.map((label, yi) => (
        <text key={yi} x={leftPad - 6} y={topPad + yi * cellH + cellH / 2 + 4}
          textAnchor="end" fill="var(--havn-text-secondary, #888)" fontSize={10}>
          {trunc(label, 10)}
        </text>
      ))}
      {/* Cells */}
      {yLabels.map((yLabel, yi) =>
        xLabels.map((xLabel, xi) => {
          const key = `${yLabel}|${xLabel}`;
          const v = values.map[key] ?? 0;
          const range = values.max - values.min || 1;
          const intensity = (v - values.min) / range;
          return (
            <rect
              key={key}
              x={leftPad + xi * cellW + 1}
              y={topPad + yi * cellH + 1}
              width={Math.max(0, cellW - 2)}
              height={Math.max(0, cellH - 2)}
              fill={getSeriesColor(config, 0, null)}
              opacity={0.1 + intensity * 0.85}
              rx={2}
              onMouseEnter={() => setTip({ x: leftPad + xi * cellW + cellW / 2, y: topPad + yi * cellH, label: `${yLabel} / ${xLabel}`, value: v })}
              onTouchStart={() => setTip({ x: leftPad + xi * cellW + cellW / 2, y: topPad + yi * cellH, label: `${yLabel} / ${xLabel}`, value: v })}
              onMouseLeave={() => setTip(null)}
            />
          );
        })
      )}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label}: {fmtNum(tip.value)}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// FUNNEL
// ---------------------------------------------------------------------------

export function Funnel({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);
  const labelIdx = 0;
  const valueIdx = columns.length > 1 ? 1 : 0;

  const items = rows.map(r => ({
    label: String(r[labelIdx] ?? ""),
    value: parseNum(r[valueIdx]) || 0,
  }));

  const maxVal = items.reduce((m, i) => Math.max(m, i.value), 0) || 1;
  const barH = Math.max(16, (height - PAD.top - PAD.bottom) / Math.max(1, items.length) - 4);
  const maxBarW = width - 140;

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {items.map((item, i) => {
        const frac = item.value / maxVal;
        const barW = maxBarW * frac;
        const x = (width - barW) / 2;
        const y = PAD.top + i * (barH + 4);
        const pct = i > 0 ? ((item.value / items[0].value) * 100).toFixed(0) + "%" : "100%";
        return (
          <g key={i}
            onMouseEnter={() => setTip({ x: x + barW / 2, y, label: item.label, value: item.value })}
            onTouchStart={() => setTip({ x: x + barW / 2, y, label: item.label, value: item.value })}
            onMouseLeave={() => setTip(null)}
          >
            <rect x={x} y={y} width={barW} height={barH} rx={4}
              fill={getSeriesColor(config, i, item.label)} opacity={0.85} />
            <text x={width / 2} y={y + barH / 2 + 4} textAnchor="middle" fill="#fff" fontSize={12} fontWeight={600}>
              {trunc(item.label, 20)} — {fmtNum(item.value)}
            </text>
            <text x={width - 16} y={y + barH / 2 + 4} textAnchor="end" fill="var(--havn-text-secondary, #888)" fontSize={11}>
              {pct}
            </text>
          </g>
        );
      })}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label}: {fmtNum(tip.value)}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// WATERFALL
// ---------------------------------------------------------------------------

export function Waterfall({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);
  const labelIdx = 0;
  const valueIdx = columns.length > 1 ? 1 : 0;

  const items = rows.map(r => ({
    label: String(r[labelIdx] ?? ""),
    value: parseNum(r[valueIdx]) || 0,
  }));

  // Compute running totals
  let running = 0;
  const bars = items.map((item, i) => {
    const isTotal = i === items.length - 1; // last item is treated as total
    const start = isTotal ? 0 : running;
    running += isTotal ? 0 : item.value;
    const end = isTotal ? item.value : running;
    return { ...item, start: Math.min(start, end), height: Math.abs(end - start), isPositive: item.value >= 0, isTotal };
  });

  const allValues = bars.flatMap(b => [b.start, b.start + b.height]);
  const minV = Math.min(0, ...allValues);
  const maxV = Math.max(0, ...allValues);
  const scale = niceScale(minV, maxV);

  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const barW = Math.max(8, plotW / bars.length - 4);

  function yPos(v) { return PAD.top + plotH - ((v - scale.min) / (scale.max - scale.min)) * plotH; }

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {/* Y axis */}
      {scale.ticks.map(t => (
        <g key={t}>
          <line x1={PAD.left} x2={width - PAD.right} y1={yPos(t)} y2={yPos(t)} stroke="var(--havn-border)" strokeDasharray="3,3" />
          <text x={PAD.left - 6} y={yPos(t) + 4} textAnchor="end" fill="var(--havn-text-secondary, #888)" fontSize={10}>{fmtAxis(t)}</text>
        </g>
      ))}
      {/* Bars */}
      {bars.map((bar, i) => {
        const x = PAD.left + i * (barW + 4) + 2;
        const y = yPos(bar.start + bar.height);
        const h = Math.max(1, yPos(bar.start) - y);
        const color = bar.isTotal ? "var(--havn-text-secondary, #888)" : bar.isPositive ? "var(--havn-green)" : "var(--havn-red)";
        return (
          <g key={i}
            onMouseEnter={() => setTip({ x: x + barW / 2, y, label: bar.label, value: bar.value })}
            onTouchStart={() => setTip({ x: x + barW / 2, y, label: bar.label, value: bar.value })}
            onMouseLeave={() => setTip(null)}
          >
            <rect x={x} y={y} width={barW} height={h} fill={color} rx={2} opacity={0.85} />
            <text x={x + barW / 2} y={height - PAD.bottom + 14} textAnchor="middle" fill="var(--havn-text-secondary, #888)" fontSize={10}>
              {trunc(bar.label, 8)}
            </text>
          </g>
        );
      })}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label}: {fmtNum(tip.value)}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// HISTOGRAM
// ---------------------------------------------------------------------------

export function Histogram({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);

  // Find the first numeric column
  const numIdx = useMemo(() => {
    for (let ci = 0; ci < (columns || []).length; ci++) {
      const sample = rows.slice(0, 20).map(r => parseNum(r[ci])).filter(v => !isNaN(v));
      if (sample.length > rows.slice(0, 20).length * 0.5) return ci;
    }
    return 0;
  }, [columns, rows]);

  const values = useMemo(() => rows.map(r => parseNum(r[numIdx])).filter(v => !isNaN(v)), [rows, numIdx]);

  if (values.length === 0) {
    return <text x={width / 2} y={height / 2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>No numeric data for histogram</text>;
  }

  const binCount = Math.max(5, Math.min(30, Math.ceil(Math.sqrt(values.length))));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const binWidth = (max - min) / binCount || 1;

  const bins = useMemo(() => {
    const b = Array(binCount).fill(0);
    for (const v of values) {
      const idx = Math.min(binCount - 1, Math.floor((v - min) / binWidth));
      b[idx]++;
    }
    return b;
  }, [values, binCount, min, binWidth]);

  const maxCount = Math.max(...bins) || 1;
  const scale = niceScale(0, maxCount);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const barW = plotW / binCount;

  function yPos(v) { return PAD.top + plotH - (v / scale.max) * plotH; }

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {scale.ticks.map(t => (
        <g key={t}>
          <line x1={PAD.left} x2={width - PAD.right} y1={yPos(t)} y2={yPos(t)} stroke="var(--havn-border)" strokeDasharray="3,3" />
          <text x={PAD.left - 6} y={yPos(t) + 4} textAnchor="end" fill="var(--havn-text-secondary, #888)" fontSize={10}>{fmtAxis(t)}</text>
        </g>
      ))}
      {bins.map((count, i) => {
        const x = PAD.left + i * barW;
        const h = (count / scale.max) * plotH;
        const y = PAD.top + plotH - h;
        const binStart = min + i * binWidth;
        const binEnd = binStart + binWidth;
        return (
          <g key={i}
            onMouseEnter={() => setTip({ x: x + barW / 2, y, label: `${fmtNum(binStart)} - ${fmtNum(binEnd)}`, value: count })}
            onTouchStart={() => setTip({ x: x + barW / 2, y, label: `${fmtNum(binStart)} - ${fmtNum(binEnd)}`, value: count })}
            onMouseLeave={() => setTip(null)}
          >
            <rect x={x + 0.5} y={y} width={Math.max(0, barW - 1)} height={h} fill={getSeriesColor(config, 0, null)} opacity={0.8} />
          </g>
        );
      })}
      {/* X axis labels */}
      {[0, Math.floor(binCount / 2), binCount].map(i => (
        <text key={i} x={PAD.left + i * barW} y={height - PAD.bottom + 16}
          textAnchor="middle" fill="var(--havn-text-secondary, #888)" fontSize={10}>
          {fmtNum(min + i * binWidth)}
        </text>
      ))}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label}: {tip.value}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// RADAR / SPIDER
// ---------------------------------------------------------------------------

export function Radar({ columns, rows, width, height, config }) {
  if (!rows.length) return null;
  const metrics = columns.filter((_, i) => {
    const v = parseNum(rows[0][i]);
    return !isNaN(v);
  });

  const cx = width / 2;
  const cy = height / 2;
  const r = Math.min(cx, cy) - 40;
  const n = metrics.length;
  if (n < 3) return <svg width={width} height={height}><text x={cx} y={cy} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>Need 3+ numeric columns for radar</text></svg>;

  const maxVal = metrics.reduce((m, col) => {
    const idx = columns.indexOf(col);
    return Math.max(m, parseNum(rows[0][idx]) || 0);
  }, 0) || 1;

  const angleStep = (2 * Math.PI) / n;

  // Grid rings
  const rings = [0.25, 0.5, 0.75, 1.0];

  // Data polygon
  const points = metrics.map((col, i) => {
    const idx = columns.indexOf(col);
    const val = (parseNum(rows[0][idx]) || 0) / maxVal;
    const angle = -Math.PI / 2 + i * angleStep;
    return { x: cx + val * r * Math.cos(angle), y: cy + val * r * Math.sin(angle) };
  });

  const polyStr = points.map(p => `${p.x},${p.y}`).join(" ");

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {/* Grid */}
      {rings.map(ring => (
        <polygon key={ring}
          points={Array.from({ length: n }, (_, i) => {
            const angle = -Math.PI / 2 + i * angleStep;
            return `${cx + ring * r * Math.cos(angle)},${cy + ring * r * Math.sin(angle)}`;
          }).join(" ")}
          fill="none" stroke="var(--havn-border)" strokeWidth={0.5}
        />
      ))}
      {/* Axis lines */}
      {metrics.map((col, i) => {
        const angle = -Math.PI / 2 + i * angleStep;
        const lx = cx + r * Math.cos(angle);
        const ly = cy + r * Math.sin(angle);
        const labelX = cx + (r + 16) * Math.cos(angle);
        const labelY = cy + (r + 16) * Math.sin(angle);
        return (
          <g key={i}>
            <line x1={cx} y1={cy} x2={lx} y2={ly} stroke="var(--havn-border)" strokeWidth={0.5} />
            <text x={labelX} y={labelY + 4} textAnchor="middle" fill="var(--havn-text-secondary, #888)" fontSize={10}>
              {trunc(col, 10)}
            </text>
          </g>
        );
      })}
      {/* Data polygon */}
      <polygon points={polyStr} fill={getSeriesColor(config, 0, null)} fillOpacity={0.25} stroke={getSeriesColor(config, 0, null)} strokeWidth={2} />
      {points.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={3.5} fill={getSeriesColor(config, 0, null)} stroke="#fff" strokeWidth={1} />
      ))}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// BUBBLE (scatter with size)
// ---------------------------------------------------------------------------

export function Bubble({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);

  // Expect: col0 = x, col1 = y, col2 = size
  const data = useMemo(() => rows.map(r => ({
    x: parseNum(r[0]) || 0,
    y: parseNum(r[1]) || 0,
    size: Math.abs(parseNum(r[2]) || 1),
    label: columns.length > 3 ? String(r[3] ?? "") : `(${fmtNum(r[0])}, ${fmtNum(r[1])})`,
  })), [rows, columns]);

  const xVals = data.map(d => d.x);
  const yVals = data.map(d => d.y);
  const sizeVals = data.map(d => d.size);
  const xScale = niceScale(Math.min(...xVals), Math.max(...xVals));
  const yScale = niceScale(Math.min(...yVals), Math.max(...yVals));
  const maxSize = Math.max(...sizeVals) || 1;

  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  function xPos(v) { return PAD.left + ((v - xScale.min) / (xScale.max - xScale.min)) * plotW; }
  function yPos(v) { return PAD.top + plotH - ((v - yScale.min) / (yScale.max - yScale.min)) * plotH; }

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {yScale.ticks.map(t => (
        <g key={t}>
          <line x1={PAD.left} x2={width - PAD.right} y1={yPos(t)} y2={yPos(t)} stroke="var(--havn-border)" strokeDasharray="3,3" />
          <text x={PAD.left - 6} y={yPos(t) + 4} textAnchor="end" fill="var(--havn-text-secondary, #888)" fontSize={10}>{fmtAxis(t)}</text>
        </g>
      ))}
      {xScale.ticks.map(t => (
        <text key={t} x={xPos(t)} y={height - PAD.bottom + 16} textAnchor="middle" fill="var(--havn-text-secondary, #888)" fontSize={10}>{fmtAxis(t)}</text>
      ))}
      {data.map((d, i) => {
        const r = 4 + (d.size / maxSize) * 24;
        return (
          <circle key={i} cx={xPos(d.x)} cy={yPos(d.y)} r={r}
            fill={getSeriesColor(config, i, d.label)} fillOpacity={0.6} stroke={getSeriesColor(config, i, d.label)} strokeWidth={1}
            onMouseEnter={() => setTip({ x: xPos(d.x), y: yPos(d.y), label: d.label, value: d.size })}
            onTouchStart={() => setTip({ x: xPos(d.x), y: yPos(d.y), label: d.label, value: d.size })}
            onMouseLeave={() => setTip(null)}
          />
        );
      })}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label} (size: {fmtNum(tip.value)})</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// SPARKLINE
// ---------------------------------------------------------------------------

export function Sparkline({ columns, rows, width, height, config }) {
  const values = useMemo(() => rows.map(r => parseNum(r[columns.length > 1 ? 1 : 0])).filter(v => !isNaN(v)), [rows, columns]);
  if (values.length < 2) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pad = 4;
  const w = width - pad * 2;
  const h = height - pad * 2;

  const points = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * w;
    const y = pad + h - ((v - min) / range) * h;
    return `${x},${y}`;
  }).join(" ");

  const color = config?.color || COLORS[0];
  const last = values[values.length - 1];
  const prev = values[values.length - 2];

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <polyline points={points} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={pad + w} cy={pad + h - ((last - min) / range) * h} r={2.5} fill={last >= prev ? "var(--havn-green)" : "var(--havn-red)"} />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// PROGRESS BAR
// ---------------------------------------------------------------------------

export function ProgressBar({ columns, rows, width, height, config }) {
  const numIdx = findNumericCol(columns, rows);
  const value = rows?.[0] ? parseNum(rows[0][numIdx]) : 0;
  // Auto-scale max: use configured max, or compute from all data values (not fixed 100)
  const allVals = (rows || []).map(r => Math.abs(parseNum(r[numIdx]))).filter(v => !isNaN(v) && v > 0);
  const dataMax = allVals.length > 0 ? Math.max(...allVals) : Math.abs(value) || 100;
  const max = config?.max ?? dataMax;
  const pct = max > 0 ? Math.max(0, Math.min(1, Math.abs(value) / max)) : 0;
  const label = columns.length > 1 ? String(rows[0][1] ?? "") : "";

  const barH = Math.min(32, height / 2);
  const barY = (height - barH) / 2;
  const barW = width - 80;

  const color = pct < 0.33 ? "var(--havn-red)" : pct < 0.66 ? "var(--havn-yellow)" : "var(--havn-green)";

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {label && (
        <text x={12} y={barY - 6} fill="var(--havn-text)" fontSize={12}>{label}</text>
      )}
      <rect x={12} y={barY} width={barW} height={barH} rx={barH / 2} fill="var(--havn-border)" opacity={0.3} />
      <rect x={12} y={barY} width={barW * pct} height={barH} rx={barH / 2} fill={color} opacity={0.85} />
      <text x={barW + 20} y={barY + barH / 2 + 5} fill="var(--havn-text)" fontSize={14} fontWeight={600}>
        {(pct * 100).toFixed(0)}%
      </text>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// BULLET CHART
// ---------------------------------------------------------------------------

export function Bullet({ columns, rows, width, height, config }) {
  const numIdx = findNumericCol(columns, rows);
  const actual = rows?.[0] ? parseNum(rows[0][numIdx]) : 0;
  const numIdx2 = columns?.length > numIdx + 1 ? numIdx + 1 : -1;
  const target = numIdx2 >= 0 && rows?.[0] ? parseNum(rows[0][numIdx2]) : config?.target ?? (actual * 1.2 || 100);
  const max = config?.max ?? (Math.max(actual, isNaN(target) ? actual : target) * 1.2 || 100);

  const barH = Math.min(28, height / 3);
  const barY = (height - barH) / 2;
  const barW = width - 60;

  function xPos(v) { return 12 + (v / max) * barW; }

  // Qualitative ranges
  const ranges = [
    { pct: 0.6, opacity: 0.15 },
    { pct: 0.8, opacity: 0.1 },
    { pct: 1.0, opacity: 0.05 },
  ];

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {/* Background ranges */}
      {ranges.map((r, i) => (
        <rect key={i} x={12} y={barY - 4} width={barW * r.pct} height={barH + 8}
          fill="var(--havn-text)" opacity={r.opacity} rx={3} />
      ))}
      {/* Actual bar */}
      <rect x={12} y={barY} width={xPos(actual) - 12} height={barH} rx={3}
        fill={COLORS[0]} opacity={0.85} />
      {/* Target marker */}
      <line x1={xPos(target)} y1={barY - 8} x2={xPos(target)} y2={barY + barH + 8}
        stroke="var(--havn-text)" strokeWidth={2.5} />
      {/* Value label */}
      <text x={barW + 20} y={barY + barH / 2 + 5} fill="var(--havn-text)" fontSize={14} fontWeight={600}>
        {fmtNum(actual)}
      </text>
    </svg>
  );
}

// ---------------------------------------------------------------------------
// SANKEY (simplified — 2-column flow)
// ---------------------------------------------------------------------------

export function Sankey({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);

  if (!rows || rows.length === 0 || !columns || columns.length < 3) {
    return <svg width={width} height={height}><text x={width/2} y={height/2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>Need source, target, value columns</text></svg>;
  }

  // Expect: source, target, value
  const links = useMemo(() => rows.map(r => ({
    source: String(r[0] ?? ""),
    target: String(r[1] ?? ""),
    value: parseNum(r[2]) || 0,
  })), [rows]);

  const sources = useMemo(() => [...new Set(links.map(l => l.source))], [links]);
  const targets = useMemo(() => [...new Set(links.map(l => l.target))], [links]);

  const sourceTotal = {};
  const targetTotal = {};
  for (const l of links) {
    sourceTotal[l.source] = (sourceTotal[l.source] || 0) + l.value;
    targetTotal[l.target] = (targetTotal[l.target] || 0) + l.value;
  }

  const pad = 40;
  const nodeW = 16;
  const gapY = 4;
  const plotH = height - pad * 2;

  const totalSourceVal = Object.values(sourceTotal).reduce((s, v) => s + v, 0) || 1;
  const totalTargetVal = Object.values(targetTotal).reduce((s, v) => s + v, 0) || 1;

  // Layout source nodes
  let sy = pad;
  const sourcePos = {};
  for (const s of sources) {
    const h = (sourceTotal[s] / totalSourceVal) * (plotH - gapY * (sources.length - 1));
    sourcePos[s] = { x: pad, y: sy, h };
    sy += h + gapY;
  }

  // Layout target nodes
  let ty = pad;
  const targetPos = {};
  for (const t of targets) {
    const h = (targetTotal[t] / totalTargetVal) * (plotH - gapY * (targets.length - 1));
    targetPos[t] = { x: width - pad - nodeW, y: ty, h };
    ty += h + gapY;
  }

  // Build link paths
  const sourceOffset = {};
  const targetOffset = {};
  for (const s of sources) sourceOffset[s] = 0;
  for (const t of targets) targetOffset[t] = 0;

  const linkPaths = links.map((l, i) => {
    const sp = sourcePos[l.source];
    const tp = targetPos[l.target];
    if (!sp || !tp) return null;

    const sh = (l.value / sourceTotal[l.source]) * sp.h;
    const th = (l.value / targetTotal[l.target]) * tp.h;

    const sy1 = sp.y + sourceOffset[l.source];
    const sy2 = sy1 + sh;
    const ty1 = tp.y + targetOffset[l.target];
    const ty2 = ty1 + th;

    sourceOffset[l.source] += sh;
    targetOffset[l.target] += th;

    const sx = pad + nodeW;
    const tx = width - pad - nodeW;
    const mx = (sx + tx) / 2;

    const d = `M ${sx} ${sy1} C ${mx} ${sy1}, ${mx} ${ty1}, ${tx} ${ty1} L ${tx} ${ty2} C ${mx} ${ty2}, ${mx} ${sy2}, ${sx} ${sy2} Z`;

    return (
      <path key={i} d={d} fill={getSeriesColor(config, i, l.source)} fillOpacity={0.35}
        stroke={getSeriesColor(config, i, l.source)} strokeWidth={0.5}
        onMouseEnter={() => setTip({ x: mx, y: (sy1 + ty1) / 2, label: `${l.source} → ${l.target}`, value: l.value })}
        onTouchStart={() => setTip({ x: mx, y: (sy1 + ty1) / 2, label: `${l.source} → ${l.target}`, value: l.value })}
        onMouseLeave={() => setTip(null)}
      />
    );
  });

  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {linkPaths}
      {/* Source nodes */}
      {sources.map((s, i) => {
        const p = sourcePos[s];
        return (
          <g key={`s-${i}`}>
            <rect x={p.x} y={p.y} width={nodeW} height={p.h} fill={getSeriesColor(config, i, s)} rx={3} />
            <text x={p.x - 4} y={p.y + p.h / 2 + 4} textAnchor="end" fill="var(--havn-text)" fontSize={11}>
              {trunc(s, 14)}
            </text>
          </g>
        );
      })}
      {/* Target nodes */}
      {targets.map((t, i) => {
        const p = targetPos[t];
        return (
          <g key={`t-${i}`}>
            <rect x={p.x} y={p.y} width={nodeW} height={p.h} fill={getSeriesColor(config, sources.length + i, t)} rx={3} />
            <text x={p.x + nodeW + 4} y={p.y + p.h / 2 + 4} textAnchor="start" fill="var(--havn-text)" fontSize={11}>
              {trunc(t, 14)}
            </text>
          </g>
        );
      })}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true}>{tip.label}: {fmtNum(tip.value)}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// STACKED AREA
// ---------------------------------------------------------------------------

export function StackedArea({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);
  const [hoverIdx, setHoverIdx] = useState(null);
  const labelIdx = 0;
  const numericCols = useMemo(() => {
    return columns.reduce((acc, col, i) => {
      if (i === 0) return acc;
      const sample = rows.slice(0, 20).map(r => parseNum(r[i])).filter(v => !isNaN(v));
      if (sample.length > 0) acc.push(i);
      return acc;
    }, []);
  }, [columns, rows]);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const data = useMemo(() => {
    const labels = rows.map(r => String(r[labelIdx] ?? ""));
    const seriesRaw = numericCols.map(ci => rows.map(r => { const n = parseNum(r[ci]); return isNaN(n) ? 0 : Math.max(0, n); }));
    // Cumulative stacking
    const stacked = [];
    for (let si = 0; si < seriesRaw.length; si++) {
      stacked[si] = seriesRaw[si].map((v, ri) => v + (si > 0 ? stacked[si - 1][ri] : 0));
    }
    const maxVal = Math.max(...(stacked[stacked.length - 1] || [0]));
    return { labels, seriesRaw, stacked, maxVal };
  }, [rows, numericCols, labelIdx]);
  if (numericCols.length === 0 || rows.length === 0) return <svg width={width} height={height}><text x={width/2} y={height/2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>Need numeric columns for stacked area</text></svg>;
  const scale = niceScale(0, data.maxVal);
  const n = data.labels.length;
  const xPos = (i) => PAD.left + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yPos = (v) => PAD.top + plotH - ((v - scale.min) / (scale.max - scale.min || 1)) * plotH;
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      <g>
        {scale.ticks.map(t => <line key={t} x1={PAD.left} x2={PAD.left + plotW} y1={yPos(t)} y2={yPos(t)} stroke="var(--havn-border)" strokeDasharray="3,3" opacity={0.3} />)}
        {scale.ticks.map(t => <text key={`l${t}`} x={PAD.left - 6} y={yPos(t) + 4} textAnchor="end" fill="var(--havn-text-secondary)" fontSize={10}>{fmtAxis(t)}</text>)}
        {data.stacked.map((cumVals, si) => {
          const prevVals = si > 0 ? data.stacked[si - 1] : cumVals.map(() => 0);
          const d = `M ${xPos(0)} ${yPos(cumVals[0])} ${cumVals.map((v, i) => `L ${xPos(i)} ${yPos(v)}`).join(" ")} L ${xPos(n - 1)} ${yPos(prevVals[n - 1])} ${[...prevVals].reverse().map((v, i) => `L ${xPos(n - 1 - i)} ${yPos(v)}`).join(" ")} Z`;
          return <path key={si} d={d} fill={getSeriesColor(config, si, columns[numericCols[si]])} opacity={0.6} />;
        })}
      </g>
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true} svgWidth={width} svgHeight={height}>{tip.label}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// BOX PLOT
// ---------------------------------------------------------------------------

export function BoxPlot({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);
  if (!rows || rows.length === 0) return <svg width={width} height={height}><text x={width/2} y={height/2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>No data for box plot</text></svg>;
  // Find text column (category) and numeric column
  const textIdx = columns.findIndex((_, i) => { const s = rows.slice(0, 10).map(r => parseNum(r[i])).filter(v => !isNaN(v)); return s.length < rows.slice(0, 10).length * 0.3; });
  const numIdx = columns.findIndex((_, i) => { const s = rows.slice(0, 10).map(r => parseNum(r[i])).filter(v => !isNaN(v)); return s.length > rows.slice(0, 10).length * 0.5; });
  if (numIdx < 0) return <svg width={width} height={height}><text x={width/2} y={height/2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>Need a numeric column</text></svg>;
  // Group by category
  const groups = useMemo(() => {
    const map = {};
    for (const r of rows) {
      const key = textIdx >= 0 ? String(r[textIdx] ?? "all") : "all";
      if (!map[key]) map[key] = [];
      const v = parseNum(r[numIdx]);
      if (!isNaN(v)) map[key].push(v);
    }
    return Object.entries(map).map(([label, values]) => {
      values.sort((a, b) => a - b);
      const n = values.length;
      const q1 = values[Math.floor(n * 0.25)];
      const median = values[Math.floor(n * 0.5)];
      const q3 = values[Math.floor(n * 0.75)];
      const iqr = q3 - q1;
      const whiskerLo = Math.max(values[0], q1 - 1.5 * iqr);
      const whiskerHi = Math.min(values[n - 1], q3 + 1.5 * iqr);
      const outliers = values.filter(v => v < whiskerLo || v > whiskerHi);
      return { label, q1, median, q3, whiskerLo, whiskerHi, outliers, min: values[0], max: values[n - 1] };
    });
  }, [rows, textIdx, numIdx]);
  const allVals = groups.flatMap(g => [g.whiskerLo, g.whiskerHi, ...g.outliers]);
  const scale = niceScale(Math.min(...allVals), Math.max(...allVals));
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const boxW = Math.min(40, plotW / groups.length - 8);
  const yPos = (v) => PAD.top + plotH - ((v - scale.min) / (scale.max - scale.min || 1)) * plotH;
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {scale.ticks.map(t => <g key={t}><line x1={PAD.left} x2={PAD.left + plotW} y1={yPos(t)} y2={yPos(t)} stroke="var(--havn-border)" strokeDasharray="3,3" opacity={0.3} /><text x={PAD.left - 6} y={yPos(t) + 4} textAnchor="end" fill="var(--havn-text-secondary)" fontSize={10}>{fmtAxis(t)}</text></g>)}
      {groups.map((g, i) => {
        const cx = PAD.left + (i + 0.5) * (plotW / groups.length);
        const color = getSeriesColor(config, i, g.label);
        return (
          <g key={i} onMouseEnter={() => setTip({ x: cx, y: yPos(g.median), label: `${g.label}: Q1=${fmtNum(g.q1)} Med=${fmtNum(g.median)} Q3=${fmtNum(g.q3)}` })} onMouseLeave={() => setTip(null)}>
            <line x1={cx} y1={yPos(g.whiskerHi)} x2={cx} y2={yPos(g.whiskerLo)} stroke={color} strokeWidth={1.5} />
            <line x1={cx - boxW/4} y1={yPos(g.whiskerHi)} x2={cx + boxW/4} y2={yPos(g.whiskerHi)} stroke={color} strokeWidth={1.5} />
            <line x1={cx - boxW/4} y1={yPos(g.whiskerLo)} x2={cx + boxW/4} y2={yPos(g.whiskerLo)} stroke={color} strokeWidth={1.5} />
            <rect x={cx - boxW/2} y={yPos(g.q3)} width={boxW} height={Math.max(1, yPos(g.q1) - yPos(g.q3))} fill={color} opacity={0.3} stroke={color} strokeWidth={1.5} rx={2} />
            <line x1={cx - boxW/2} y1={yPos(g.median)} x2={cx + boxW/2} y2={yPos(g.median)} stroke={color} strokeWidth={2.5} />
            {g.outliers.map((o, oi) => <circle key={oi} cx={cx} cy={yPos(o)} r={3} fill={color} opacity={0.6} />)}
            <text x={cx} y={PAD.top + plotH + 16} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={10}>{trunc(g.label, 10)}</text>
          </g>
        );
      })}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true} svgWidth={width} svgHeight={height}>{tip.label}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// COMBO CHART (bar + line, dual Y-axis)
// ---------------------------------------------------------------------------

export function ComboChart({ columns, rows, width, height, config }) {
  const [tip, setTip] = useState(null);
  if (!rows || rows.length < 1 || !columns || columns.length < 3) return <svg width={width} height={height}><text x={width/2} y={height/2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>Need 3+ columns for combo chart</text></svg>;
  const labelIdx = 0;
  const numCols = columns.reduce((acc, _, i) => { if (i > 0) { const s = rows.slice(0, 10).map(r => parseNum(r[i])).filter(v => !isNaN(v)); if (s.length > 0) acc.push(i); } return acc; }, []);
  if (numCols.length < 2) return <svg width={width} height={height}><text x={width/2} y={height/2} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={13}>Need 2+ numeric columns</text></svg>;
  const barCols = config?.comboBarColumns ? numCols.filter(ci => config.comboBarColumns.includes(columns[ci])) : [numCols[0]];
  const lineCols = numCols.filter(ci => !barCols.includes(ci));
  const labels = rows.map(r => String(r[labelIdx] ?? ""));
  const n = labels.length;
  const plotW = width - PAD.left - PAD.right - 50;
  const plotH = height - PAD.top - PAD.bottom;
  const barVals = barCols.flatMap(ci => rows.map(r => parseNum(r[ci]) || 0));
  const lineVals = lineCols.flatMap(ci => rows.map(r => parseNum(r[ci]) || 0));
  const barScale = niceScale(Math.min(0, ...barVals), Math.max(...barVals));
  const lineScale = niceScale(Math.min(...lineVals), Math.max(...lineVals));
  const groupW = plotW / n;
  const barW = Math.min(groupW * 0.6, 32);
  const barYPos = (v) => PAD.top + plotH - ((v - barScale.min) / (barScale.max - barScale.min || 1)) * plotH;
  const lineYPos = (v) => PAD.top + plotH - ((v - lineScale.min) / (lineScale.max - lineScale.min || 1)) * plotH;
  const xPos = (i) => PAD.left + i * groupW + groupW / 2;
  return (
    <svg width={width} height={height} style={{ display: "block" }}>
      {barScale.ticks.map(t => <g key={`bg${t}`}><line x1={PAD.left} x2={PAD.left + plotW} y1={barYPos(t)} y2={barYPos(t)} stroke="var(--havn-border)" strokeDasharray="3,3" opacity={0.2} /><text x={PAD.left - 6} y={barYPos(t) + 4} textAnchor="end" fill="var(--havn-text-secondary)" fontSize={10}>{fmtAxis(t)}</text></g>)}
      {lineScale.ticks.map(t => <text key={`lr${t}`} x={PAD.left + plotW + 8} y={lineYPos(t) + 4} textAnchor="start" fill={getSeriesColor(config, barCols.length, columns[lineCols[0]])} fontSize={10}>{fmtAxis(t)}</text>)}
      {barCols.map((ci, si) => rows.map((r, ri) => {
        const v = parseNum(r[ci]) || 0;
        const h = Math.abs(barYPos(v) - barYPos(0));
        return <rect key={`b${si}-${ri}`} x={xPos(ri) - barW/2} y={Math.min(barYPos(v), barYPos(0))} width={barW} height={Math.max(1, h)} fill={getSeriesColor(config, si, columns[ci])} opacity={0.8} rx={2}
          onMouseEnter={() => setTip({ x: xPos(ri), y: barYPos(v), label: `${labels[ri]}: ${columns[ci]}=${fmtNum(v)}` })} onMouseLeave={() => setTip(null)} />;
      }))}
      {lineCols.map((ci, si) => {
        const d = rows.map((r, ri) => `${ri === 0 ? "M" : "L"} ${xPos(ri)} ${lineYPos(parseNum(r[ci]) || 0)}`).join(" ");
        return <g key={`l${si}`}><path d={d} fill="none" stroke={getSeriesColor(config, barCols.length + si, columns[ci])} strokeWidth={2} strokeLinecap="round" />
          {rows.map((r, ri) => <circle key={ri} cx={xPos(ri)} cy={lineYPos(parseNum(r[ci]) || 0)} r={3} fill={getSeriesColor(config, barCols.length + si, columns[ci])} stroke="var(--havn-bg)" strokeWidth={1.5}
            onMouseEnter={() => setTip({ x: xPos(ri), y: lineYPos(parseNum(r[ci]) || 0), label: `${labels[ri]}: ${columns[ci]}=${fmtNum(parseNum(r[ci]))}` })} onMouseLeave={() => setTip(null)} />)}</g>;
      })}
      {labels.map((l, i) => <text key={i} x={xPos(i)} y={PAD.top + plotH + 16} textAnchor="middle" fill="var(--havn-text-secondary)" fontSize={10}>{trunc(l, 8)}</text>)}
      {tip && <Tooltip x={tip.x} y={tip.y} visible={true} svgWidth={width} svgHeight={height}>{tip.label}</Tooltip>}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Chart dispatcher — picks the right renderer for dashboard chart types
// ---------------------------------------------------------------------------

export default function DashboardChart({ type, columns, rows, width, height, config }) {
  const props = { columns, rows, width, height, config };

  switch (type) {
    case "gauge": return <Gauge {...props} />;
    case "treemap": return <Treemap {...props} />;
    case "heatmap": return <Heatmap {...props} />;
    case "funnel": return <Funnel {...props} />;
    case "waterfall": return <Waterfall {...props} />;
    case "histogram": return <Histogram {...props} />;
    case "radar": return <Radar {...props} />;
    case "bubble": return <Bubble {...props} />;
    case "sparkline": return <Sparkline {...props} />;
    case "progress": return <ProgressBar {...props} />;
    case "bullet": return <Bullet {...props} />;
    case "sankey": return <Sankey {...props} />;
    case "stacked_area": return <StackedArea {...props} />;
    case "boxplot": return <BoxPlot {...props} />;
    case "combo": return <ComboChart {...props} />;
    default:
      return (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--havn-text-secondary)", fontSize: 13 }}>
          Unknown chart type: {type}
        </div>
      );
  }
}

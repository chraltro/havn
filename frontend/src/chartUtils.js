/**
 * Shared chart utilities — extracted from ChartPanel.jsx for reuse by DashboardCharts.
 */

export function niceScale(min, max, maxTicks = 6) {
  if (min === max) { min = min === 0 ? -1 : min * 0.9; max = max === 0 ? 1 : max * 1.1; }
  const range = max - min;
  const rough = range / maxTicks;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  let step;
  if (norm <= 1.5) step = mag;
  else if (norm <= 3) step = 2 * mag;
  else if (norm <= 7) step = 5 * mag;
  else step = 10 * mag;
  const nMin = Math.floor(min / step) * step;
  const nMax = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = nMin; v <= nMax + step * 0.001; v += step) {
    ticks.push(parseFloat(v.toPrecision(12)));
  }
  return { min: nMin, max: nMax, ticks };
}

export function fmtNum(v) {
  if (v === null || v === undefined) return "";
  const n = Number(v);
  if (isNaN(n)) return String(v);
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
  if (Number.isInteger(n)) return n.toLocaleString();
  return n.toFixed(2);
}

export function fmtAxis(v) {
  const n = Number(v);
  if (isNaN(n)) return String(v);
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1e4) return (n / 1e3).toFixed(0) + "K";
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(abs < 1 ? 2 : 1);
}

export function parseNum(v) {
  if (v === null || v === undefined || v === "") return NaN;
  return Number(v);
}

export function trunc(s, max = 12) {
  s = String(s);
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

export function arcPath(cx, cy, r, startAngle, endAngle) {
  const x1 = cx + r * Math.cos(startAngle);
  const y1 = cy + r * Math.sin(startAngle);
  const x2 = cx + r * Math.cos(endAngle);
  const y2 = cy + r * Math.sin(endAngle);
  const large = endAngle - startAngle > Math.PI ? 1 : 0;
  return `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`;
}

export function donutArc(cx, cy, outer, inner, startAngle, endAngle) {
  const cos1 = Math.cos(startAngle), sin1 = Math.sin(startAngle);
  const cos2 = Math.cos(endAngle), sin2 = Math.sin(endAngle);
  const large = endAngle - startAngle > Math.PI ? 1 : 0;
  return [
    `M ${cx + outer * cos1} ${cy + outer * sin1}`,
    `A ${outer} ${outer} 0 ${large} 1 ${cx + outer * cos2} ${cy + outer * sin2}`,
    `L ${cx + inner * cos2} ${cy + inner * sin2}`,
    `A ${inner} ${inner} 0 ${large} 0 ${cx + inner * cos1} ${cy + inner * sin1}`,
    "Z",
  ].join(" ");
}

export function analyzeColumns(columns, rows) {
  return columns.map((name, i) => {
    let numCount = 0, dateCount = 0, nullCount = 0;
    const sampleSize = Math.min(rows.length, 100);
    const unique = new Set();
    for (let r = 0; r < sampleSize; r++) {
      const val = rows[r][i];
      if (val === null || val === undefined || val === "") { nullCount++; continue; }
      unique.add(val);
      const str = String(val).trim();
      if (str === "true" || str === "false") continue;
      if (!isNaN(Number(str))) numCount++;
      else if (/^\d{4}-\d{2}-\d{2}/.test(str)) dateCount++;
    }
    const valid = sampleSize - nullCount;
    const isNumeric = valid > 0 && numCount / valid > 0.7;
    const isTemporal = valid > 0 && dateCount / valid > 0.7;
    return { name, index: i, isNumeric, isTemporal, isText: !isNumeric && !isTemporal, uniqueValues: unique.size };
  });
}

export function detectBestChart(analysis, rowCount) {
  const numeric = analysis.filter((c) => c.isNumeric);
  const temporal = analysis.filter((c) => c.isTemporal);
  const text = analysis.filter((c) => c.isText);

  // Extended detection for dashboard chart types
  if (rowCount === 1 && numeric.length >= 1) return { type: "kpi", x: 0, y: [numeric[0].index] };
  if (numeric.length === 1 && text.length === 0 && rowCount > 5) return { type: "histogram", x: numeric[0].index, y: [numeric[0].index] };

  if (temporal.length >= 1 && numeric.length >= 1) {
    return { type: "line", x: temporal[0].index, y: numeric.slice(0, 4).map((c) => c.index) };
  }
  if (numeric.length >= 2 && text.length === 0) {
    return { type: "scatter", x: numeric[0].index, y: [numeric[1].index] };
  }
  if (text.length >= 1 && numeric.length >= 1) {
    if (rowCount <= 7 && numeric.length === 1) return { type: "donut", x: text[0].index, y: [numeric[0].index] };
    return { type: "bar", x: text[0].index, y: numeric.slice(0, 4).map((c) => c.index) };
  }
  if (numeric.length === 1) return { type: "bar", x: 0, y: [numeric[0].index] };
  return { type: "bar", x: 0, y: [Math.min(1, analysis.length - 1)] };
}

export const COLORS = [
  "#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6",
  "#06b6d4", "#ec4899", "#14b8a6", "#f97316", "#a855f7",
  "#3b82f6", "#10b981", "#eab308", "#f43f5e", "#8b5cf6",
  "#0ea5e9",
];

// Re-export key utilities from chartStyleDefaults
export { getSeriesColor, formatNumber, autoContrast, PALETTES, DEFAULT_CHART_STYLE } from "./chartStyleDefaults";

export const DASHBOARD_CHART_TYPES = [
  // Basic
  { id: "bar", label: "Bar", group: "Basic", desc: "Vertical bars comparing categories. Best for <15 categories. Labels may overlap with more." },
  { id: "line", label: "Line", group: "Basic", desc: "Trends over time or continuous data. Best for time series." },
  { id: "area", label: "Area", group: "Basic", desc: "Like line, with filled area below. Good for showing volume over time." },
  { id: "scatter", label: "Scatter", group: "Basic", desc: "Plot two numeric values as points. Best for correlations." },
  { id: "pie", label: "Pie", group: "Basic", desc: "Show proportions of a whole. Best for <7 categories with balanced values." },
  { id: "donut", label: "Donut", group: "Basic", desc: "Like pie with a center total. Best for <7 categories." },
  { id: "grouped", label: "Grouped", group: "Basic", desc: "Side-by-side bars comparing multiple measures per category." },
  { id: "hbar", label: "H-Bar", group: "Basic", desc: "Horizontal bars — readable labels even with 20+ categories." },
  { id: "stacked", label: "Stacked", group: "Basic", desc: "Bars stacked to show composition of each group." },
  { id: "stacked_area", label: "Stacked Area", group: "Basic", desc: "Stacked filled area chart. Good for part-of-whole trends." },
  { id: "stacked100", label: "100% Stacked", group: "Basic", desc: "Bars normalized to 100% — compare proportions across groups." },
  // Advanced
  { id: "treemap", label: "Treemap", group: "Advanced", desc: "Nested rectangles sized by value. Better than pie for many categories." },
  { id: "heatmap", label: "Heatmap", group: "Advanced", desc: "Color-coded grid of two categories" },
  { id: "funnel", label: "Funnel", group: "Advanced", desc: "Show conversion through stages" },
  { id: "waterfall", label: "Waterfall", group: "Advanced", desc: "Running total with gains/losses" },
  { id: "sankey", label: "Sankey", group: "Advanced", desc: "Flow between source and target" },
  // Statistics
  { id: "histogram", label: "Histogram", group: "Stats", desc: "Distribution of a single numeric column" },
  { id: "bubble", label: "Bubble", group: "Stats", desc: "Scatter with a third size dimension" },
  { id: "radar", label: "Radar", group: "Stats", desc: "Compare multiple metrics on radial axes" },
  { id: "boxplot", label: "Box Plot", group: "Stats", desc: "Distribution with quartiles and outliers" },
  { id: "combo", label: "Combo", group: "Advanced", desc: "Bar + line with dual Y-axis" },
  // Cards & Indicators
  { id: "gauge", label: "Gauge", group: "Cards", desc: "Semicircle meter showing a single value" },
  { id: "progress", label: "Progress", group: "Cards", desc: "Horizontal bar showing completion %" },
  { id: "sparkline", label: "Sparkline", group: "Cards", desc: "Tiny line chart, no axes" },
  { id: "bullet", label: "Bullet", group: "Cards", desc: "Actual vs target with qualitative ranges" },
];

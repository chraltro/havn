/**
 * Shared chart style defaults — palettes, color resolution, number formatting, contrast.
 * Used by ChartPanel, DashboardCharts, and DashboardWidget.
 */

// ═══════════════════════════════════════════════════════════════════
// PALETTES
// ═══════════════════════════════════════════════════════════════════

export const PALETTES = {
  default: [
    "#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6",
    "#06b6d4", "#ec4899", "#14b8a6", "#f97316", "#a855f7",
    "#3b82f6", "#10b981", "#eab308", "#f43f5e", "#8b5cf6",
    "#0ea5e9",
  ],
  colorblind: [
    "#000000", "#E69F00", "#56B4E9", "#009E73",
    "#F0E442", "#0072B2", "#D55E00", "#CC79A7",
  ],
  categorical: [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
  ],
  sequential: [
    "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#4292c6",
    "#2171b5", "#1361a0", "#08519c", "#083d7f", "#08306b",
  ],
  diverging: [
    "#b2182b", "#d6604d", "#f4a582", "#fddbc7", "#f7f7f7",
    "#d1e5f0", "#92c5de", "#4393c3", "#2166ac", "#053061",
  ],
};

// ═══════════════════════════════════════════════════════════════════
// COLOR RESOLUTION
// ═══════════════════════════════════════════════════════════════════

/**
 * Simple deterministic string hash -> index.
 * Maps a category name to a stable palette index.
 */
export function hashStringToIndex(str, size) {
  if (!str || size <= 0) return 0;
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
  }
  return ((hash % size) + size) % size;
}

/**
 * Resolve the color for a given series/slice.
 *
 * Priority:
 *   1. Per-series override in config.seriesColors[seriesName]
 *   2. Palette selected by config.palette (name key into PALETTES)
 *   3. Falls back to PALETTES.default
 *
 * Uses hashStringToIndex for sticky name-based mapping when seriesName
 * is provided; otherwise cycles by index.
 */
export function getSeriesColor(config, index, seriesName) {
  // 1. Per-series override
  if (seriesName && config?.seriesColors?.[seriesName]) {
    return config.seriesColors[seriesName];
  }

  // 2. Select palette
  const paletteName = config?.palette || "default";
  const palette = PALETTES[paletteName] || PALETTES.default;

  // 3. Resolve index — sticky hash when name is available
  if (seriesName) {
    return palette[hashStringToIndex(seriesName, palette.length)];
  }
  return palette[index % palette.length];
}

// ═══════════════════════════════════════════════════════════════════
// NUMBER FORMATTING
// ═══════════════════════════════════════════════════════════════════

/**
 * Format a number with configurable mode.
 *
 * @param {number|string} value
 * @param {"auto"|"plain"|"compact"|"percent"|"currency"} format
 * @param {number|null} decimals  — fixed decimal places (null = auto)
 * @param {string} currency       — ISO 4217 code for "currency" format
 */
export function formatNumber(value, format = "auto", decimals = null, currency = "USD") {
  if (value === null || value === undefined) return "";
  const n = Number(value);
  if (isNaN(n)) return String(value);

  switch (format) {
    case "plain": {
      if (decimals !== null && decimals !== undefined) {
        return n.toLocaleString(undefined, {
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals,
        });
      }
      return n.toLocaleString();
    }
    case "compact": {
      const abs = Math.abs(n);
      const d = decimals ?? 1;
      if (abs >= 1e9) return (n / 1e9).toFixed(d) + "B";
      if (abs >= 1e6) return (n / 1e6).toFixed(d) + "M";
      if (abs >= 1e3) return (n / 1e3).toFixed(d) + "K";
      return decimals !== null ? n.toFixed(decimals) : String(n);
    }
    case "percent": {
      const d = decimals ?? 1;
      return (n * 100).toFixed(d) + "%";
    }
    case "currency": {
      try {
        return new Intl.NumberFormat(undefined, {
          style: "currency",
          currency: currency || "USD",
          minimumFractionDigits: decimals ?? 2,
          maximumFractionDigits: decimals ?? 2,
        }).format(n);
      } catch {
        // Fallback if currency code is invalid
        return n.toFixed(decimals ?? 2);
      }
    }
    case "auto":
    default: {
      const abs = Math.abs(n);
      if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
      if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
      if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
      if (Number.isInteger(n)) return n.toLocaleString();
      return n.toFixed(2);
    }
  }
}

// ═══════════════════════════════════════════════════════════════════
// CONTRAST
// ═══════════════════════════════════════════════════════════════════

/**
 * Return "#000" or "#fff" for readable text on the given hex background.
 * Uses relative luminance (W3C formula).
 */
export function autoContrast(bgColor) {
  if (!bgColor || typeof bgColor !== "string") return "#fff";
  let hex = bgColor.replace("#", "");
  if (hex.length === 3) hex = hex[0] + hex[0] + hex[1] + hex[1] + hex[2] + hex[2];
  if (hex.length !== 6) return "#fff";
  const r = parseInt(hex.slice(0, 2), 16) / 255;
  const g = parseInt(hex.slice(2, 4), 16) / 255;
  const b = parseInt(hex.slice(4, 6), 16) / 255;
  const lum = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const L = 0.2126 * lum(r) + 0.7152 * lum(g) + 0.0722 * lum(b);
  return L > 0.179 ? "#000" : "#fff";
}

// ═══════════════════════════════════════════════════════════════════
// DEFAULT CHART STYLE CONFIG
// ═══════════════════════════════════════════════════════════════════

export const DEFAULT_CHART_STYLE = {
  palette: null,           // palette name (key into PALETTES), null = "default"
  seriesColors: null,      // { [seriesName]: "#hexcolor" } overrides
  bgColor: null,           // SVG background color, null = transparent
  gridlineColor: null,     // gridline stroke color, null = "var(--havn-border-light)"
  gridlineStyle: null,     // "solid" | "dashed" | "dotted", null = "dashed"
  showGridlines: true,     // whether to render gridlines at all
  numberFormat: null,      // "auto" | "plain" | "compact" | "percent" | "currency"
  numberDecimals: null,    // fixed decimals or null for auto
  numberCurrency: null,    // ISO 4217 code for currency format
};

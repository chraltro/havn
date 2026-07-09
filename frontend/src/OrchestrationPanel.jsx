import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { api } from "./api";
import { usePipeline } from "./PipelineContext";

/* =====================================================================
 * OrchestrationPanel — Plan Jobs (with inline row form, cron wizard,
 * DAG picker) + Job Results.
 *
 * NOTE: JSX text cannot contain \uXXXX escape sequences — they render
 * literally. For special characters, use actual UTF-8 chars or wrap in
 * {'\uXXXX'} expressions. We use literal chars throughout.
 * ================================================================= */

/* ------------------------------------------------------------------ */
/* Formatters                                                          */
/* ------------------------------------------------------------------ */

const EM_DASH = "\u2014";
const MID_DOT = "\u00B7";

function timeAgo(dateStr) {
  if (!dateStr) return "";
  const now = Date.now();
  const then = new Date(dateStr).getTime();
  const diffMs = now - then;
  if (isNaN(diffMs) || diffMs < 0) return "just now";
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function formatDuration(ms) {
  if (ms == null) return EM_DASH;
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const min = Math.floor(ms / 60000);
  const sec = Math.floor((ms % 60000) / 1000);
  return `${min}m ${sec}s`;
}

function formatAbsolute(dateStr) {
  if (!dateStr) return "";
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function formatIn(dateStr) {
  if (!dateStr) return "";
  const then = new Date(dateStr).getTime();
  if (isNaN(then)) return "";
  const diff = then - Date.now();
  if (diff < 0) return "now";
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "< 1m";
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `in ${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `in ${days}d ${hours % 24}h`;
}

/* ------------------------------------------------------------------ */
/* Status badges                                                       */
/* ------------------------------------------------------------------ */

function statusStyle(status) {
  if (status === "success")
    return { background: "color-mix(in srgb, var(--havn-green) 15%, transparent)", color: "var(--havn-green)" };
  if (status === "failure" || status === "error")
    return { background: "color-mix(in srgb, var(--havn-red) 15%, transparent)", color: "var(--havn-red)" };
  if (status === "running")
    return { background: "color-mix(in srgb, var(--havn-accent) 15%, transparent)", color: "var(--havn-accent)" };
  if (status === "timeout")
    return { background: "color-mix(in srgb, orange 15%, transparent)", color: "orange" };
  if (status === "cancelled")
    return { background: "color-mix(in srgb, var(--havn-text-dim) 15%, transparent)", color: "var(--havn-text-dim)" };
  return { background: "color-mix(in srgb, var(--havn-text-dim) 12%, transparent)", color: "var(--havn-text-secondary)" };
}

function StatusBadge({ status }) {
  return <span style={{ ...s.chip, ...statusStyle(status) }}>{status}</span>;
}

const TYPE_BADGE_CFG = {
  ingest:    { bg: "color-mix(in srgb, cyan 18%, transparent)",                color: "cyan",               label: "ING" },
  transform: { bg: "color-mix(in srgb, var(--havn-accent) 18%, transparent)",  color: "var(--havn-accent)", label: "TRF" },
  export:    { bg: "color-mix(in srgb, orchid 18%, transparent)",              color: "orchid",             label: "EXP" },
};

function TypeBadge({ type }) {
  const cfg = TYPE_BADGE_CFG[type] || { bg: "transparent", color: "var(--havn-text-secondary)", label: (type || "?").toUpperCase() };
  return <span style={{ ...s.chip, background: cfg.bg, color: cfg.color }}>{cfg.label}</span>;
}

/* ------------------------------------------------------------------ */
/* Cron engine (client-side matching + description + next runs)        */
/* ------------------------------------------------------------------ */

// POSIX cron weekday: 0=Sun, 1=Mon, ..., 6=Sat.
const WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const WEEKDAY_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];
const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function ordinal(n) {
  const suf = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (suf[(v - 20) % 10] || suf[v] || suf[0]);
}

// Interval schedules ("every 2 weeks", "every 30 minutes", etc.) — an
// alternative to cron that expresses elapsed-time intervals. Standard
// 5-field cron can't express "every 2 weeks" or "every 3 days from now"
// because it's stateless, so the backend scheduler tracks last-fire per
// interval schedule and triggers on elapsed time.
const INTERVAL_RE = /^\s*every\s+(\d+)\s+(minute|hour|day|week|month|year)s?\s*$/i;

function isIntervalSchedule(expr) {
  return INTERVAL_RE.test(expr || "");
}

function parseIntervalSchedule(expr) {
  const m = INTERVAL_RE.exec(expr || "");
  if (!m) return null;
  const n = parseInt(m[1], 10);
  if (!isFinite(n) || n <= 0) return null;
  const unit = m[2].toLowerCase();
  const UNIT_SEC = {
    minute: 60, hour: 3600, day: 86400,
    week: 86400 * 7, month: 86400 * 30, year: 86400 * 365,
  };
  return { n, unit, seconds: n * UNIT_SEC[unit] };
}

function describeInterval(expr) {
  const p = parseIntervalSchedule(expr);
  if (!p) return null;
  return `Every ${p.n} ${p.unit}${p.n === 1 ? "" : "s"}`;
}

// A schedule expression is valid if it's either a real cron OR an interval.
function isValidSchedule(expr) {
  if (!expr || !expr.trim()) return false;
  if (isIntervalSchedule(expr)) return parseIntervalSchedule(expr) != null;
  return parseCron(expr) != null;
}

// Next N runs for a schedule expression. Works for both cron and interval.
function nextRunsForSchedule(expr, count = 5, lastFireIso = null) {
  if (isIntervalSchedule(expr)) {
    const p = parseIntervalSchedule(expr);
    if (!p) return [];
    const base = lastFireIso ? new Date(lastFireIso) : new Date();
    if (isNaN(base.getTime())) return [];
    const out = [];
    for (let i = 1; i <= count; i++) {
      out.push(new Date(base.getTime() + p.seconds * 1000 * i));
    }
    return out;
  }
  return nextRuns(expr, count);
}

// Parse a single cron field into a set of allowed numeric values.
// Returns Set<number> or null if invalid.
function fieldAllowed(field, minVal, maxVal) {
  if (field === "*" || field === "?") {
    const s = new Set();
    for (let i = minVal; i <= maxVal; i++) s.add(i);
    return s;
  }
  const set = new Set();
  const parts = String(field).split(",");
  for (const part of parts) {
    if (!part) return null;
    let base = part;
    let step = 1;
    if (base.includes("/")) {
      const [b, st] = base.split("/");
      base = b;
      step = parseInt(st, 10);
      if (!isFinite(step) || step <= 0) return null;
    }
    let lo, hi;
    if (base === "*") {
      lo = minVal; hi = maxVal;
    } else if (base.includes("-")) {
      const [l, h] = base.split("-");
      lo = parseInt(l, 10);
      hi = parseInt(h, 10);
    } else {
      lo = parseInt(base, 10);
      hi = lo;
    }
    if (!isFinite(lo) || !isFinite(hi)) return null;
    if (lo < minVal || hi > maxVal || lo > hi) return null;
    for (let v = lo; v <= hi; v += step) set.add(v);
  }
  return set;
}

function parseCron(expr) {
  if (!expr || !expr.trim()) return null;
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const min = fieldAllowed(parts[0], 0, 59);
  const hour = fieldAllowed(parts[1], 0, 23);
  const dom = fieldAllowed(parts[2], 1, 31);
  const mon = fieldAllowed(parts[3], 1, 12);
  const dow = fieldAllowed(parts[4], 0, 6);
  if (!min || !hour || !dom || !mon || !dow) return null;
  return { min, hour, dom, mon, dow, parts };
}

function matchesCron(parsed, date) {
  if (!parsed) return false;
  // POSIX weekday: 0=Sun..6=Sat. JS getDay() is also 0=Sun..6=Sat.
  if (!parsed.min.has(date.getMinutes())) return false;
  if (!parsed.hour.has(date.getHours())) return false;
  if (!parsed.mon.has(date.getMonth() + 1)) return false;
  // Cron day-of-month and day-of-week: when BOTH are restricted (not `*`),
  // the match is OR (either satisfies). When one is `*`, it's AND with the other.
  const domPart = parsed.parts[2];
  const dowPart = parsed.parts[4];
  const domMatch = parsed.dom.has(date.getDate());
  const dowMatch = parsed.dow.has(date.getDay());
  const domRestricted = domPart !== "*" && domPart !== "?";
  const dowRestricted = dowPart !== "*" && dowPart !== "?";
  if (domRestricted && dowRestricted) {
    if (!domMatch && !dowMatch) return false;
  } else {
    if (domRestricted && !domMatch) return false;
    if (dowRestricted && !dowMatch) return false;
  }
  return true;
}

function nextRuns(expr, count = 5, maxMinutes = 60 * 24 * 400) {
  const parsed = parseCron(expr);
  if (!parsed) return [];
  const results = [];
  const candidate = new Date();
  candidate.setSeconds(0, 0);
  candidate.setMinutes(candidate.getMinutes() + 1);
  for (let i = 0; i < maxMinutes && results.length < count; i++) {
    if (matchesCron(parsed, candidate)) {
      results.push(new Date(candidate.getTime()));
    }
    candidate.setMinutes(candidate.getMinutes() + 1);
  }
  return results;
}

// Human-readable description of a cron or interval expression. Handles
// common cases; falls back to the raw expression for anything it can't
// summarize.
function describeCron(expr) {
  if (!expr || !expr.trim()) return "No schedule \u2014 on-demand only";
  // Interval schedules route to their own describer
  if (isIntervalSchedule(expr)) {
    const desc = describeInterval(expr);
    return desc || "Invalid interval expression";
  }
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return "Invalid schedule (expected cron 5-field or 'every N unit')";
  if (!parseCron(expr)) return "Invalid cron expression";

  const [m, h, dom, mon, dow] = parts;

  const minAsInt = /^\d+$/.test(m) ? parseInt(m, 10) : null;
  const hourAsInt = /^\d+$/.test(h) ? parseInt(h, 10) : null;

  const timeStr = () => {
    if (minAsInt == null || hourAsInt == null) return null;
    const ampm = hourAsInt < 12 ? "AM" : "PM";
    const h12 = hourAsInt === 0 ? 12 : hourAsInt > 12 ? hourAsInt - 12 : hourAsInt;
    return `${h12}:${String(minAsInt).padStart(2, "0")} ${ampm}`;
  };

  const everyN = (field) => {
    const match = /^\*\/(\d+)$/.exec(field);
    return match ? parseInt(match[1], 10) : null;
  };

  // Pure interval cases
  if (dom === "*" && mon === "*" && dow === "*") {
    // Every N minutes
    const stepMin = everyN(m);
    if (stepMin && h === "*") return `Every ${stepMin} minute${stepMin === 1 ? "" : "s"}`;
    // Every minute
    if (m === "*" && h === "*") return "Every minute";
    // Every hour on the hour (minute=0)
    if (m === "0" && h === "*") return "Every hour";
    // Every N hours at minute M
    const stepH = everyN(h);
    if (stepH && minAsInt != null) {
      return `Every ${stepH} hour${stepH === 1 ? "" : "s"} at minute ${minAsInt}`;
    }
    // Specific time every day
    if (minAsInt != null && hourAsInt != null) {
      return `Every day at ${timeStr()}`;
    }
  }

  // Weekday patterns
  if (dom === "*" && mon === "*" && dow !== "*") {
    const t = timeStr();
    const dows = dow.split(",").map((x) => parseInt(x, 10)).filter((n) => !isNaN(n));
    if (dow === "1-5") return t ? `Every weekday at ${t}` : `Every weekday`;
    if (dow === "0,6" || dow === "6,0") return t ? `Every weekend at ${t}` : `Every weekend`;
    if (dows.length === 1) {
      return t
        ? `Every ${WEEKDAY_NAMES[dows[0]]} at ${t}`
        : `Every ${WEEKDAY_NAMES[dows[0]]}`;
    }
    if (dows.length > 1) {
      const list = dows.map((d) => WEEKDAY_SHORT[d]).join(", ");
      return t ? `Every ${list} at ${t}` : `Every ${list}`;
    }
  }

  // Specific day-of-month
  if (dom !== "*" && dow === "*") {
    const t = timeStr();
    if (/^\d+$/.test(dom)) {
      const dayNum = parseInt(dom, 10);
      if (mon === "*") {
        return t
          ? `On the ${ordinal(dayNum)} of every month at ${t}`
          : `On the ${ordinal(dayNum)} of every month`;
      }
      if (/^\d+$/.test(mon)) {
        return t
          ? `On ${MONTH_NAMES[parseInt(mon, 10) - 1]} ${ordinal(dayNum)} at ${t}`
          : `On ${MONTH_NAMES[parseInt(mon, 10) - 1]} ${ordinal(dayNum)}`;
      }
    }
  }

  // Nth-weekday-of-month trick: dom is a 7-day range + specific dow
  if (/^\d+-\d+$/.test(dom) && /^\d+$/.test(dow)) {
    const [lo, hi] = dom.split("-").map((x) => parseInt(x, 10));
    if (hi - lo === 6) {
      const weekNum = Math.ceil(lo / 7); // 1=1st week, 2=2nd, etc.
      const dayName = WEEKDAY_NAMES[parseInt(dow, 10)];
      const t = timeStr();
      const monPhrase = mon === "*" ? "every month" : /^\d+$/.test(mon) ? MONTH_NAMES[parseInt(mon, 10) - 1] : `month(s) ${mon}`;
      const ord = ["first", "second", "third", "fourth"][weekNum - 1] || `${weekNum}th`;
      return t
        ? `On the ${ord} ${dayName} of ${monPhrase} at ${t}`
        : `On the ${ord} ${dayName} of ${monPhrase}`;
    }
  }

  // Fallback: spelled-out field-by-field
  const fieldDesc = (field, unit, names) => {
    if (field === "*") return `every ${unit}`;
    if (/^\*\/\d+$/.test(field)) return `every ${field.slice(2)} ${unit}s`;
    if (/^\d+$/.test(field)) {
      const n = parseInt(field, 10);
      return names ? names[n] || String(n) : String(n);
    }
    return field;
  };
  const minDesc = fieldDesc(m, "minute");
  const hourDesc = fieldDesc(h, "hour");
  return `Runs at ${minDesc} of ${hourDesc}, day ${dom}, month ${mon}, weekday ${dow}`;
}

// Build a cron field string from a mode + values (used by the wizard).
function buildField(mode, values, range) {
  if (mode === "every") return "*";
  if (mode === "every-n") {
    const n = parseInt(values?.n, 10);
    return isFinite(n) && n > 0 ? `*/${n}` : "*";
  }
  if (mode === "specific") {
    const list = (values?.specific || []).filter((v) => v !== "" && v != null);
    if (list.length === 0) return "*";
    return list.sort((a, b) => Number(a) - Number(b)).join(",");
  }
  if (mode === "range") {
    if (range?.from == null || range?.to == null) return "*";
    return `${range.from}-${range.to}`;
  }
  if (mode === "raw") return values?.raw || "*";
  return "*";
}

// Detect which mode a raw cron field is in (for initial load from existing cron).
function detectFieldMode(field) {
  if (!field || field === "*") return { mode: "every" };
  if (/^\*\/\d+$/.test(field)) return { mode: "every-n", n: parseInt(field.slice(2), 10) };
  if (/^\d+-\d+$/.test(field)) {
    const [from, to] = field.split("-").map((x) => parseInt(x, 10));
    return { mode: "range", from, to };
  }
  if (/^(\d+)(,\d+)*$/.test(field)) {
    return { mode: "specific", specific: field.split(",").map((x) => parseInt(x, 10)) };
  }
  return { mode: "raw", raw: field };
}

/* ------------------------------------------------------------------ */
/* Cron Wizard Modal                                                   */
/* ------------------------------------------------------------------ */

const CRON_TABS = [
  { id: "common",   label: "Common" },
  { id: "interval", label: "Every N" },
  { id: "minute",   label: "Minutes" },
  { id: "hour",     label: "Hours" },
  { id: "dom",      label: "Day of Month" },
  { id: "month",    label: "Month" },
  { id: "dow",      label: "Day of Week" },
  { id: "advanced", label: "Advanced" },
];

const COMMON_PRESETS = [
  { label: "Every minute",                          cron: "* * * * *" },
  { label: "Every 5 minutes",                       cron: "*/5 * * * *" },
  { label: "Every 15 minutes",                      cron: "*/15 * * * *" },
  { label: "Every 30 minutes",                      cron: "*/30 * * * *" },
  { label: "Every hour (on the hour)",              cron: "0 * * * *" },
  { label: "Every 2 hours",                         cron: "0 */2 * * *" },
  { label: "Every 6 hours",                         cron: "0 */6 * * *" },
  { label: "Every day at midnight",                 cron: "0 0 * * *" },
  { label: "Every day at 6:00 AM",                  cron: "0 6 * * *" },
  { label: "Every weekday at 9:00 AM (Mon–Fri)",    cron: "0 9 * * 1-5" },
  { label: "Every Monday at 9:00 AM",               cron: "0 9 * * 1" },
  { label: "Every Friday at 5:00 PM",               cron: "0 17 * * 5" },
  { label: "Every Saturday at midnight",            cron: "0 0 * * 6" },
  { label: "1st of every month at midnight",        cron: "0 0 1 * *" },
  { label: "15th of every month at midnight",       cron: "0 0 15 * *" },
  { label: "Last day of month (28th fallback)",     cron: "0 0 28 * *" },
  { label: "1st of January at midnight (yearly)",   cron: "0 0 1 1 *" },
];

function CronWizardModal({ initial, onCancel, onSave }) {
  const initialIsInterval = isIntervalSchedule(initial || "");
  // `parts` holds the 5-field cron state; `intervalExpr` holds the interval
  // string when the wizard is in interval mode. Exactly one of them is
  // "active" depending on the active tab / mode.
  const [parts, setParts] = useState(() => {
    if (initialIsInterval) return ["*", "*", "*", "*", "*"];
    const t = (initial || "").trim().split(/\s+/);
    if (t.length === 5) return t;
    return ["*", "*", "*", "*", "*"];
  });
  const [intervalExpr, setIntervalExpr] = useState(() => initialIsInterval ? initial : "");
  const [activeTab, setActiveTab] = useState(initialIsInterval ? "interval" : "common");

  // Close on Escape, matching the backdrop-click dismiss.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Final expression: interval mode wins whenever intervalExpr is set
  const expr = intervalExpr || parts.join(" ");
  const description = useMemo(() => describeCron(expr), [expr]);
  const upcoming = useMemo(() => nextRunsForSchedule(expr, 5), [expr]);

  const setField = (idx, value) => {
    setIntervalExpr("");  // leaving interval mode
    setParts((p) => {
      const next = [...p];
      next[idx] = value;
      return next;
    });
  };

  // Wrapper that clears interval mode when any cron-based tab writes
  const setPartsAndClearInterval = (next) => {
    setIntervalExpr("");
    setParts(typeof next === "function" ? next : next);
  };

  return (
    <div style={s.modalOverlay} onClick={onCancel}>
      <div style={s.modalCard} onClick={(e) => e.stopPropagation()}>
        <div style={s.modalHeader}>
          <div style={{ fontSize: 15, fontWeight: 600 }}>Cron Schedule Wizard</div>
          <button style={s.btn} onClick={onCancel}>Cancel</button>
        </div>

        {/* Tab bar */}
        <div style={s.cronTabs}>
          {CRON_TABS.map((t) => (
            <button
              key={t.id}
              style={{ ...s.cronTab, ...(activeTab === t.id ? s.cronTabActive : {}) }}
              onClick={() => setActiveTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div style={s.cronBody}>
          {activeTab === "common"   && <CommonTab    parts={parts} setParts={setPartsAndClearInterval} />}
          {activeTab === "interval" && <IntervalTab  intervalExpr={intervalExpr} setIntervalExpr={setIntervalExpr} setParts={setPartsAndClearInterval} />}
          {activeTab === "minute"   && <FieldTab     idx={0} label="Minutes"      min={0} max={59} parts={parts} setField={setField} />}
          {activeTab === "hour"     && <FieldTab     idx={1} label="Hours"        min={0} max={23} parts={parts} setField={setField} />}
          {activeTab === "dom"      && <DomTab       parts={parts} setParts={setPartsAndClearInterval} />}
          {activeTab === "month"    && <MonthTab     parts={parts} setField={setField} />}
          {activeTab === "dow"      && <DowTab       parts={parts} setField={setField} />}
          {activeTab === "advanced" && <AdvancedTab  parts={parts} setParts={setPartsAndClearInterval} intervalExpr={intervalExpr} setIntervalExpr={setIntervalExpr} />}
        </div>

        {/* Preview footer */}
        <div style={s.cronPreview}>
          <div style={s.cronExprBox}>
            <code style={s.cronExprCode}>{expr}</code>
            <span style={s.cronExprDesc}>{description}</span>
          </div>
          <div style={s.cronNextRuns}>
            <div style={{ fontSize: 11, textTransform: "uppercase", color: "var(--havn-text-dim)", letterSpacing: "0.3px", marginBottom: 4 }}>
              Next 5 runs
            </div>
            {upcoming.length === 0 ? (
              <div style={{ fontSize: 12, color: "var(--havn-text-dim)" }}>
                No upcoming runs match this expression
              </div>
            ) : (
              <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                {upcoming.map((d, i) => (
                  <li key={i} style={{ fontSize: 12, color: "var(--havn-text)", padding: "2px 0" }}>
                    {formatAbsolute(d.toISOString())} {MID_DOT}{" "}
                    <span style={{ color: "var(--havn-text-dim)" }}>{formatIn(d.toISOString())}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div style={s.modalActions}>
          <button style={s.btn} onClick={onCancel}>Cancel</button>
          <button
            style={s.btnPrimary}
            onClick={() => onSave(expr)}
            disabled={!parseCron(expr)}
          >
            Use this schedule
          </button>
        </div>
      </div>
    </div>
  );
}

/* Common presets tab ------------------------------------------------- */
function CommonTab({ parts, setParts }) {
  const currentExpr = parts.join(" ");
  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 12, color: "var(--havn-text-dim)", marginBottom: 10 }}>
        Pick a quick preset, then switch to any other tab to fine-tune.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
        {COMMON_PRESETS.map((p) => {
          const active = currentExpr === p.cron;
          return (
            <button
              key={p.cron}
              style={{
                ...s.btn,
                textAlign: "left",
                padding: "8px 10px",
                fontSize: 12,
                background: active ? "color-mix(in srgb, var(--havn-accent) 15%, transparent)" : "var(--havn-btn-bg)",
                borderColor: active ? "var(--havn-accent)" : "var(--havn-btn-border)",
                color: active ? "var(--havn-accent)" : "var(--havn-text)",
              }}
              onClick={() => setParts(p.cron.split(/\s+/))}
            >
              <div style={{ fontWeight: 500 }}>{p.label}</div>
              <code style={{ fontSize: 10, color: "var(--havn-text-dim)" }}>{p.cron}</code>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* Interval tab ------------------------------------------------------- */
function IntervalTab({ intervalExpr, setIntervalExpr, setParts }) {
  // Seed from existing interval expression if any
  const parsed = parseIntervalSchedule(intervalExpr);
  const [n, setN] = useState(parsed ? parsed.n : 15);
  const [unit, setUnit] = useState(parsed ? parsed.unit : "minute");

  const apply = () => {
    const num = Math.max(1, parseInt(n, 10) || 1);
    // Minute/hour intervals can be expressed cleanly in cron (when N divides
    // 60 or 24), but for consistency and flexibility across ALL units we
    // always emit the interval form. The scheduler handles both.
    const pluralUnit = num === 1 ? unit : `${unit}s`;
    const expr = `every ${num} ${pluralUnit}`;
    setIntervalExpr(expr);
  };

  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 12, color: "var(--havn-text-dim)", marginBottom: 10 }}>
        Run the job at a regular interval. Unlike cron, interval schedules
        track the last fire time so "every 2 weeks" means literally 14 days
        after the previous run.
      </div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <span style={{ fontSize: 13 }}>Every</span>
        <input
          type="number"
          min="1"
          value={n}
          onChange={(e) => setN(e.target.value)}
          style={{ ...s.input, width: 80 }}
        />
        <select
          value={unit}
          onChange={(e) => setUnit(e.target.value)}
          style={{ ...s.input, width: 140 }}
        >
          <option value="minute">minute(s)</option>
          <option value="hour">hour(s)</option>
          <option value="day">day(s)</option>
          <option value="week">week(s)</option>
          <option value="month">month(s) (~30 days)</option>
          <option value="year">year(s) (~365 days)</option>
        </select>
        <button style={s.btnPrimary} onClick={apply}>Apply</button>
      </div>
      <div style={{ fontSize: 11, color: "var(--havn-text-dim)", lineHeight: 1.5, padding: "8px 10px", background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border-light)", borderRadius: 4 }}>
        <strong>Interval presets:</strong>{" "}
        {[
          { label: "every 15 min", n: 15, unit: "minute" },
          { label: "every 30 min", n: 30, unit: "minute" },
          { label: "every 1 hour", n: 1, unit: "hour" },
          { label: "every 6 hours", n: 6, unit: "hour" },
          { label: "every 1 day", n: 1, unit: "day" },
          { label: "every 3 days", n: 3, unit: "day" },
          { label: "every 1 week", n: 1, unit: "week" },
          { label: "every 2 weeks", n: 2, unit: "week" },
          { label: "every 1 month", n: 1, unit: "month" },
        ].map((p, i) => (
          <button
            key={i}
            type="button"
            style={{
              ...s.btn,
              fontSize: 10,
              padding: "2px 8px",
              marginRight: 4,
              marginTop: 4,
            }}
            onClick={() => {
              setN(p.n);
              setUnit(p.unit);
              setIntervalExpr(`every ${p.n} ${p.n === 1 ? p.unit : `${p.unit}s`}`);
            }}
          >
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}

/* Generic per-field tab (minutes & hours) ---------------------------- */
function FieldTab({ idx, label, min, max, parts, setField }) {
  const initial = useMemo(() => detectFieldMode(parts[idx]), [parts, idx]);
  const [mode, setMode] = useState(initial.mode);
  const [everyN, setEveryN] = useState(initial.n || (idx === 0 ? 5 : 1));
  const [specific, setSpecific] = useState(initial.specific || []);
  const [rangeFrom, setRangeFrom] = useState(initial.from ?? min);
  const [rangeTo, setRangeTo] = useState(initial.to ?? max);

  // Inputs keep the raw string while typing (so clearing the field doesn't
  // snap to a default mid-edit) and clamp on blur; the cron field is always
  // built from the clamped value so an empty/invalid input can't emit NaN.
  const clampInt = (value, lo, hi, fallback) => {
    const num = parseInt(value, 10);
    if (Number.isNaN(num)) return fallback;
    return Math.min(hi, Math.max(lo, num));
  };

  useEffect(() => {
    // Rebuild field when any mode control changes
    const next = buildField(
      mode,
      { n: clampInt(everyN, 1, max, 1), specific },
      { from: clampInt(rangeFrom, min, max, min), to: clampInt(rangeTo, min, max, max) },
    );
    setField(idx, next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, everyN, specific, rangeFrom, rangeTo]);

  const toggle = (v) => {
    setSpecific((prev) => (prev.includes(v) ? prev.filter((x) => x !== v) : [...prev, v]));
  };

  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>{label}</div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
        {[
          { id: "every",    label: `Every ${label.toLowerCase()}` },
          { id: "every-n",  label: "Every N" },
          { id: "specific", label: "Specific values" },
          { id: "range",    label: "Range (from-to)" },
        ].map((m) => (
          <button
            key={m.id}
            style={{
              ...s.btn,
              background: mode === m.id ? "color-mix(in srgb, var(--havn-accent) 15%, transparent)" : undefined,
              borderColor: mode === m.id ? "var(--havn-accent)" : undefined,
              color: mode === m.id ? "var(--havn-accent)" : undefined,
            }}
            onClick={() => setMode(m.id)}
          >
            {m.label}
          </button>
        ))}
      </div>

      {mode === "every" && (
        <div style={{ fontSize: 12, color: "var(--havn-text-dim)" }}>
          Fires at every {label.toLowerCase().slice(0, -1)} value ({min}{"\u2013"}{max}).
        </div>
      )}
      {mode === "every-n" && (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 13 }}>Every</span>
          <input
            type="number"
            min="1"
            max={max}
            value={everyN}
            onChange={(e) => setEveryN(e.target.value)}
            onBlur={() => setEveryN(clampInt(everyN, 1, max, 1))}
            style={{ ...s.input, width: 80 }}
          />
          <span style={{ fontSize: 13, color: "var(--havn-text-dim)" }}>
            {label.toLowerCase()} starting at {label === "Minutes" ? ":00" : "00:00"}
          </span>
        </div>
      )}
      {mode === "specific" && (
        <div>
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 6 }}>
            Click to toggle individual values
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(12, 1fr)", gap: 4 }}>
            {Array.from({ length: max - min + 1 }, (_, i) => i + min).map((v) => {
              const active = specific.includes(v);
              return (
                <button
                  key={v}
                  type="button"
                  style={{
                    padding: "4px 0",
                    background: active ? "var(--havn-accent)" : "var(--havn-bg-tertiary)",
                    color: active ? "#fff" : "var(--havn-text)",
                    border: "1px solid var(--havn-border-light)",
                    borderRadius: 4,
                    cursor: "pointer",
                    fontSize: 11,
                  }}
                  onClick={() => toggle(v)}
                >
                  {v}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {mode === "range" && (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 13 }}>From</span>
          <input
            type="number" min={min} max={max}
            value={rangeFrom}
            onChange={(e) => setRangeFrom(e.target.value)}
            onBlur={() => setRangeFrom(clampInt(rangeFrom, min, max, min))}
            style={{ ...s.input, width: 70 }}
          />
          <span style={{ fontSize: 13 }}>to</span>
          <input
            type="number" min={min} max={max}
            value={rangeTo}
            onChange={(e) => setRangeTo(e.target.value)}
            onBlur={() => setRangeTo(clampInt(rangeTo, min, max, max))}
            style={{ ...s.input, width: 70 }}
          />
        </div>
      )}
    </div>
  );
}

/* Day of month tab with nth-weekday helper --------------------------- */
function DomTab({ parts, setParts }) {
  const [mode, setMode] = useState("every");
  const [nth, setNth] = useState(2);
  const [nthDow, setNthDow] = useState(5); // Friday
  const [specific, setSpecific] = useState([1]);

  const apply = (newMode) => {
    setMode(newMode);
    const next = [...parts];
    if (newMode === "every") {
      next[2] = "*";
      setParts(next);
    } else if (newMode === "first") {
      next[2] = "1";
      setParts(next);
    } else if (newMode === "last") {
      // Closest standard approximation: 28th. Comment documents the caveat.
      next[2] = "28";
      setParts(next);
    }
  };

  const applySpecific = () => {
    const next = [...parts];
    next[2] = specific.length > 0 ? specific.sort((a, b) => a - b).join(",") : "*";
    setParts(next);
  };

  const applyNth = () => {
    // Nth weekday of month = day-of-month range for exactly one week, combined
    // with a specific day-of-week. Week 1 = days 1-7, week 2 = 8-14, etc.
    const weekStart = (nth - 1) * 7 + 1;
    const weekEnd = weekStart + 6;
    const next = [...parts];
    next[2] = `${weekStart}-${weekEnd}`;
    next[4] = String(nthDow);
    setParts(next);
  };

  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>Day of Month</div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 12 }}>
        {[
          { id: "every",    label: "Every day" },
          { id: "specific", label: "Specific days" },
          { id: "first",    label: "1st of month" },
          { id: "last",     label: "Last day (28th)" },
          { id: "nth",      label: "Nth weekday of month" },
        ].map((m) => (
          <button
            key={m.id}
            style={{
              ...s.btn,
              background: mode === m.id ? "color-mix(in srgb, var(--havn-accent) 15%, transparent)" : undefined,
              borderColor: mode === m.id ? "var(--havn-accent)" : undefined,
              color: mode === m.id ? "var(--havn-accent)" : undefined,
            }}
            onClick={() => apply(m.id)}
          >
            {m.label}
          </button>
        ))}
      </div>
      {mode === "specific" && (
        <div>
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 6 }}>
            Pick specific dates, then click Apply.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(10, 1fr)", gap: 4, marginBottom: 8 }}>
            {Array.from({ length: 31 }, (_, i) => i + 1).map((v) => {
              const active = specific.includes(v);
              return (
                <button
                  key={v}
                  type="button"
                  style={{
                    padding: "4px 0",
                    background: active ? "var(--havn-accent)" : "var(--havn-bg-tertiary)",
                    color: active ? "#fff" : "var(--havn-text)",
                    border: "1px solid var(--havn-border-light)",
                    borderRadius: 4,
                    cursor: "pointer",
                    fontSize: 11,
                  }}
                  onClick={() => {
                    setSpecific((prev) => prev.includes(v) ? prev.filter((x) => x !== v) : [...prev, v]);
                  }}
                >
                  {v}
                </button>
              );
            })}
          </div>
          <button style={s.btnPrimary} onClick={applySpecific}>Apply</button>
        </div>
      )}
      {mode === "nth" && (
        <div>
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 8 }}>
            Standard 5-field cron doesn't have a native "nth weekday" token. This
            uses the day-range trick: day 1-7 is the 1st week, 8-14 the 2nd, etc.
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 13 }}>The</span>
            <select value={nth} onChange={(e) => setNth(parseInt(e.target.value, 10))} style={{ ...s.input, width: 90 }}>
              <option value={1}>1st</option>
              <option value={2}>2nd</option>
              <option value={3}>3rd</option>
              <option value={4}>4th</option>
            </select>
            <select value={nthDow} onChange={(e) => setNthDow(parseInt(e.target.value, 10))} style={{ ...s.input, width: 120 }}>
              {WEEKDAY_NAMES.map((name, i) => <option key={i} value={i}>{name}</option>)}
            </select>
            <span style={{ fontSize: 13, color: "var(--havn-text-dim)" }}>of the month</span>
            <button style={s.btnPrimary} onClick={applyNth}>Apply</button>
          </div>
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 8 }}>
            Tip: to schedule "every 2nd Friday of March", apply this and then go to
            the Month tab to restrict to March only.
          </div>
        </div>
      )}
    </div>
  );
}

/* Month tab ---------------------------------------------------------- */
function MonthTab({ parts, setField }) {
  const current = parts[3];
  const isAll = current === "*";
  const selected = isAll ? [] : current.split(",").map((x) => parseInt(x, 10)).filter((n) => !isNaN(n));

  const toggle = (m) => {
    const set = new Set(selected);
    if (set.has(m)) set.delete(m);
    else set.add(m);
    if (set.size === 0 || set.size === 12) {
      setField(3, "*");
    } else {
      setField(3, [...set].sort((a, b) => a - b).join(","));
    }
  };

  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>Month</div>
      <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 8 }}>
        Click months to restrict. Leaving none selected means every month.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(6, 1fr)", gap: 6 }}>
        {MONTH_NAMES.map((name, i) => {
          const m = i + 1;
          const active = selected.includes(m);
          return (
            <button
              key={m}
              type="button"
              style={{
                padding: "8px 0",
                background: active ? "var(--havn-accent)" : "var(--havn-bg-tertiary)",
                color: active ? "#fff" : "var(--havn-text)",
                border: "1px solid var(--havn-border-light)",
                borderRadius: 4,
                cursor: "pointer",
                fontSize: 12,
              }}
              onClick={() => toggle(m)}
            >
              {MONTH_SHORT[i]}
            </button>
          );
        })}
      </div>
      <div style={{ marginTop: 10 }}>
        <button style={s.btn} onClick={() => setField(3, "*")}>Every month</button>
      </div>
    </div>
  );
}

/* Day of week tab ---------------------------------------------------- */
function DowTab({ parts, setField }) {
  const current = parts[4];
  const selected = current === "*"
    ? []
    : current === "1-5"
      ? [1, 2, 3, 4, 5]
      : current === "0,6" || current === "6,0"
        ? [0, 6]
        : current.split(",").map((x) => parseInt(x, 10)).filter((n) => !isNaN(n));

  const toggle = (d) => {
    const set = new Set(selected);
    if (set.has(d)) set.delete(d);
    else set.add(d);
    if (set.size === 0 || set.size === 7) {
      setField(4, "*");
    } else if ([...set].sort().join(",") === "1,2,3,4,5") {
      setField(4, "1-5");
    } else {
      setField(4, [...set].sort((a, b) => a - b).join(","));
    }
  };

  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>Day of Week</div>
      <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 8 }}>
        POSIX cron weekdays: <strong>0 = Sunday</strong>, 1 = Monday, ..., 6 = Saturday.
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 6, marginBottom: 10 }}>
        {WEEKDAY_NAMES.map((name, i) => {
          const active = selected.includes(i);
          return (
            <button
              key={i}
              type="button"
              style={{
                padding: "10px 0",
                background: active ? "var(--havn-accent)" : "var(--havn-bg-tertiary)",
                color: active ? "#fff" : "var(--havn-text)",
                border: "1px solid var(--havn-border-light)",
                borderRadius: 4,
                cursor: "pointer",
                fontSize: 12,
              }}
              onClick={() => toggle(i)}
            >
              <div style={{ fontWeight: 600 }}>{WEEKDAY_SHORT[i]}</div>
              <div style={{ fontSize: 9, opacity: 0.7 }}>{i}</div>
            </button>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <button style={s.btn} onClick={() => setField(4, "*")}>Every day</button>
        <button style={s.btn} onClick={() => setField(4, "1-5")}>Weekdays only</button>
        <button style={s.btn} onClick={() => setField(4, "0,6")}>Weekends only</button>
      </div>
    </div>
  );
}

/* Advanced raw cron tab ---------------------------------------------- */
function AdvancedTab({ parts, setParts }) {
  const [raw, setRaw] = useState(parts.join(" "));
  useEffect(() => { setRaw(parts.join(" ")); }, [parts]);

  const apply = () => {
    const next = raw.trim().split(/\s+/);
    if (next.length === 5) setParts(next);
  };

  return (
    <div style={{ padding: "12px 4px" }}>
      <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>Raw cron expression</div>
      <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 8 }}>
        Format: <code style={s.codeSm}>minute hour day-of-month month day-of-week</code>.
        Supports <code style={s.codeSm}>*</code>, <code style={s.codeSm}>*/N</code>,
        <code style={s.codeSm}> N-M</code>, <code style={s.codeSm}>a,b,c</code>.
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <input
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          style={{ ...s.input, flex: 1, fontFamily: "var(--havn-font-mono)" }}
          placeholder="0 6 * * *"
        />
        <button style={s.btnPrimary} onClick={apply}>Apply</button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* DAG Picker Modal                                                    */
/* ------------------------------------------------------------------ */

const SCHEMA_COLORS = {
  ingest:  "cyan",
  landing: "#64748b",
  bronze:  "#a16207",
  silver:  "#64748b",
  gold:    "#eab308",
  export:  "orchid",
};
function schemaColor(schema) {
  return SCHEMA_COLORS[schema] || "var(--havn-accent)";
}

function DagPickerModal({ initialTargets, onCancel, onSave }) {
  const [dag, setDag] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Selection is keyed by the BARE node id. Selector markers (+prefix/suffix)
  // are stored separately so a single node can have both.
  const [selection, setSelection] = useState(() => {
    const m = new Map();
    for (const t of initialTargets || []) {
      const { up, down, inner } = parseSelector(t);
      m.set(inner, { up, down });
    }
    return m;
  });
  const [filter, setFilter] = useState("");
  const [hover, setHover] = useState(null);
  const [planPreview, setPlanPreview] = useState(null);

  // Close on Escape, matching the backdrop-click dismiss.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onCancel(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const data = await api.getOrchestrationDag();
        setDag(data);
      } catch (e) {
        setError(e.message || "Failed to load DAG");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const byId = useMemo(() => {
    if (!dag) return new Map();
    const m = new Map();
    for (const n of dag.nodes) m.set(n.id, n);
    return m;
  }, [dag]);

  // Group nodes into columns for layout: Ingests / schemas / Exports
  const columns = useMemo(() => {
    if (!dag) return [];
    const ingests = dag.nodes.filter((n) => n.kind === "ingest");
    const exports_ = dag.nodes.filter((n) => n.kind === "export");
    const schemas = dag.schemas || [];
    const byschema = {};
    for (const sch of schemas) byschema[sch] = [];
    for (const n of dag.nodes) {
      if (n.kind === "transform" && byschema[n.schema] != null) {
        byschema[n.schema].push(n);
      }
    }
    const cols = [];
    if (ingests.length) cols.push({ id: "ingest", label: "Ingest", color: "cyan", nodes: ingests });
    for (const sch of schemas) {
      if ((byschema[sch] || []).length) {
        cols.push({ id: sch, label: sch.toUpperCase(), color: schemaColor(sch), nodes: byschema[sch] });
      }
    }
    if (exports_.length) cols.push({ id: "export", label: "Export", color: "orchid", nodes: exports_ });
    return cols;
  }, [dag]);

  // Filtered nodes
  const lf = filter.toLowerCase();
  const isVisible = (n) => {
    if (!lf) return true;
    return (
      (n.id || "").toLowerCase().includes(lf) ||
      (n.label || "").toLowerCase().includes(lf) ||
      (n.schema || "").toLowerCase().includes(lf)
    );
  };

  const toggleNode = (id) => {
    setSelection((prev) => {
      const next = new Map(prev);
      if (next.has(id)) next.delete(id);
      else next.set(id, { up: false, down: false });
      return next;
    });
  };

  // Click the left arrow of a box: toggle the "include upstream" flag on
  // that node's selection. Adds the node to the selection if it wasn't
  // already selected.
  const toggleUpstream = (id) => {
    setSelection((prev) => {
      const next = new Map(prev);
      const cur = next.get(id) || { up: false, down: false };
      next.set(id, { ...cur, up: !cur.up });
      return next;
    });
  };

  const toggleDownstream = (id) => {
    setSelection((prev) => {
      const next = new Map(prev);
      const cur = next.get(id) || { up: false, down: false };
      next.set(id, { ...cur, down: !cur.down });
      return next;
    });
  };

  const clearAll = () => setSelection(new Map());

  // Build dbt-style selector strings for each selected node
  const buildTargets = () => {
    const result = [];
    for (const [id, markers] of selection) {
      result.push(buildSelector(id, markers.up, markers.down));
    }
    return result;
  };

  // Build adjacency lists for upstream/downstream traversal
  const { upstreamOf, downstreamOf } = useMemo(() => {
    if (!dag) return { upstreamOf: {}, downstreamOf: {} };
    const up = {};   // nodeId -> Set of upstream node ids
    const down = {}; // nodeId -> Set of downstream node ids
    for (const n of dag.nodes) {
      up[n.id] = new Set();
      down[n.id] = new Set();
    }
    for (const e of dag.edges || []) {
      // e.source flows INTO e.target (source is upstream of target)
      if (up[e.target]) up[e.target].add(e.source);
      if (down[e.source]) down[e.source].add(e.target);
    }
    // Transitive closure helper
    const collect = (start, adj) => {
      const result = new Set();
      const queue = [start];
      while (queue.length) {
        const cur = queue.pop();
        for (const nb of adj[cur] || []) {
          if (!result.has(nb)) { result.add(nb); queue.push(nb); }
        }
      }
      return result;
    };
    // Pre-compute full transitive sets
    const upFull = {};
    const downFull = {};
    for (const n of dag.nodes) {
      upFull[n.id] = collect(n.id, up);
      downFull[n.id] = collect(n.id, down);
    }
    return { upstreamOf: upFull, downstreamOf: downFull };
  }, [dag]);

  // Compute the set of nodes "affected" by selector expansion (highlighted)
  const affectedNodes = useMemo(() => {
    const affected = new Set();
    for (const [id, markers] of selection) {
      if (markers.up) {
        for (const n of upstreamOf[id] || []) affected.add(n);
      }
      if (markers.down) {
        for (const n of downstreamOf[id] || []) affected.add(n);
      }
    }
    return affected;
  }, [selection, upstreamOf, downstreamOf]);

  // Live plan preview: count selected nodes per kind. The backend will
  // expand selector markers (+/+) at run time.
  useEffect(() => {
    if (selection.size === 0) {
      setPlanPreview(null);
      return;
    }
    const counts = { ingest: 0, transform: 0, export: 0 };
    for (const id of selection.keys()) {
      const n = byId.get(id);
      if (n) counts[n.kind] = (counts[n.kind] || 0) + 1;
    }
    setPlanPreview(counts);
  }, [selection, byId]);

  return (
    <div style={s.modalOverlay} onClick={onCancel}>
      <div style={{ ...s.modalCard, width: "min(1200px, 95vw)", maxHeight: "92vh" }} onClick={(e) => e.stopPropagation()}>
        <div style={s.modalHeader}>
          <div style={{ fontSize: 15, fontWeight: 600 }}>Pick targets from the DAG</div>
          <input
            style={{ ...s.filterInput, width: 260 }}
            placeholder="Filter nodes..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div style={{ flex: 1 }} />
          <span style={{ fontSize: 12, color: "var(--havn-text-dim)" }}>
            {selection.size} selected
          </span>
          <button style={s.btn} onClick={clearAll} disabled={selection.size === 0}>
            Clear
          </button>
        </div>

        <div style={s.dagHint}>
          <strong>Click</strong> a box to toggle it {MID_DOT}{" "}
          <strong>Click the ◀ on the left</strong> to include all upstream (+prefix){" "}
          {MID_DOT} <strong>Click the ▶ on the right</strong> to include all downstream (suffix+){" "}
          {MID_DOT} selectors resolve at run time
        </div>

        <div style={s.dagCanvas}>
          {loading && <div style={{ padding: 40, textAlign: "center", color: "var(--havn-text-dim)" }}>Loading DAG...</div>}
          {error && <div style={s.errorBanner}>{error}</div>}
          {!loading && !error && (
            <div style={{ display: "flex", gap: 14, padding: "12px 16px", minWidth: "fit-content" }}>
              {columns.map((col) => {
                const visibleNodes = col.nodes.filter(isVisible);
                return (
                  <div key={col.id} style={s.dagColumn}>
                    <div style={{ ...s.dagColumnHeader, color: col.color }}>
                      {col.label}
                      <span style={{ fontSize: 10, color: "var(--havn-text-dim)", fontWeight: 400, marginLeft: 6 }}>
                        {visibleNodes.length}/{col.nodes.length}
                      </span>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                      {visibleNodes.map((n) => {
                        const markers = selection.get(n.id);
                        const selected = markers != null;
                        const isHover = hover === n.id;
                        const isAffected = !selected && affectedNodes.has(n.id);
                        return (
                          <div
                            key={n.id}
                            style={{
                              ...s.dagBox,
                              borderColor: selected ? col.color : isAffected ? col.color : "var(--havn-border)",
                              borderStyle: isAffected ? "dashed" : "solid",
                              background: selected
                                ? `color-mix(in srgb, ${col.color} 18%, var(--havn-bg-secondary))`
                                : isAffected
                                  ? `color-mix(in srgb, ${col.color} 8%, var(--havn-bg-secondary))`
                                  : isHover
                                    ? "var(--havn-bg-tertiary)"
                                    : "var(--havn-bg-secondary)",
                              boxShadow: isHover ? `0 0 0 1px ${col.color}` : "none",
                              opacity: isAffected ? 0.85 : 1,
                            }}
                            onMouseEnter={() => setHover(n.id)}
                            onMouseLeave={() => setHover((h) => (h === n.id ? null : h))}
                          >
                            <button
                              type="button"
                              title="Toggle '+upstream' selector on this target"
                              style={{
                                ...s.dagZoneLeft,
                                background: markers?.up
                                  ? `color-mix(in srgb, ${col.color} 40%, transparent)`
                                  : s.dagZoneLeft.background,
                                color: markers?.up ? col.color : s.dagZoneLeft.color,
                                fontWeight: markers?.up ? 700 : 400,
                              }}
                              onClick={(e) => { e.stopPropagation(); toggleUpstream(n.id); }}
                            >
                              {markers?.up ? "+" : "\u25C0"}
                            </button>
                            <div style={s.dagBoxCenter} onClick={() => toggleNode(n.id)}>
                              <div style={{ fontSize: 11, fontWeight: 600, color: col.color }}>
                                {n.kind === "ingest" ? "ING" : n.kind === "export" ? "EXP" : n.schema.toUpperCase()}
                              </div>
                              <div style={{ fontSize: 12, fontWeight: 500, color: "var(--havn-text)", marginTop: 2 }}>
                                {n.label}
                              </div>
                              {n.row_count != null && (
                                <div style={{ fontSize: 10, color: "var(--havn-text-dim)", marginTop: 2 }}>
                                  {n.row_count.toLocaleString()} rows
                                </div>
                              )}
                              {n.kind === "transform" && n.last_run_at && (
                                <div style={{ fontSize: 10, color: "var(--havn-green)", marginTop: 2 }}>
                                  ran {timeAgo(n.last_run_at)}
                                </div>
                              )}
                            </div>
                            <button
                              type="button"
                              title="Toggle 'downstream+' selector on this target"
                              style={{
                                ...s.dagZoneRight,
                                background: markers?.down
                                  ? `color-mix(in srgb, ${col.color} 40%, transparent)`
                                  : s.dagZoneRight.background,
                                color: markers?.down ? col.color : s.dagZoneRight.color,
                                fontWeight: markers?.down ? 700 : 400,
                              }}
                              onClick={(e) => { e.stopPropagation(); toggleDownstream(n.id); }}
                            >
                              {markers?.down ? "+" : "\u25B6"}
                            </button>
                          </div>
                        );
                      })}
                      {visibleNodes.length === 0 && (
                        <div style={{ fontSize: 11, color: "var(--havn-text-dim)", padding: "8px 4px", textAlign: "center" }}>
                          no matches
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
              {columns.length === 0 && (
                <div style={{ padding: 40, color: "var(--havn-text-dim)", fontSize: 13 }}>
                  No DAG nodes. Create ingest scripts or transform models first.
                </div>
              )}
            </div>
          )}
        </div>

        {selection.size > 0 && (
          <div style={s.dagSelectionSummary}>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.3px" }}>
                Selection ({selection.size})
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                {[...selection.entries()].slice(0, 20).map(([id, markers]) => (
                  <span key={id} style={s.selectedChip}>
                    <code style={{ fontSize: 10 }}>
                      {buildSelector(id, markers.up, markers.down)}
                    </code>
                    <button style={s.chipRemove} onClick={() => toggleNode(id)}>×</button>
                  </span>
                ))}
                {selection.size > 20 && (
                  <span style={{ fontSize: 11, color: "var(--havn-text-dim)", alignSelf: "center" }}>
                    + {selection.size - 20} more
                  </span>
                )}
              </div>
            </div>
            {planPreview && (
              <div style={{ fontSize: 11, color: "var(--havn-text-dim)", alignSelf: "flex-start", textAlign: "right" }}>
                <div>{planPreview.ingest || 0} ingest {MID_DOT} {planPreview.transform || 0} transform {MID_DOT} {planPreview.export || 0} export</div>
                <div style={{ marginTop: 2, fontStyle: "italic" }}>
                  (selectors expand upstream/downstream at run time)
                </div>
              </div>
            )}
          </div>
        )}

        <div style={s.modalActions}>
          <button style={s.btn} onClick={onCancel}>Cancel</button>
          <button
            style={s.btnPrimary}
            onClick={() => onSave(buildTargets())}
            disabled={selection.size === 0}
          >
            Use {selection.size} target{selection.size === 1 ? "" : "s"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Plan Jobs Tab                                                       */
/* ------------------------------------------------------------------ */

const EMPTY_FORM = {
  name: "",
  targets: [],
  // resolve is still sent to the backend for BC but the UI no longer
  // exposes it — selector syntax on each target replaces it.
  resolve: "none",
  schedules: [],          // multi-schedule list
  tags: [],
  enabled: true,
  retry: 0,
  retry_delay: 10,
  timeout_minutes: 60,
  description: "",
  notify: [],
};

// Selector helpers. Each target string is a dbt-style selector:
//   schema.name   — just this
//   +schema.name  — this + upstream
//   schema.name+  — this + downstream
//   +schema.name+ — all of the above
function parseSelector(t) {
  if (!t) return { up: false, down: false, inner: "" };
  let up = false, down = false, inner = t;
  if (inner.startsWith("+")) { up = true; inner = inner.slice(1); }
  if (inner.endsWith("+")) { down = true; inner = inner.slice(0, -1); }
  return { up, down, inner };
}
function buildSelector(inner, up, down) {
  return `${up ? "+" : ""}${inner}${down ? "+" : ""}`;
}

function PlanJobsTab({ onSwitchToResults, showConfirm }) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [formMode, setFormMode] = useState(null); // "new" | { editing: jobName } | null
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  // Cron wizard is scoped to a specific schedule index within form.schedules
  const [cronWizardIdx, setCronWizardIdx] = useState(null);
  const [showDagPicker, setShowDagPicker] = useState(false);
  const [filterTag, setFilterTag] = useState("");

  const loadJobs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listJobs();
      setJobs(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message || "Failed to load jobs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadJobs(); }, [loadJobs]);

  const openCreate = () => {
    setForm(EMPTY_FORM);
    setFormMode("new");
  };

  const openEdit = (job) => {
    setForm({
      name: job.name || "",
      targets: Array.isArray(job.targets) && job.targets.length > 0
        ? [...job.targets]
        : (job.target ? [job.target] : []),
      resolve: job.resolve || "upstream",
      schedules: Array.isArray(job.schedules) && job.schedules.length > 0
        ? [...job.schedules]
        : (job.cron ? [job.cron] : []),
      tags: Array.isArray(job.tags) ? [...job.tags] : [],
      enabled: job.enabled !== false,
      retry: job.retry || 0,
      retry_delay: job.retry_delay || 10,
      timeout_minutes: job.timeout_minutes || 60,
      description: job.description || "",
      notify: job.notify || [],
    });
    setFormMode({ editing: job.name });
  };

  const openClone = (job) => {
    setForm({
      name: `${job.name}-copy`,
      targets: Array.isArray(job.targets) ? [...job.targets] : (job.target ? [job.target] : []),
      resolve: job.resolve || "upstream",
      schedules: Array.isArray(job.schedules) ? [...job.schedules] : (job.cron ? [job.cron] : []),
      tags: Array.isArray(job.tags) ? [...job.tags] : [],
      enabled: false,  // start disabled so the clone doesn't fire unexpectedly
      retry: job.retry || 0,
      retry_delay: job.retry_delay || 10,
      timeout_minutes: job.timeout_minutes || 60,
      description: job.description || "",
      notify: job.notify || [],
    });
    setFormMode("new");
  };

  const closeForm = () => {
    setFormMode(null);
    setCronWizardIdx(null);
    setShowDagPicker(false);
  };

  const handleSave = async () => {
    if (!form.name.trim()) {
      setError("Name is required");
      return;
    }
    if (form.targets.length === 0) {
      setError("At least one target is required");
      return;
    }
    // Validate every schedule client-side (accepts cron OR interval)
    for (const sched of form.schedules) {
      if (!isValidSchedule(sched)) {
        setError(`Invalid schedule (expected cron 5-field or 'every N unit'): ${sched}`);
        return;
      }
    }
    setSaving(true);
    setError(null);
    try {
      // Numeric fields hold the raw string while the user types; coerce and
      // clamp here so a field cleared at save time falls back to its default.
      const toInt = (v, lo, hi, fallback) => {
        const num = parseInt(v, 10);
        return Number.isNaN(num) ? fallback : Math.min(hi, Math.max(lo, num));
      };
      const payload = {
        targets: form.targets,
        resolve: form.resolve,
        schedules: form.schedules,
        tags: form.tags,
        enabled: form.enabled,
        retry: toInt(form.retry, 0, 10, 0),
        retry_delay: toInt(form.retry_delay, 0, 3600, 10),
        timeout_minutes: toInt(form.timeout_minutes, 1, 24 * 60, 60),
        description: form.description,
      };
      if (formMode && formMode.editing) {
        await api.updateJob(formMode.editing, payload);
      } else {
        await api.createJob({ name: form.name, ...payload });
      }
      closeForm();
      await loadJobs();
    } catch (e) {
      setError(e.message || "Failed to save job");
    } finally {
      setSaving(false);
    }
  };

  const handleRun = async (name) => {
    try {
      await api.runJob(name);
      onSwitchToResults();
    } catch (e) {
      setError(e.message || "Failed to run job");
    }
  };

  const handleToggle = async (name, enabled) => {
    try {
      await api.updateJob(name, { enabled });
      await loadJobs();
    } catch (e) {
      setError(e.message || "Failed to toggle job");
    }
  };

  const handleDelete = async (name) => {
    const ok = await showConfirm("Delete Job", `Are you sure you want to delete job "${name}"?`, "Delete", true);
    if (!ok) return;
    try {
      await api.deleteJob(name);
      await loadJobs();
    } catch (e) {
      setError(e.message || "Failed to delete job");
    }
  };

  const editing = formMode && formMode.editing;

  // Collect all unique tags from the loaded jobs
  const allTags = useMemo(() => {
    const set = new Set();
    for (const j of jobs) {
      for (const t of (j.tags || [])) set.add(t);
    }
    return [...set].sort();
  }, [jobs]);

  const visibleJobs = useMemo(() => {
    if (!filterTag) return jobs;
    return jobs.filter((j) => (j.tags || []).includes(filterTag));
  }, [jobs, filterTag]);

  return (
    <div style={s.content}>
      <div style={s.toolbar}>
        <button style={s.btnPrimary} onClick={openCreate} disabled={formMode != null}>
          + New Job
        </button>
        <button style={s.btn} onClick={loadJobs} disabled={loading}>
          {loading ? "Loading..." : "Refresh"}
        </button>
        {allTags.length > 0 && (
          <select
            style={s.filterSelect}
            value={filterTag}
            onChange={(e) => setFilterTag(e.target.value)}
          >
            <option value="">All tags</option>
            {allTags.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        <span style={s.count}>
          {visibleJobs.length} {visibleJobs.length === 1 ? "job" : "jobs"}
          {filterTag && ` (${jobs.length} total)`}
        </span>
      </div>

      {error && (
        <div style={s.errorBanner}>
          <span>{error}</span>
          <button style={s.errorClose} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {loading && jobs.length === 0 ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>Loading...</div>
        </div>
      ) : jobs.length === 0 && formMode == null ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>No orchestration jobs yet</div>
          <div style={s.emptyText}>
            Click <strong>+ New Job</strong> to define a pipeline, or create YAML files
            in <code style={s.emptyCode}>orchestration/</code>.
          </div>
        </div>
      ) : (
        <div style={{ border: "1px solid var(--havn-border)", borderRadius: 4, overflow: "hidden" }}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}>Name / Tags</th>
                <th style={s.th}>Targets</th>
                <th style={s.th}>Schedules</th>
                <th style={s.th}>Enabled</th>
                <th style={s.th}>History</th>
                <th style={s.th}>Last run</th>
                <th style={s.th}>Next run</th>
                <th style={{ ...s.th, textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {formMode === "new" && (
                <JobFormRow
                  form={form}
                  setForm={setForm}
                  editing={false}
                  saving={saving}
                  onSave={handleSave}
                  onCancel={closeForm}
                  onOpenCronWizard={(idx) => setCronWizardIdx(idx)}
                  onOpenDagPicker={() => setShowDagPicker(true)}
                />
              )}
              {visibleJobs.map((job) => {
                const isBeingEdited = editing === job.name;
                if (isBeingEdited) {
                  return (
                    <JobFormRow
                      key={job.name}
                      form={form}
                      setForm={setForm}
                      editing={true}
                      saving={saving}
                      onSave={handleSave}
                      onCancel={closeForm}
                      onOpenCronWizard={(idx) => setCronWizardIdx(idx)}
                      onOpenDagPicker={() => setShowDagPicker(true)}
                    />
                  );
                }
                return (
                  <JobTableRow
                    key={job.name}
                    job={job}
                    onRun={() => handleRun(job.name)}
                    onEdit={() => openEdit(job)}
                    onClone={() => openClone(job)}
                    onDelete={() => handleDelete(job.name)}
                    onToggle={(enabled) => handleToggle(job.name, enabled)}
                    onTagClick={(tag) => setFilterTag(tag)}
                    disabled={formMode != null}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {cronWizardIdx != null && (
        <CronWizardModal
          initial={form.schedules[cronWizardIdx] || ""}
          onCancel={() => setCronWizardIdx(null)}
          onSave={(expr) => {
            setForm((f) => {
              const next = [...f.schedules];
              if (cronWizardIdx < next.length) {
                next[cronWizardIdx] = expr;
              } else {
                next.push(expr);
              }
              return { ...f, schedules: next };
            });
            setCronWizardIdx(null);
          }}
        />
      )}

      {showDagPicker && (
        <DagPickerModal
          initialTargets={form.targets}
          onCancel={() => setShowDagPicker(false)}
          onSave={(targets) => {
            setForm((f) => ({ ...f, targets }));
            setShowDagPicker(false);
          }}
        />
      )}
    </div>
  );
}

/* History sparkline: last 10 runs as colored dots --------------------- */
function Sparkline({ runs }) {
  if (!runs || runs.length === 0) {
    return <span style={{ color: "var(--havn-text-dim)", fontSize: 11 }}>—</span>;
  }
  const dots = runs.slice(-10);
  const maxMs = Math.max(...dots.map((r) => r.duration_ms || 0), 1);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 24 }}>
      {dots.map((r, i) => {
        const h = Math.max(4, Math.round(((r.duration_ms || 0) / maxMs) * 20));
        const color =
          r.status === "success"
            ? "var(--havn-green)"
            : r.status === "failure" || r.status === "error" || r.status === "timeout"
              ? "var(--havn-red)"
              : r.status === "cancelled"
                ? "var(--havn-text-dim)"
                : "var(--havn-accent)";
        return (
          <div
            key={i}
            title={`${r.status}${r.duration_ms ? ` · ${formatDuration(r.duration_ms)}` : ""}${r.started_at ? ` · ${timeAgo(r.started_at)}` : ""}`}
            style={{
              width: 6,
              height: h,
              background: color,
              borderRadius: 1,
            }}
          />
        );
      })}
    </div>
  );
}

/* Single job row (read-only) ----------------------------------------- */
function JobTableRow({ job, onRun, onEdit, onClone, onDelete, onToggle, onTagClick, disabled }) {
  const targets = Array.isArray(job.targets) && job.targets.length > 0
    ? job.targets
    : job.target ? [job.target] : [];
  const schedules = Array.isArray(job.schedules) && job.schedules.length > 0
    ? job.schedules
    : job.cron ? [job.cron] : [];
  const tags = Array.isArray(job.tags) ? job.tags : [];

  return (
    <tr>
      <td style={s.td}>
        <div style={{ fontWeight: 500 }}>{job.name}</div>
        {job.description && (
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 2 }}>
            {job.description}
          </div>
        )}
        {tags.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>
            {tags.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => onTagClick(t)}
                style={s.tagBadge}
                title={`Filter by tag: ${t}`}
              >
                #{t}
              </button>
            ))}
          </div>
        )}
      </td>
      <td style={s.td}>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {targets.slice(0, 4).map((t) => (
            <code key={t} style={s.codeSm}>{t}</code>
          ))}
          {targets.length > 4 && (
            <span style={{ fontSize: 11, color: "var(--havn-text-dim)" }}>
              + {targets.length - 4} more
            </span>
          )}
        </div>
      </td>
      <td style={s.td}>
        {schedules.length === 0 ? (
          <span style={{ color: "var(--havn-text-dim)", fontSize: 11 }}>on-demand</span>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {schedules.map((sched, i) => (
              <div key={i}>
                <code style={s.codeSm}>{sched}</code>
                <div style={{ fontSize: 10, color: "var(--havn-text-dim)", marginTop: 1 }}>
                  {describeCron(sched)}
                </div>
              </div>
            ))}
          </div>
        )}
      </td>
      <td style={s.td}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 12 }}>
          <input
            type="checkbox"
            checked={job.enabled}
            onChange={(e) => onToggle(e.target.checked)}
            disabled={disabled}
          />
          {job.enabled ? "on" : "off"}
        </label>
      </td>
      <td style={s.td}>
        <Sparkline runs={job.sparkline || []} />
      </td>
      <td style={s.td}>
        {job.last_run ? (
          <div>
            <StatusBadge status={job.last_run.status} />
            <div style={{ fontSize: 10, color: "var(--havn-text-dim)", marginTop: 2 }}>
              {timeAgo(job.last_run.started_at)}
            </div>
          </div>
        ) : <span style={{ color: "var(--havn-text-dim)", fontSize: 11 }}>never</span>}
      </td>
      <td style={s.td}>
        {job.next_run ? (
          <div title={formatAbsolute(job.next_run)}>
            <div style={{ fontSize: 12 }}>{formatIn(job.next_run)}</div>
            <div style={{ fontSize: 10, color: "var(--havn-text-dim)", marginTop: 2 }}>
              {formatAbsolute(job.next_run)}
            </div>
          </div>
        ) : <span style={{ color: "var(--havn-text-dim)", fontSize: 11 }}>—</span>}
      </td>
      <td style={{ ...s.td, textAlign: "right" }}>
        <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
          <button style={s.btn} onClick={onRun} disabled={disabled}>Run</button>
          <button style={s.btn} onClick={onEdit} disabled={disabled}>Edit</button>
          <button style={s.btn} onClick={onClone} disabled={disabled} title="Clone this job">Clone</button>
          <button style={s.btnDanger} onClick={onDelete} disabled={disabled}>×</button>
        </div>
      </td>
    </tr>
  );
}

/* Inline form row (new or edit) -------------------------------------- */
function JobFormRow({ form, setForm, editing, saving, onSave, onCancel, onOpenCronWizard, onOpenDagPicker }) {
  const [tagInput, setTagInput] = useState("");

  const removeTarget = (idx) => {
    setForm((f) => ({ ...f, targets: f.targets.filter((_, i) => i !== idx) }));
  };
  const removeSchedule = (idx) => {
    setForm((f) => ({ ...f, schedules: f.schedules.filter((_, i) => i !== idx) }));
  };
  const updateSchedule = (idx, value) => {
    setForm((f) => {
      const next = [...f.schedules];
      next[idx] = value;
      return { ...f, schedules: next };
    });
  };
  const addSchedule = () => {
    setForm((f) => ({ ...f, schedules: [...f.schedules, ""] }));
  };
  const addTag = () => {
    const t = tagInput.trim();
    if (!t) return;
    if (!form.tags.includes(t)) {
      setForm((f) => ({ ...f, tags: [...f.tags, t] }));
    }
    setTagInput("");
  };
  const removeTag = (tag) => {
    setForm((f) => ({ ...f, tags: f.tags.filter((t) => t !== tag) }));
  };

  return (
    <tr style={{ background: "var(--havn-bg-secondary)" }}>
      <td colSpan={8} style={{ padding: "14px 16px", borderBottom: "1px solid var(--havn-border)" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              {editing ? `Editing: ${form.name}` : "New job"}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <button style={s.btn} onClick={onCancel}>Cancel</button>
              <button style={s.btnPrimary} onClick={onSave} disabled={saving}>
                {saving ? "Saving..." : editing ? "Save" : "Create"}
              </button>
            </div>
          </div>

          {/* Row: name + description */}
          <div style={{ display: "grid", gridTemplateColumns: "280px 1fr", gap: 12 }}>
            <label style={s.formLabel}>
              <span style={s.formLabelText}>Name</span>
              <input
                style={s.input}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="daily-refresh"
                disabled={editing}
              />
            </label>
            <label style={s.formLabel}>
              <span style={s.formLabelText}>Description</span>
              <input
                style={s.input}
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Optional human-readable description"
              />
            </label>
          </div>

          {/* Targets: chip editor with per-chip upstream/downstream toggles */}
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
              <span style={s.formLabelText}>
                Targets {form.targets.length > 0 && `(${form.targets.length})`}
              </span>
              <button style={s.btn} onClick={onOpenDagPicker}>
                Pick from DAG
              </button>
            </div>
            <div style={s.chipBox}>
              {form.targets.length === 0 ? (
                <span style={{ fontSize: 12, color: "var(--havn-text-dim)", padding: "4px 0" }}>
                  No targets selected. Click <strong>Pick from DAG</strong> to choose, or
                  use dbt-style selectors: <code style={s.codeSm}>+silver.orders</code> (upstream),{" "}
                  <code style={s.codeSm}>bronze.orders+</code> (downstream),{" "}
                  <code style={s.codeSm}>+gold.*+</code> (both).
                </span>
              ) : (
                form.targets.map((t, i) => {
                  const { up, down, inner } = parseSelector(t);
                  return (
                    <span key={`${t}-${i}`} style={s.targetChip}>
                      <button
                        type="button"
                        style={{ ...s.selectorBtn, background: up ? "var(--havn-accent)" : "transparent", color: up ? "#fff" : "var(--havn-accent)" }}
                        onClick={() => {
                          const next = [...form.targets];
                          next[i] = buildSelector(inner, !up, down);
                          setForm((f) => ({ ...f, targets: next }));
                        }}
                        title={up ? "Remove upstream" : "Include upstream"}
                      >
                        +
                      </button>
                      <code style={{ fontSize: 11 }}>{inner}</code>
                      <button
                        type="button"
                        style={{ ...s.selectorBtn, background: down ? "var(--havn-accent)" : "transparent", color: down ? "#fff" : "var(--havn-accent)" }}
                        onClick={() => {
                          const next = [...form.targets];
                          next[i] = buildSelector(inner, up, !down);
                          setForm((f) => ({ ...f, targets: next }));
                        }}
                        title={down ? "Remove downstream" : "Include downstream"}
                      >
                        +
                      </button>
                      <button
                        type="button"
                        style={s.chipRemove}
                        onClick={() => removeTarget(i)}
                        title="Remove"
                      >
                        ×
                      </button>
                    </span>
                  );
                })
              )}
            </div>
          </div>

          {/* Schedules — multi-cron */}
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
              <span style={s.formLabelText}>
                Schedules {form.schedules.length > 0 && `(${form.schedules.length})`}
              </span>
              <button style={s.btn} onClick={addSchedule}>+ Add schedule</button>
            </div>
            {form.schedules.length === 0 ? (
              <div style={{ padding: "10px 12px", background: "var(--havn-bg-tertiary)", border: "1px solid var(--havn-border-light)", borderRadius: 6, fontSize: 12, color: "var(--havn-text-dim)" }}>
                No schedules. This job will only run when triggered manually.
                Click <strong>+ Add schedule</strong> to run it automatically.
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {form.schedules.map((sched, i) => {
                  const isInvalid = sched && !isValidSchedule(sched);
                  return (
                    <div key={i} style={s.scheduleRow}>
                      <div style={{ flex: 1 }}>
                        <div style={{ display: "flex", gap: 6 }}>
                          <input
                            style={{ ...s.input, flex: 1, fontFamily: "var(--havn-font-mono)", borderColor: isInvalid ? "var(--havn-red)" : undefined }}
                            value={sched}
                            onChange={(e) => updateSchedule(i, e.target.value)}
                            placeholder="0 6 * * *"
                          />
                          <button style={s.btn} onClick={() => onOpenCronWizard(i)} type="button">
                            Wizard
                          </button>
                          <button style={s.btnDanger} onClick={() => removeSchedule(i)} type="button" title="Remove schedule">
                            ×
                          </button>
                        </div>
                        <div style={{
                          fontSize: 11,
                          color: isInvalid ? "var(--havn-red)" : "var(--havn-text-dim)",
                          marginTop: 4,
                          fontStyle: "italic"
                        }}>
                          {isInvalid ? "Invalid schedule — use cron (5 fields) or 'every N unit'" : sched ? describeCron(sched) : "Empty schedule"}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Tags */}
          <div>
            <div>
              <div style={{ ...s.formLabelText, marginBottom: 4 }}>
                Tags {form.tags.length > 0 && `(${form.tags.length})`}
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <input
                  style={{ ...s.input, flex: 1 }}
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") { e.preventDefault(); addTag(); }
                  }}
                  placeholder="Type a tag and press Enter"
                />
                <button type="button" style={s.btn} onClick={addTag}>Add</button>
              </div>
              {form.tags.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }}>
                  {form.tags.map((t) => (
                    <span key={t} style={s.tagBadge}>
                      #{t}
                      <button type="button" style={s.chipRemove} onClick={() => removeTag(t)}>×</button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Retry / timeout / enabled */}
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ ...s.formLabel, flex: "0 0 80px" }}>
              <span style={s.formLabelText}>Retry</span>
              <input
                type="number" min="0" max="10"
                style={s.input}
                value={form.retry}
                onChange={(e) => setForm({ ...form, retry: e.target.value })}
              />
            </label>
            <label style={{ ...s.formLabel, flex: "0 0 110px" }}>
              <span style={s.formLabelText}>Retry delay (s)</span>
              <input
                type="number" min="0"
                style={s.input}
                value={form.retry_delay}
                onChange={(e) => setForm({ ...form, retry_delay: e.target.value })}
              />
            </label>
            <label style={{ ...s.formLabel, flex: "0 0 110px" }}>
              <span style={s.formLabelText}>Timeout (min)</span>
              <input
                type="number" min="1"
                style={s.input}
                value={form.timeout_minutes}
                onChange={(e) => setForm({ ...form, timeout_minutes: e.target.value })}
              />
            </label>
            <label style={{ display: "inline-flex", alignItems: "center", gap: 6, paddingBottom: 2, fontSize: 12, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              />
              Enabled
            </label>
          </div>
        </div>
      </td>
    </tr>
  );
}

/* ------------------------------------------------------------------ */
/* Job Results Tab (unchanged behavior, restyled)                      */
/* ------------------------------------------------------------------ */

function JobResultsTab() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedRun, setSelectedRun] = useState(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [error, setError] = useState(null);
  const { running: pipelineRunning } = usePipeline();

  const loadRuns = useCallback(async () => {
    try {
      const data = await api.listJobRuns(100);
      setRuns(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(e.message || "Failed to load runs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadRuns(); }, [loadRuns]);

  // Poll while any job is running (fast) or pipeline is active (medium).
  // Also do a background poll every 10s so new runs always appear.
  useEffect(() => {
    const hasRunning = runs.some((r) => r.status === "running");
    const interval = hasRunning ? 2000 : pipelineRunning ? 3000 : 10000;
    const id = setInterval(loadRuns, interval);
    return () => clearInterval(id);
  }, [runs, loadRuns, pipelineRunning]);

  // Refresh immediately when pipeline stops (a run just finished)
  const prevPipelineRunning = useRef(pipelineRunning);
  useEffect(() => {
    if (prevPipelineRunning.current && !pipelineRunning) {
      loadRuns();
    }
    prevPipelineRunning.current = pipelineRunning;
  }, [pipelineRunning, loadRuns]);

  const openRun = async (run) => {
    try {
      const detail = await api.getJobRun(run.id);
      setSelectedRun(detail);
    } catch (e) {
      setError(e.message || "Failed to load run detail");
    }
  };

  const handleCancel = async (runId) => {
    try {
      await api.cancelJobRun(runId);
      await loadRuns();
    } catch (e) {
      setError(e.message || "Failed to cancel run");
    }
  };

  const filteredRuns = statusFilter === "all" ? runs : runs.filter((r) => r.status === statusFilter);

  if (selectedRun) {
    return <JobRunDetail run={selectedRun} onBack={() => setSelectedRun(null)} onRerun={loadRuns} />;
  }

  return (
    <div style={s.content}>
      <div style={s.toolbar}>
        <select
          style={s.filterSelect}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="all">All statuses</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
          <option value="running">Running</option>
          <option value="cancelled">Cancelled</option>
          <option value="timeout">Timeout</option>
        </select>
        <button style={s.btn} onClick={loadRuns} disabled={loading}>
          {loading ? "Loading..." : "Refresh"}
        </button>
        <span style={s.count}>{filteredRuns.length} of {runs.length}</span>
      </div>

      {error && (
        <div style={s.errorBanner}>
          <span>{error}</span>
          <button style={s.errorClose} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {loading && runs.length === 0 ? (
        <div style={s.emptyState}><div style={s.emptyTitle}>Loading job runs...</div></div>
      ) : filteredRuns.length === 0 ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>No job runs yet</div>
          <div style={s.emptyText}>Run a job from the Plan Jobs tab to see results here.</div>
        </div>
      ) : (
        <table style={{ ...s.table, tableLayout: "fixed" }}>
          <colgroup>
            <col style={{ width: "auto" }} />
            <col style={{ width: 90 }} />
            <col style={{ width: 140 }} />
            <col style={{ width: 80 }} />
            <col style={{ width: 72 }} />
            <col style={{ width: 90 }} />
            <col style={{ width: 72 }} />
          </colgroup>
          <thead>
            <tr>
              <th style={s.th}>Job</th>
              <th style={s.th}>Status</th>
              <th style={s.th}>Steps</th>
              <th style={s.th}>Duration</th>
              <th style={s.th}>Trigger</th>
              <th style={s.th}>Started</th>
              <th style={{ ...s.th, textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredRuns.map((run) => (
              <tr key={run.id} style={{ cursor: "pointer" }} onClick={() => openRun(run)}>
                <td style={{ ...s.td, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}><strong>{run.job_name}</strong></td>
                <td style={s.td}><StatusBadge status={run.status} /></td>
                <td style={s.td}>
                  {run.steps_completed}/{run.steps_total}
                  {run.steps_failed > 0 && (
                    <span style={{ color: "var(--havn-red)", marginLeft: 6, fontSize: 11 }}>
                      ({run.steps_failed} failed)
                    </span>
                  )}
                  {run.steps_skipped > 0 && (
                    <span style={{ color: "var(--havn-text-dim)", marginLeft: 6, fontSize: 11 }}>
                      ({run.steps_skipped} skipped)
                    </span>
                  )}
                </td>
                <td style={s.td}>{formatDuration(run.duration_ms)}</td>
                <td style={s.td}>{run.trigger || "-"}</td>
                <td style={s.td}>
                  <div style={{ fontSize: 11 }}>{timeAgo(run.started_at)}</div>
                </td>
                <td style={{ ...s.td, textAlign: "right" }}>
                  {run.status === "running" && (
                    <button
                      style={s.btnDanger}
                      onClick={(e) => { e.stopPropagation(); handleCancel(run.id); }}
                    >
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function JobRunDetail({ run: initialRun, onBack, onRerun }) {
  const [run, setRun] = useState(initialRun);
  const [rerunError, setRerunError] = useState(null);

  // Auto-refresh while the run is still in progress
  useEffect(() => {
    if (run.status !== "running") return;
    const id = setInterval(async () => {
      try {
        const updated = await api.getJobRun(run.id);
        setRun(updated);
      } catch (_e) { /* ignore */ }
    }, 2000);
    return () => clearInterval(id);
  }, [run.id, run.status]);

  const completed = run.steps_completed || 0;
  const failed = run.steps_failed || 0;
  const total = run.steps_total || 0;
  const skipped = run.steps_skipped || 0;

  const handleRerun = async () => {
    setRerunError(null);
    try {
      await api.runJob(run.job_name);
      if (onRerun) onRerun();
      onBack();
    } catch (e) {
      setRerunError(e.message || "Failed to rerun job");
    }
  };

  return (
    <div style={s.content}>
      <div style={s.toolbar}>
        <button style={s.btn} onClick={onBack}>← Back</button>
        <button style={s.btnPrimary} onClick={handleRerun}>Rerun</button>
      </div>

      {rerunError && (
        <div role="alert" style={{ padding: "8px 12px", marginBottom: 12, border: "1px solid var(--havn-red)", borderRadius: 4, color: "var(--havn-red)", fontSize: 12, background: "color-mix(in srgb, var(--havn-red) 10%, transparent)" }}>
          {rerunError}
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", padding: "12px 16px", border: "1px solid var(--havn-border)", borderRadius: 4, marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 600 }}>{run.job_name}</div>
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 4 }}>
            {run.started_at && new Date(run.started_at).toLocaleString()} {MID_DOT} {run.trigger || "manual"}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <StatusBadge status={run.status} />
          <div style={{ fontSize: 12, color: "var(--havn-text-dim)", marginTop: 4 }}>
            {formatDuration(run.duration_ms)}
          </div>
        </div>
      </div>

      <div style={{ padding: "8px 16px", fontSize: 13, marginBottom: 12, background: "color-mix(in srgb, var(--havn-text) 4%, transparent)", borderRadius: 4 }}>
        <strong>{completed}</strong>/<strong>{total}</strong> completed
        {failed > 0 && <span style={{ color: "var(--havn-red)", marginLeft: 8 }}>{failed} failed</span>}
        {skipped > 0 && <span style={{ color: "var(--havn-text-dim)", marginLeft: 8 }}>{skipped} skipped</span>}
      </div>

      {run.error && (
        <div style={s.errorBanner}>{run.error}</div>
      )}

      <div style={{ border: "1px solid var(--havn-border)", borderRadius: 4, overflow: "hidden" }}>
        {(run.step_details || []).length === 0 ? (
          <div style={{ padding: 16, textAlign: "center", color: "var(--havn-text-dim)" }}>
            No steps yet
          </div>
        ) : (
          (run.step_details || []).map((step, i) => <StepDetailRow key={i} step={step} />)
        )}
      </div>
    </div>
  );
}

function RowDelta({ current, previous }) {
  if (previous == null || current == null) return null;
  const delta = current - previous;
  if (delta === 0) return <span style={{ fontSize: 10, color: "var(--havn-text-dim)", marginLeft: 4 }}>{"\u00B1"}0</span>;
  const color = delta > 0 ? "var(--havn-green)" : "var(--havn-red)";
  const sign = delta > 0 ? "+" : "";
  return <span style={{ fontSize: 10, color, marginLeft: 4, fontWeight: 500 }}>{sign}{delta.toLocaleString()}</span>;
}

function StepPreview({ target }) {
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    const parts = target.split(".");
    if (parts.length !== 2) return;
    setLoading(true);
    api.getStepPreview(parts[0], parts[1], 8)
      .then(setPreview)
      .catch((e) => setError(e.message || "Preview unavailable"))
      .finally(() => setLoading(false));
  }, [target]);

  if (loading) return <div style={{ padding: "6px 0", fontSize: 11, color: "var(--havn-text-dim)" }}>Loading preview...</div>;
  if (error) return null;
  if (!preview || !preview.rows?.length) return null;

  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 10, color: "var(--havn-text-dim)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.3px" }}>
        Data preview ({preview.rows.length} rows)
      </div>
      <div style={{ overflow: "auto", maxHeight: 200, border: "1px solid var(--havn-border)", borderRadius: 3 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, fontFamily: "var(--havn-font-mono)" }}>
          <thead>
            <tr>
              {preview.columns.map((col) => (
                <th key={col} style={{ padding: "3px 8px", borderBottom: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)", color: "var(--havn-text-secondary)", textAlign: "left", fontWeight: 600, whiteSpace: "nowrap", position: "sticky", top: 0 }}>
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((row, ri) => (
              <tr key={ri}>
                {row.map((cell, ci) => (
                  <td key={ci} style={{ padding: "2px 8px", borderBottom: "1px solid var(--havn-border)", color: cell == null ? "var(--havn-text-dim)" : "var(--havn-text)", fontStyle: cell == null ? "italic" : "normal", whiteSpace: "nowrap", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {cell == null ? "NULL" : String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StepDetailRow({ step }) {
  const [expanded, setExpanded] = useState(false);
  const hasError = step.status === "error" && step.error;
  const hasOutput = !!(step.log_output);
  const isTransform = step.type === "transform";
  const canExpand = hasError || hasOutput || (isTransform && step.status === "success");
  const rows = step.rows_affected;
  const prevRows = step.previous_row_count;
  return (
    <div style={{ borderBottom: "1px solid var(--havn-border)" }}>
      <div
        style={{ display: "flex", alignItems: "center", padding: "8px 12px", fontSize: 13, cursor: canExpand ? "pointer" : "default" }}
        onClick={() => canExpand && setExpanded(!expanded)}
      >
        <span style={{ color: "var(--havn-text-dim)", width: 28, flexShrink: 0, textAlign: "right", marginRight: 8 }}>{step.step}.</span>
        <span style={{ width: 40, flexShrink: 0, marginRight: 8 }}><TypeBadge type={step.type} /></span>
        <code style={{ ...s.codeSm, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{step.target}</code>
        <span style={{ width: 72, flexShrink: 0, textAlign: "center", marginLeft: 8 }}><StatusBadge status={step.status} /></span>
        <span style={{ width: 64, flexShrink: 0, textAlign: "right", fontSize: 11, color: "var(--havn-text-dim)", marginLeft: 8 }}>
          {step.duration_ms != null ? formatDuration(step.duration_ms) : ""}
        </span>
        <span style={{ width: 100, flexShrink: 0, textAlign: "right", fontSize: 11, color: "var(--havn-text-dim)", marginLeft: 8, display: "flex", alignItems: "center", justifyContent: "flex-end" }}>
          {rows != null && rows > 0 && <span>{rows.toLocaleString()} rows</span>}
          <RowDelta current={rows} previous={prevRows} />
        </span>
        <span style={{ width: 16, flexShrink: 0, textAlign: "center", fontSize: 10, color: "var(--havn-text-dim)", marginLeft: 4 }}>
          {canExpand ? (expanded ? "\u25BE" : "\u25B8") : ""}
        </span>
      </div>
      {expanded && (
        <div style={{ padding: "8px 16px 10px 84px" }}>
          {hasError && (
            <div style={{ fontFamily: "var(--havn-font-mono)", fontSize: 11, whiteSpace: "pre-wrap", color: "var(--havn-red)", padding: "6px 8px", background: "color-mix(in srgb, var(--havn-red) 4%, transparent)", borderRadius: 3, marginBottom: hasOutput ? 6 : 0 }}>
              {step.error}
            </div>
          )}
          {hasOutput && (
            <div style={{ fontFamily: "var(--havn-font-mono)", fontSize: 11, whiteSpace: "pre-wrap", color: "var(--havn-text-secondary)", padding: "6px 8px", background: "color-mix(in srgb, var(--havn-text) 3%, transparent)", borderRadius: 3 }}>
              {step.log_output}
            </div>
          )}
          {isTransform && step.status === "success" && step.target.includes(".") && (
            <StepPreview target={step.target} />
          )}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Root                                                                */
/* ------------------------------------------------------------------ */

export default function OrchestrationPanel({ showConfirm }) {
  const [subTab, setSubTab] = useState("plan-jobs");
  return (
    <div style={s.container}>
      <div style={s.header}>
        <div style={s.tabs}>
          {[
            { id: "plan-jobs", label: "Plan Jobs" },
            { id: "job-results", label: "Job Results" },
          ].map((t) => (
            <div
              key={t.id}
              style={{ ...s.tab, ...(subTab === t.id ? s.tabActive : {}) }}
              onClick={() => setSubTab(t.id)}
            >
              {t.label}
            </div>
          ))}
        </div>
      </div>
      {subTab === "plan-jobs" && <PlanJobsTab onSwitchToResults={() => setSubTab("job-results")} showConfirm={showConfirm} />}
      {subTab === "job-results" && <JobResultsTab />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const s = {
  container: {
    height: "100%",
    display: "flex",
    flexDirection: "column",
    background: "var(--havn-bg)",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    flexDirection: "column",
    padding: "0 12px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
  },
  tabs: { display: "flex", gap: 0 },
  tab: {
    padding: "10px 20px",
    cursor: "pointer",
    fontSize: 13,
    color: "var(--havn-text-secondary)",
    borderBottom: "2px solid transparent",
    background: "none",
    userSelect: "none",
  },
  tabActive: {
    color: "var(--havn-text)",
    borderBottom: "2px solid var(--havn-accent)",
  },
  content: { flex: 1, overflow: "auto", padding: 20 },
  toolbar: {
    display: "flex",
    gap: 8,
    marginBottom: 14,
    alignItems: "center",
    flexWrap: "wrap",
  },
  count: { fontSize: 12, color: "var(--havn-text-dim)", marginLeft: "auto" },
  filterInput: {
    padding: "5px 10px",
    background: "var(--havn-bg-tertiary)",
    color: "var(--havn-text)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
    fontSize: 12,
    width: 220,
  },
  filterSelect: {
    padding: "5px 8px",
    background: "var(--havn-bg-tertiary)",
    color: "var(--havn-text)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
    fontSize: 12,
  },
  btn: {
    padding: "4px 12px",
    background: "var(--havn-btn-bg)",
    color: "var(--havn-text)",
    border: "1px solid var(--havn-btn-border)",
    borderRadius: "var(--havn-radius-lg)",
    cursor: "pointer",
    fontSize: 11,
    fontWeight: 500,
  },
  btnPrimary: {
    padding: "4px 12px",
    background: "var(--havn-accent)",
    color: "#fff",
    border: "1px solid var(--havn-accent)",
    borderRadius: "var(--havn-radius-lg)",
    cursor: "pointer",
    fontSize: 11,
    fontWeight: 500,
  },
  btnDanger: {
    padding: "4px 10px",
    background: "transparent",
    color: "var(--havn-red)",
    border: "1px solid color-mix(in srgb, var(--havn-red) 40%, transparent)",
    borderRadius: "var(--havn-radius-lg)",
    cursor: "pointer",
    fontSize: 11,
    fontWeight: 500,
  },
  errorBanner: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
    padding: "8px 12px",
    marginBottom: 12,
    background: "color-mix(in srgb, var(--havn-red) 10%, transparent)",
    color: "var(--havn-red)",
    border: "1px solid color-mix(in srgb, var(--havn-red) 30%, transparent)",
    borderRadius: 4,
    fontSize: 12,
  },
  errorClose: {
    background: "transparent",
    border: "none",
    color: "var(--havn-red)",
    cursor: "pointer",
    fontSize: 16,
    lineHeight: 1,
    padding: 0,
  },
  emptyState: {
    padding: "40px 20px",
    color: "var(--havn-text-dim)",
    textAlign: "center",
    fontSize: 13,
    lineHeight: 1.6,
  },
  emptyTitle: {
    fontSize: 14,
    fontWeight: 600,
    color: "var(--havn-text-secondary)",
    marginBottom: 6,
  },
  emptyText: {
    fontSize: 12,
    color: "var(--havn-text-dim)",
    lineHeight: 1.6,
    maxWidth: 380,
    margin: "0 auto",
  },
  emptyCode: {
    fontSize: 11,
    fontFamily: "var(--havn-font-mono)",
    background: "var(--havn-bg-tertiary)",
    padding: "1px 5px",
    borderRadius: 3,
  },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: {
    textAlign: "left",
    padding: "8px 12px",
    borderBottom: "1px solid var(--havn-border-light)",
    color: "var(--havn-text-secondary)",
    fontSize: 11,
    textTransform: "uppercase",
    letterSpacing: "0.3px",
    background: "var(--havn-bg-secondary)",
  },
  td: {
    padding: "10px 12px",
    borderBottom: "1px solid var(--havn-border)",
    color: "var(--havn-text)",
    verticalAlign: "top",
  },
  chip: {
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: 4,
    fontSize: 11,
    fontWeight: 500,
  },
  codeSm: {
    fontFamily: "var(--havn-font-mono)",
    fontSize: 11,
    background: "color-mix(in srgb, var(--havn-text) 6%, transparent)",
    padding: "1px 5px",
    borderRadius: 3,
  },

  /* Form row */
  formLabel: { display: "flex", flexDirection: "column", gap: 4 },
  formLabelText: {
    fontSize: 11,
    color: "var(--havn-text-secondary)",
    textTransform: "uppercase",
    letterSpacing: "0.3px",
    fontWeight: 600,
  },
  input: {
    width: "100%",
    padding: "6px 10px",
    background: "var(--havn-bg-tertiary)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
    color: "var(--havn-text)",
    fontSize: 13,
    boxSizing: "border-box",
  },
  chipBox: {
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
    padding: "8px 10px",
    background: "var(--havn-bg-tertiary)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
    minHeight: 38,
  },
  targetChip: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "3px 4px 3px 8px",
    background: "color-mix(in srgb, var(--havn-accent) 15%, transparent)",
    color: "var(--havn-accent)",
    border: "1px solid color-mix(in srgb, var(--havn-accent) 30%, transparent)",
    borderRadius: 4,
    fontSize: 11,
  },
  chipRemove: {
    background: "transparent",
    border: "none",
    color: "inherit",
    cursor: "pointer",
    fontSize: 13,
    lineHeight: 1,
    padding: "0 4px",
  },
  scheduleRow: {
    display: "flex",
    gap: 8,
    alignItems: "flex-start",
    padding: 8,
    background: "var(--havn-bg-tertiary)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
  },
  tagBadge: {
    display: "inline-flex",
    alignItems: "center",
    gap: 2,
    padding: "2px 6px",
    background: "color-mix(in srgb, var(--havn-accent) 12%, transparent)",
    color: "var(--havn-accent)",
    border: "1px solid color-mix(in srgb, var(--havn-accent) 25%, transparent)",
    borderRadius: 10,
    fontSize: 10,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "var(--havn-font)",
  },
  selectorBtn: {
    width: 16,
    height: 16,
    padding: 0,
    border: "1px solid color-mix(in srgb, var(--havn-accent) 30%, transparent)",
    borderRadius: 3,
    cursor: "pointer",
    fontSize: 10,
    fontWeight: 700,
    lineHeight: 1,
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
  },

  /* Modal shared */
  modalOverlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0, 0, 0, 0.5)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  modalCard: {
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 8,
    padding: 0,
    width: "min(820px, 92vw)",
    maxHeight: "92vh",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    color: "var(--havn-text)",
  },
  modalHeader: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "12px 16px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
  },
  modalActions: {
    display: "flex",
    justifyContent: "flex-end",
    gap: 8,
    padding: "12px 16px",
    borderTop: "1px solid var(--havn-border)",
    flexShrink: 0,
  },

  /* Cron wizard */
  cronTabs: {
    display: "flex",
    gap: 0,
    padding: "0 16px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
    overflowX: "auto",
  },
  cronTab: {
    padding: "8px 14px",
    background: "transparent",
    border: "none",
    borderBottom: "2px solid transparent",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: 12,
    whiteSpace: "nowrap",
  },
  cronTabActive: {
    color: "var(--havn-text)",
    borderBottom: "2px solid var(--havn-accent)",
  },
  cronBody: {
    flex: 1,
    overflow: "auto",
    padding: "8px 16px",
    minHeight: 260,
  },
  cronPreview: {
    display: "grid",
    gridTemplateColumns: "1fr 260px",
    gap: 16,
    padding: "12px 16px",
    borderTop: "1px solid var(--havn-border)",
    background: "var(--havn-bg-secondary)",
    flexShrink: 0,
  },
  cronExprBox: { display: "flex", flexDirection: "column", gap: 4 },
  cronExprCode: {
    fontFamily: "var(--havn-font-mono)",
    fontSize: 15,
    padding: "6px 10px",
    background: "var(--havn-bg-tertiary)",
    borderRadius: 4,
    color: "var(--havn-accent)",
    display: "inline-block",
    width: "fit-content",
  },
  cronExprDesc: {
    fontSize: 13,
    color: "var(--havn-text)",
    fontStyle: "italic",
    marginTop: 4,
  },
  cronNextRuns: {
    borderLeft: "1px solid var(--havn-border)",
    paddingLeft: 12,
  },

  /* DAG picker */
  dagHint: {
    fontSize: 11,
    color: "var(--havn-text-dim)",
    padding: "8px 16px",
    background: "var(--havn-bg-secondary)",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
  },
  dagCanvas: {
    flex: 1,
    overflow: "auto",
    background: "var(--havn-bg)",
  },
  dagColumn: {
    flexShrink: 0,
    minWidth: 200,
    maxWidth: 260,
  },
  dagColumnHeader: {
    fontSize: 11,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.5px",
    padding: "6px 0",
    borderBottom: "1px solid var(--havn-border)",
    marginBottom: 8,
  },
  dagBox: {
    display: "flex",
    alignItems: "stretch",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    overflow: "hidden",
    cursor: "pointer",
    transition: "border-color 0.1s, background 0.1s",
  },
  dagZoneLeft: {
    width: 20,
    background: "color-mix(in srgb, var(--havn-text) 4%, transparent)",
    border: "none",
    borderRight: "1px solid var(--havn-border)",
    color: "var(--havn-text-dim)",
    cursor: "pointer",
    fontSize: 10,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  dagZoneRight: {
    width: 20,
    background: "color-mix(in srgb, var(--havn-text) 4%, transparent)",
    border: "none",
    borderLeft: "1px solid var(--havn-border)",
    color: "var(--havn-text-dim)",
    cursor: "pointer",
    fontSize: 10,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
  },
  dagBoxCenter: {
    flex: 1,
    padding: "6px 8px",
    userSelect: "none",
  },
  dagSelectionSummary: {
    display: "flex",
    gap: 12,
    padding: "10px 16px",
    borderTop: "1px solid var(--havn-border)",
    background: "var(--havn-bg-secondary)",
    flexShrink: 0,
    maxHeight: 160,
    overflow: "auto",
  },
  selectedChip: {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "2px 4px 2px 6px",
    background: "color-mix(in srgb, var(--havn-accent) 12%, transparent)",
    color: "var(--havn-accent)",
    border: "1px solid color-mix(in srgb, var(--havn-accent) 25%, transparent)",
    borderRadius: 3,
    fontSize: 11,
  },
};

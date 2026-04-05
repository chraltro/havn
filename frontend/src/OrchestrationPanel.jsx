import React, { useState, useEffect, useCallback, useMemo } from "react";
import { api } from "./api";

/* --------------------------------------------------------------- */
/* Helpers                                                          */
/* --------------------------------------------------------------- */

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
  if (ms == null) return "\u2014";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const min = Math.floor(ms / 60000);
  const sec = Math.floor((ms % 60000) / 1000);
  return `${min}m ${sec}s`;
}

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

function statusIcon(status) {
  if (status === "success") return "\u2713";
  if (status === "failure" || status === "error") return "\u2717";
  if (status === "running") return "\u25CB";
  if (status === "timeout") return "\u23F0";
  if (status === "cancelled") return "\u00D7";
  return "\u2013";
}

const TYPE_BADGE_CFG = {
  ingest:    { bg: "color-mix(in srgb, cyan 18%, transparent)",               color: "cyan",               label: "ING" },
  transform: { bg: "color-mix(in srgb, var(--havn-accent) 18%, transparent)", color: "var(--havn-accent)", label: "TRF" },
  export:    { bg: "color-mix(in srgb, orchid 18%, transparent)",              color: "orchid",             label: "EXP" },
};

function TypeBadge({ type }) {
  const cfg = TYPE_BADGE_CFG[type] || { bg: "transparent", color: "var(--havn-text-secondary)", label: (type || "?").toUpperCase() };
  return <span style={{ ...s.chip, background: cfg.bg, color: cfg.color }}>{cfg.label}</span>;
}

function StatusBadge({ status }) {
  return (
    <span style={{ ...s.chip, ...statusStyle(status) }}>
      {statusIcon(status)} {status}
    </span>
  );
}

/* --------------------------------------------------------------- */
/* Cron wizard helpers                                              */
/* --------------------------------------------------------------- */

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function parseCronField(field, min, max) {
  if (field === "*") return { type: "all" };
  if (/^\*\/(\d+)$/.test(field)) {
    const step = parseInt(field.slice(2));
    return { type: "step", step };
  }
  if (/^\d+$/.test(field)) {
    const val = parseInt(field);
    if (val >= min && val <= max) return { type: "exact", val };
  }
  return null; // complex or invalid
}

/**
 * Lightweight cron-to-English converter.
 * Handles 5-field standard cron: minute hour dom month dow
 * POSIX weekdays: 0=Sunday, 1=Monday, ..., 6=Saturday
 */
function describeCron(expr) {
  if (!expr || !expr.trim()) return "";
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return "Invalid cron expression (expected 5 fields)";

  const [minF, hourF, domF, monF, dowF] = parts;

  const min  = parseCronField(minF,  0, 59);
  const hour = parseCronField(hourF, 0, 23);
  const dom  = parseCronField(domF,  1, 31);
  const mon  = parseCronField(monF,  1, 12);
  const dow  = parseCronField(dowF,  0,  6);

  if (!min || !hour || !dom || !mon || !dow) {
    return `Runs on: ${expr}`;
  }

  // Build time string
  let timeStr = "";
  if (min.type === "exact" && hour.type === "exact") {
    const h = hour.val;
    const m = min.val;
    const ampm = h < 12 ? "AM" : "PM";
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
    timeStr = `at ${h12}:${String(m).padStart(2, "0")} ${ampm}`;
  } else if (min.type === "step" && hourF === "*") {
    return `Every ${min.step} minute${min.step === 1 ? "" : "s"}`;
  } else if (minF === "0" && hourF === "*") {
    return "Every hour";
  } else if (hour.type === "step" && minF === "0") {
    return `Every ${hour.step} hour${hour.step === 1 ? "" : "s"}`;
  } else if (hour.type === "step") {
    return `Every ${hour.step} hour${hour.step === 1 ? "" : "s"} at minute ${min.type === "exact" ? min.val : minF}`;
  } else if (min.type === "step") {
    return `Every ${min.step} minutes`;
  } else {
    timeStr = `at ${hourF}:${minF}`;
  }

  // Day-of-week specific
  if (dowF !== "*" && domF === "*") {
    if (dow.type === "exact") {
      const dayName = DAYS[dow.val] || `weekday ${dow.val}`;
      const monthStr = mon.type === "all" ? "" : ` in ${MONTHS[(mon.val || 1) - 1]}`;
      return `Every ${dayName} ${timeStr}${monthStr}`;
    }
    if (dow.type === "step") {
      return `Every ${dow.step} days (by weekday) ${timeStr}`;
    }
  }

  // DOM specific, no DOW
  if (domF !== "*" && dowF === "*") {
    if (dom.type === "exact") {
      const monthStr = mon.type === "all" ? "every month" : `every ${MONTHS[(mon.val || 1) - 1]}`;
      const suffix = dom.val === 1 ? "1st" : ordinal(dom.val);
      return `On the ${suffix} of ${monthStr} ${timeStr}`;
    }
    if (dom.type === "step") {
      return `Every ${dom.step} days ${timeStr}`;
    }
  }

  // Every day
  if (domF === "*" && dowF === "*") {
    const monthStr = mon.type === "all" ? "" : ` in ${MONTHS[(mon.val || 1) - 1]}`;
    return `Every day ${timeStr}${monthStr}`;
  }

  // Fallback with partial info
  return `Runs on: ${expr}`;
}

const CRON_PRESETS = [
  { label: "Every 15 min",     value: "*/15 * * * *" },
  { label: "Every hour",       value: "0 * * * *" },
  { label: "Every day 6am",    value: "0 6 * * *" },
  { label: "Every Monday",     value: "0 9 * * 1" },
  { label: "1st of month",     value: "0 0 1 * *" },
];

/* --------------------------------------------------------------- */
/* DAG picker helpers                                               */
/* --------------------------------------------------------------- */

/** Walk a FileEntry tree to collect files matching a predicate. */
function collectFiles(entries, predicate, acc = []) {
  if (!Array.isArray(entries)) return acc;
  for (const entry of entries) {
    if (entry.type === "file" && predicate(entry.path)) {
      acc.push(entry.path);
    } else if (entry.type === "directory" && entry.children) {
      collectFiles(entry.children, predicate, acc);
    }
  }
  return acc;
}

function isIngestFile(path) {
  return /^ingest\/.+\.(py|dpnb)$/.test(path) && !path.includes("/_");
}

function isExportFile(path) {
  return /^export\/.+\.(py|dpnb)$/.test(path) && !path.includes("/_");
}

function groupNodesBySchema(nodes) {
  const schemas = {};
  for (const n of nodes || []) {
    const schema = n.schema || (n.id && n.id.includes(".") ? n.id.split(".")[0] : "other");
    if (!schemas[schema]) schemas[schema] = [];
    schemas[schema][schemas[schema].length] = n;
  }
  return schemas;
}

/* --------------------------------------------------------------- */
/* Root component                                                   */
/* --------------------------------------------------------------- */

export default function OrchestrationPanel() {
  const [subTab, setSubTab] = useState("plan-jobs");

  return (
    <div style={s.container}>
      {/* Header with stat cards + tab bar (mirrors QualityPanel header) */}
      <div style={s.header}>
        <div style={s.tabs}>
          {["plan-jobs", "job-results"].map((t) => (
            <div
              key={t}
              style={{ ...s.tab, ...(subTab === t ? s.tabActive : {}) }}
              onClick={() => setSubTab(t)}
            >
              {t === "plan-jobs" ? "Plan Jobs" : "Job Results"}
            </div>
          ))}
        </div>
      </div>
      {subTab === "plan-jobs"    && <PlanJobsTab onSwitchToResults={() => setSubTab("job-results")} />}
      {subTab === "job-results"  && <JobResultsTab />}
    </div>
  );
}

/* --------------------------------------------------------------- */
/* Plan Jobs Tab                                                    */
/* --------------------------------------------------------------- */

const EMPTY_FORM = {
  name: "",
  target: "",
  resolve: "upstream",
  cron: "",
  enabled: true,
  retry: 0,
  retry_delay: 10,
  timeout_minutes: 60,
  description: "",
  notify: [],
};

function PlanJobsTab({ onSwitchToResults }) {
  const [jobs, setJobs]               = useState([]);
  const [loading, setLoading]         = useState(true);
  const [error, setError]             = useState(null);
  const [showForm, setShowForm]       = useState(false);
  const [editJob, setEditJob]         = useState(null);
  const [previewData, setPreviewData] = useState(null);
  const [form, setForm]               = useState(EMPTY_FORM);
  const [saving, setSaving]           = useState(false);

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
    setEditJob(null);
    setForm(EMPTY_FORM);
    setPreviewData(null);
    setShowForm(true);
  };

  const openEdit = (job) => {
    setEditJob(job);
    setForm({
      name:            job.name            || "",
      target:          job.target          || "",
      resolve:         job.resolve         || "upstream",
      cron:            job.cron            || "",
      enabled:         job.enabled !== false,
      retry:           job.retry           || 0,
      retry_delay:     job.retry_delay     || 10,
      timeout_minutes: job.timeout_minutes || 60,
      description:     job.description     || "",
      notify:          job.notify          || [],
    });
    setPreviewData(null);
    setShowForm(true);
  };

  const handleCancel = () => {
    setShowForm(false);
    setEditJob(null);
    setPreviewData(null);
  };

  const handleSave = async () => {
    if (!form.name.trim() || !form.target.trim()) {
      setError("Name and target are required");
      return;
    }
    setSaving(true);
    try {
      if (editJob) {
        await api.updateJob(editJob.name, form);
      } else {
        await api.createJob(form);
      }
      setShowForm(false);
      setEditJob(null);
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

  const handlePreview = async (name) => {
    try {
      const plan = await api.getJobPlan(name);
      setPreviewData({ name, plan });
      // If form is not showing, show preview inline below table
    } catch (e) {
      setError(e.message || "Failed to get plan");
    }
  };

  const handleDelete = async (name) => {
    if (!window.confirm(`Delete job "${name}"?`)) return;
    try {
      await api.deleteJob(name);
      await loadJobs();
    } catch (e) {
      setError(e.message || "Failed to delete job");
    }
  };

  return (
    <div style={s.content}>
      {/* Toolbar */}
      <div style={s.toolbar}>
        <button style={s.btnPrimary} onClick={openCreate} disabled={showForm}>
          + New Job
        </button>
        <button style={s.btn} onClick={loadJobs} disabled={loading}>
          {loading ? "Loading\u2026" : "Refresh"}
        </button>
        <span style={s.count}>{jobs.length} job{jobs.length !== 1 ? "s" : ""}</span>
      </div>

      {error && (
        <div style={s.errorBanner}>
          <span>{error}</span>
          <button style={s.errorClose} onClick={() => setError(null)}>\u00D7</button>
        </div>
      )}

      {/* Inline new/edit form */}
      {showForm && (
        <JobForm
          form={form}
          setForm={setForm}
          editJob={editJob}
          saving={saving}
          onSave={handleSave}
          onCancel={handleCancel}
        />
      )}

      {/* Jobs table */}
      {loading && jobs.length === 0 ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>Loading\u2026</div>
        </div>
      ) : jobs.length === 0 ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>No orchestration jobs yet</div>
          <div style={s.emptyText}>
            Click <strong>+ New Job</strong> to define a scheduled pipeline, or create YAML files
            in <code style={s.emptyCode}>orchestration/</code>.
          </div>
        </div>
      ) : (
        <table style={s.table}>
          <thead>
            <tr>
              <th style={s.th}>Name</th>
              <th style={s.th}>Target</th>
              <th style={s.th}>Schedule</th>
              <th style={s.th}>Resolve</th>
              <th style={s.th}>Enabled</th>
              <th style={s.th}>Last Run</th>
              <th style={s.th}>Next Run</th>
              <th style={{ ...s.th, textAlign: "right" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.name}>
                <td style={s.td}>
                  <strong>{job.name}</strong>
                  {job.description && (
                    <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 2 }}>
                      {job.description}
                    </div>
                  )}
                </td>
                <td style={s.td}>
                  <code style={s.mono}>{job.target}</code>
                </td>
                <td style={s.td}>
                  {job.cron ? (
                    <div>
                      <code style={s.mono}>{job.cron}</code>
                      <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 2 }}>
                        {describeCron(job.cron)}
                      </div>
                    </div>
                  ) : (
                    <span style={{ color: "var(--havn-text-dim)" }}>\u2014</span>
                  )}
                </td>
                <td style={s.td}>{job.resolve}</td>
                <td style={s.td}>
                  <label style={s.toggle}>
                    <input
                      type="checkbox"
                      checked={job.enabled}
                      onChange={(e) => handleToggle(job.name, e.target.checked)}
                    />
                    <span style={{ color: job.enabled ? "var(--havn-green)" : "var(--havn-text-dim)" }}>
                      {job.enabled ? "on" : "off"}
                    </span>
                  </label>
                </td>
                <td style={s.td}>
                  {job.last_run ? (
                    <div>
                      <StatusBadge status={job.last_run.status} />
                      <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 2 }}>
                        {timeAgo(job.last_run.started_at)}
                      </div>
                    </div>
                  ) : (
                    <span style={{ color: "var(--havn-text-dim)" }}>never</span>
                  )}
                </td>
                <td style={s.td}>
                  {job.next_run ? (
                    <span style={{ fontSize: 11 }}>{new Date(job.next_run).toLocaleString()}</span>
                  ) : (
                    <span style={{ color: "var(--havn-text-dim)" }}>\u2014</span>
                  )}
                </td>
                <td style={{ ...s.td, textAlign: "right" }}>
                  <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                    <button style={s.btn} onClick={() => handleRun(job.name)}>Run</button>
                    <button style={s.btn} onClick={() => handlePreview(job.name)}>Preview</button>
                    <button style={s.btn} onClick={() => openEdit(job)}>Edit</button>
                    <button style={s.btnDanger} onClick={() => handleDelete(job.name)}>\u00D7</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Inline execution plan preview (shown below the table) */}
      {previewData && !showForm && (
        <div style={s.inlineCard}>
          <div style={s.inlineCardHeader}>
            <span style={{ fontWeight: 600, fontSize: 13 }}>
              Execution Plan: {previewData.name}
            </span>
            <span style={{ fontSize: 12, color: "var(--havn-text-dim)" }}>
              {previewData.plan.total_steps} steps
              {" \u00B7 "}
              {previewData.plan.ingest_count} ingest, {previewData.plan.transform_count} transform,{" "}
              {previewData.plan.export_count} export
              {previewData.plan.total_estimated_ms > 0 && (
                <> {" \u00B7 "} ~{formatDuration(previewData.plan.total_estimated_ms)} est.</>
              )}
            </span>
            <button style={s.btn} onClick={() => setPreviewData(null)}>Close</button>
          </div>
          <div style={{ maxHeight: 320, overflow: "auto" }}>
            {previewData.plan.steps.length === 0 ? (
              <div style={{ padding: "16px 0", textAlign: "center", color: "var(--havn-text-dim)", fontSize: 13 }}>
                No steps in plan
              </div>
            ) : (
              previewData.plan.steps.map((step) => (
                <div key={step.step} style={s.stepRow}>
                  <span style={{ color: "var(--havn-text-dim)", minWidth: 24, fontSize: 12 }}>{step.step}.</span>
                  <TypeBadge type={step.type} />
                  <code style={{ ...s.mono, flex: 1 }}>{step.target}</code>
                  {step.estimated_duration_ms > 0 && (
                    <span style={{ fontSize: 11, color: "var(--havn-text-dim)" }}>
                      ~{formatDuration(step.estimated_duration_ms)}
                    </span>
                  )}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- */
/* Inline job form (new + edit)                                     */
/* --------------------------------------------------------------- */

function JobForm({ form, setForm, editJob, saving, onSave, onCancel }) {
  const [showDagPicker, setShowDagPicker] = useState(false);

  const cronDesc = useMemo(() => describeCron(form.cron), [form.cron]);

  const handleTargetPick = (target) => {
    setForm((f) => ({ ...f, target }));
    setShowDagPicker(false);
  };

  return (
    <div style={s.formRow}>
      <div style={s.formRowHeader}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>
          {editJob ? `Edit Job: ${editJob.name}` : "New Job"}
        </span>
        <div style={{ display: "flex", gap: 6 }}>
          <button style={s.btn} onClick={onCancel}>Cancel</button>
          <button style={s.btnPrimary} onClick={onSave} disabled={saving}>
            {saving ? "Saving\u2026" : editJob ? "Save" : "Create"}
          </button>
        </div>
      </div>

      {showDagPicker ? (
        <DagPicker
          currentTarget={form.target}
          onPick={handleTargetPick}
          onClose={() => setShowDagPicker(false)}
        />
      ) : (
        <div style={s.formGrid}>
          {/* Row 1: Name + Description */}
          <div style={s.formRow2}>
            <label style={s.label}>
              Name
              <input
                style={s.input}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="daily-refresh"
                disabled={!!editJob}
              />
            </label>
            <label style={s.label}>
              Description
              <input
                style={s.input}
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Optional description"
              />
            </label>
          </div>

          {/* Row 2: Target + Resolve */}
          <div style={s.formRow2}>
            <label style={s.label}>
              Target
              <div style={{ display: "flex", gap: 6 }}>
                <input
                  style={{ ...s.input, flex: 1 }}
                  value={form.target}
                  onChange={(e) => setForm({ ...form, target: e.target.value })}
                  placeholder="silver.orders | ingest/orders.py | bronze.*"
                />
                <button
                  style={{ ...s.btn, whiteSpace: "nowrap", flexShrink: 0 }}
                  type="button"
                  onClick={() => setShowDagPicker(true)}
                >
                  Pick from DAG
                </button>
              </div>
            </label>
            <label style={s.label}>
              Resolve
              <select
                style={s.input}
                value={form.resolve}
                onChange={(e) => setForm({ ...form, resolve: e.target.value })}
              >
                <option value="upstream">upstream (include deps)</option>
                <option value="none">none (target only)</option>
              </select>
            </label>
          </div>

          {/* Cron wizard */}
          <label style={s.label}>
            Cron Schedule
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 4 }}>
              {CRON_PRESETS.map((p) => (
                <button
                  key={p.value}
                  type="button"
                  style={{
                    ...s.btn,
                    fontSize: 11,
                    padding: "2px 8px",
                    background: form.cron === p.value
                      ? "color-mix(in srgb, var(--havn-accent) 20%, transparent)"
                      : undefined,
                    borderColor: form.cron === p.value ? "var(--havn-accent)" : undefined,
                    color: form.cron === p.value ? "var(--havn-accent)" : undefined,
                  }}
                  onClick={() => setForm({ ...form, cron: p.value })}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <input
              style={s.input}
              value={form.cron}
              onChange={(e) => setForm({ ...form, cron: e.target.value })}
              placeholder="0 6 * * * (optional)"
            />
            {form.cron && (
              <div style={s.cronDesc}>
                {cronDesc}
              </div>
            )}
          </label>

          {/* Row 3: Retry / Retry delay / Timeout / Enabled */}
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ ...s.label, flex: "0 0 80px" }}>
              Retry
              <input
                type="number" min="0" max="10"
                style={s.input}
                value={form.retry}
                onChange={(e) => setForm({ ...form, retry: parseInt(e.target.value) || 0 })}
              />
            </label>
            <label style={{ ...s.label, flex: "0 0 110px" }}>
              Retry Delay (s)
              <input
                type="number" min="0"
                style={s.input}
                value={form.retry_delay}
                onChange={(e) => setForm({ ...form, retry_delay: parseInt(e.target.value) || 0 })}
              />
            </label>
            <label style={{ ...s.label, flex: "0 0 110px" }}>
              Timeout (min)
              <input
                type="number" min="1"
                style={s.input}
                value={form.timeout_minutes}
                onChange={(e) => setForm({ ...form, timeout_minutes: parseInt(e.target.value) || 60 })}
              />
            </label>
            <label style={{ ...s.label, flexDirection: "row", alignItems: "center", gap: 6, paddingBottom: 2 }}>
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              />
              Enabled
            </label>
          </div>
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- */
/* DAG picker                                                       */
/* --------------------------------------------------------------- */

function DagPicker({ currentTarget, onPick, onClose }) {
  const [dagNodes, setDagNodes]         = useState([]);
  const [ingestFiles, setIngestFiles]   = useState([]);
  const [exportFiles, setExportFiles]   = useState([]);
  const [loading, setLoading]           = useState(true);
  const [error, setError]               = useState(null);
  const [filter, setFilter]             = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const [dagResult, filesResult] = await Promise.allSettled([
          api.getDAG(),
          api.listFiles(),
        ]);

        if (cancelled) return;

        if (dagResult.status === "fulfilled") {
          const dag = dagResult.value;
          const nodes = Array.isArray(dag.nodes) ? dag.nodes : [];
          setDagNodes(nodes);
        }

        if (filesResult.status === "fulfilled") {
          const files = filesResult.value || [];
          setIngestFiles(collectFiles(files, isIngestFile));
          setExportFiles(collectFiles(files, isExportFile));
        }
      } catch (e) {
        if (!cancelled) setError(e.message || "Failed to load DAG");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  const schemaGroups = useMemo(() => groupNodesBySchema(dagNodes), [dagNodes]);

  const lf = filter.toLowerCase();
  const filteredIngest = ingestFiles.filter(p => p.toLowerCase().includes(lf));
  const filteredExport = exportFiles.filter(p => p.toLowerCase().includes(lf));
  const filteredSchemas = useMemo(() => {
    const result = {};
    for (const [schema, nodes] of Object.entries(schemaGroups)) {
      const filtered = nodes.filter(n =>
        (n.id || "").toLowerCase().includes(lf) ||
        (n.name || "").toLowerCase().includes(lf) ||
        schema.toLowerCase().includes(lf)
      );
      if (filtered.length > 0) result[schema] = filtered;
    }
    return result;
  }, [schemaGroups, lf]);

  if (loading) {
    return (
      <div style={s.dagPicker}>
        <div style={{ padding: 20, color: "var(--havn-text-dim)", fontSize: 13 }}>Loading DAG\u2026</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={s.dagPicker}>
        <div style={s.errorBanner}>
          <span>{error}</span>
          <button style={s.btn} onClick={onClose}>Close</button>
        </div>
      </div>
    );
  }

  return (
    <div style={s.dagPicker}>
      {/* Picker header */}
      <div style={s.dagPickerHeader}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>Pick a Target</span>
        <input
          style={{ ...s.input, width: 200, fontSize: 12 }}
          placeholder="Filter\u2026"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          autoFocus
        />
        <button style={s.btn} onClick={onClose}>Cancel</button>
      </div>

      <div style={{ maxHeight: 380, overflow: "auto" }}>
        {/* Ingest scripts */}
        {filteredIngest.length > 0 && (
          <div style={s.dagGroup}>
            <div style={{ ...s.dagGroupHeader, color: "cyan" }}>
              Ingest Scripts ({filteredIngest.length})
            </div>
            {filteredIngest.map((path) => (
              <div
                key={path}
                style={{ ...s.dagItem, background: currentTarget === path ? "color-mix(in srgb, cyan 10%, transparent)" : undefined }}
                onClick={() => onPick(path)}
              >
                <span style={{ ...s.chip, background: "color-mix(in srgb, cyan 18%, transparent)", color: "cyan", fontSize: 10 }}>ING</span>
                <code style={s.mono}>{path}</code>
              </div>
            ))}
          </div>
        )}

        {/* Transform models grouped by schema */}
        {Object.entries(filteredSchemas).map(([schema, nodes]) => (
          <div key={schema} style={s.dagGroup}>
            <div
              style={{ ...s.dagGroupHeader, color: "var(--havn-accent)", cursor: "pointer" }}
              title={`Click to target all ${schema} models`}
              onClick={() => onPick(`${schema}.*`)}
            >
              {schema.toUpperCase()} ({nodes.length})
              <span style={{ fontSize: 10, fontWeight: 400, color: "var(--havn-text-dim)", marginLeft: 6 }}>
                click to select all ({schema}.*)
              </span>
            </div>
            {nodes.map((n) => {
              const modelId = n.id || `${schema}.${n.name}`;
              return (
                <div
                  key={modelId}
                  style={{ ...s.dagItem, background: currentTarget === modelId ? "color-mix(in srgb, var(--havn-accent) 10%, transparent)" : undefined }}
                  onClick={() => onPick(modelId)}
                >
                  <span style={{ ...s.chip, background: "color-mix(in srgb, var(--havn-accent) 18%, transparent)", color: "var(--havn-accent)", fontSize: 10 }}>TRF</span>
                  <code style={s.mono}>{modelId}</code>
                  {n.materialized && (
                    <span style={{ fontSize: 10, color: "var(--havn-text-dim)", marginLeft: 4 }}>{n.materialized}</span>
                  )}
                </div>
              );
            })}
          </div>
        ))}

        {/* Export scripts */}
        {filteredExport.length > 0 && (
          <div style={s.dagGroup}>
            <div style={{ ...s.dagGroupHeader, color: "orchid" }}>
              Export Scripts ({filteredExport.length})
            </div>
            {filteredExport.map((path) => (
              <div
                key={path}
                style={{ ...s.dagItem, background: currentTarget === path ? "color-mix(in srgb, orchid 10%, transparent)" : undefined }}
                onClick={() => onPick(path)}
              >
                <span style={{ ...s.chip, background: "color-mix(in srgb, orchid 18%, transparent)", color: "orchid", fontSize: 10 }}>EXP</span>
                <code style={s.mono}>{path}</code>
              </div>
            ))}
          </div>
        )}

        {filteredIngest.length === 0 && Object.keys(filteredSchemas).length === 0 && filteredExport.length === 0 && (
          <div style={{ padding: "24px 16px", textAlign: "center", color: "var(--havn-text-dim)", fontSize: 13 }}>
            {filter ? "No matches. Try a different filter." : "No DAG nodes found. Run havn transform to build models."}
          </div>
        )}
      </div>

      {currentTarget && (
        <div style={s.dagPickerFooter}>
          <span style={{ fontSize: 12, color: "var(--havn-text-dim)" }}>
            Selected: <code style={s.mono}>{currentTarget}</code>
            {" \u2014 "}
            Click &quot;Preview Plan&quot; after saving to see the full execution plan.
          </span>
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- */
/* Job Results Tab                                                  */
/* --------------------------------------------------------------- */

function JobResultsTab() {
  const [runs, setRuns]               = useState([]);
  const [loading, setLoading]         = useState(true);
  const [selectedRun, setSelectedRun] = useState(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [error, setError]             = useState(null);

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

  // Poll for running jobs every 3s
  useEffect(() => {
    const hasRunning = runs.some((r) => r.status === "running");
    if (!hasRunning) return;
    const id = setInterval(loadRuns, 3000);
    return () => clearInterval(id);
  }, [runs, loadRuns]);

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
          {loading ? "Loading\u2026" : "Refresh"}
        </button>
        <span style={s.count}>{filteredRuns.length} of {runs.length} run{runs.length !== 1 ? "s" : ""}</span>
      </div>

      {error && (
        <div style={s.errorBanner}>
          <span>{error}</span>
          <button style={s.errorClose} onClick={() => setError(null)}>\u00D7</button>
        </div>
      )}

      {loading && runs.length === 0 ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>Loading job runs\u2026</div>
        </div>
      ) : filteredRuns.length === 0 ? (
        <div style={s.emptyState}>
          <div style={s.emptyTitle}>No job runs yet</div>
          <div style={s.emptyText}>
            Trigger a job manually from the Plan Jobs tab, or wait for a scheduled run.
          </div>
        </div>
      ) : (
        <table style={s.table}>
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
                <td style={s.td}><strong>{run.job_name}</strong></td>
                <td style={s.td}><StatusBadge status={run.status} /></td>
                <td style={s.td}>
                  <span>{run.steps_completed}/{run.steps_total}</span>
                  {run.steps_failed > 0 && (
                    <span style={{ color: "var(--havn-red)", marginLeft: 6 }}>
                      ({run.steps_failed} failed)
                    </span>
                  )}
                  {run.steps_skipped > 0 && (
                    <span style={{ color: "var(--havn-text-dim)", marginLeft: 6 }}>
                      ({run.steps_skipped} skipped)
                    </span>
                  )}
                </td>
                <td style={s.td}>{formatDuration(run.duration_ms)}</td>
                <td style={s.td}>{run.trigger || "\u2014"}</td>
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

/* --------------------------------------------------------------- */
/* Job run detail view                                              */
/* --------------------------------------------------------------- */

function JobRunDetail({ run, onBack, onRerun }) {
  const completed = run.steps_completed || 0;
  const failed    = run.steps_failed    || 0;
  const total     = run.steps_total     || 0;
  const skipped   = run.steps_skipped   || 0;

  const handleRerun = async () => {
    try {
      await api.runJob(run.job_name);
      if (onRerun) onRerun();
      onBack();
    } catch (e) {
      alert(e.message || "Failed to rerun job");
    }
  };

  return (
    <div style={s.content}>
      <div style={{ ...s.toolbar, alignItems: "center" }}>
        <button style={s.btn} onClick={onBack}>\u2190 Back</button>
        <button style={s.btnPrimary} onClick={handleRerun}>Rerun</button>
      </div>

      {/* Run summary card */}
      <div style={s.detailCard}>
        <div>
          <div style={{ fontSize: 16, fontWeight: 600 }}>{run.job_name}</div>
          <div style={{ fontSize: 11, color: "var(--havn-text-dim)", marginTop: 4 }}>
            {run.started_at && new Date(run.started_at).toLocaleString()}
            {" \u00B7 "}
            {run.trigger || "manual"}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <StatusBadge status={run.status} />
          <div style={{ fontSize: 12, color: "var(--havn-text-dim)", marginTop: 4 }}>
            {formatDuration(run.duration_ms)}
          </div>
        </div>
      </div>

      <div style={s.summaryBar}>
        <strong>{completed}</strong>/<strong>{total}</strong> completed
        {failed > 0 && <span style={{ color: "var(--havn-red)", marginLeft: 10 }}>{failed} failed</span>}
        {skipped > 0 && <span style={{ color: "var(--havn-text-dim)", marginLeft: 10 }}>{skipped} skipped</span>}
      </div>

      {run.error && <div style={s.errorBanner}>{run.error}</div>}

      <div style={{ border: "1px solid var(--havn-border)", borderRadius: 6, overflow: "hidden" }}>
        {(run.step_details || []).length === 0 ? (
          <div style={{ padding: 16, textAlign: "center", color: "var(--havn-text-dim)", fontSize: 13 }}>
            No steps recorded
          </div>
        ) : (
          (run.step_details || []).map((step, i) => <StepDetailRow key={i} step={step} />)
        )}
      </div>
    </div>
  );
}

function StepDetailRow({ step }) {
  const [expanded, setExpanded] = useState(false);
  const hasError = step.status === "error" && step.error;

  return (
    <div style={{ borderBottom: "1px solid var(--havn-border)" }}>
      <div
        style={{ ...s.stepRow, cursor: hasError ? "pointer" : "default" }}
        onClick={() => hasError && setExpanded(!expanded)}
      >
        <span style={{ color: "var(--havn-text-dim)", minWidth: 24, fontSize: 12 }}>{step.step}.</span>
        <TypeBadge type={step.type} />
        <code style={{ ...s.mono, flex: 1 }}>{step.target}</code>
        <StatusBadge status={step.status} />
        {step.duration_ms != null && (
          <span style={{ fontSize: 11, color: "var(--havn-text-dim)", minWidth: 56, textAlign: "right" }}>
            {formatDuration(step.duration_ms)}
          </span>
        )}
        {step.rows_affected != null && step.rows_affected > 0 && (
          <span style={{ fontSize: 11, color: "var(--havn-text-dim)" }}>
            {step.rows_affected.toLocaleString()} rows
          </span>
        )}
      </div>
      {expanded && hasError && (
        <div style={s.errorDetail}>{step.error}</div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- */
/* Styles — aligned with QualityPanel visual language              */
/* --------------------------------------------------------------- */

const s = {
  // Layout
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
  // Tab bar (matches QualityPanel — bottom-border style, not pill style)
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
  content: {
    flex: 1,
    overflow: "auto",
    padding: 20,
  },
  // Toolbar
  toolbar: {
    display: "flex",
    gap: 8,
    marginBottom: 14,
    alignItems: "center",
    flexWrap: "wrap",
  },
  filterSelect: {
    padding: "5px 8px",
    background: "var(--havn-bg-tertiary)",
    color: "var(--havn-text)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: 6,
    fontSize: 12,
    cursor: "pointer",
  },
  count: { fontSize: 12, color: "var(--havn-text-dim)", marginLeft: "auto" },
  // Buttons (matches QualityPanel btn/btnPrimary)
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
  // Table (matches QualityPanel)
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: {
    textAlign: "left",
    padding: "8px 12px",
    borderBottom: "1px solid var(--havn-border-light)",
    color: "var(--havn-text-secondary)",
    fontSize: 11,
    textTransform: "uppercase",
    letterSpacing: "0.3px",
    fontWeight: 600,
  },
  td: {
    padding: "9px 12px",
    borderBottom: "1px solid var(--havn-border)",
    color: "var(--havn-text)",
    verticalAlign: "top",
  },
  // Badges / chips
  chip: {
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: 4,
    fontSize: 11,
    fontWeight: 600,
    whiteSpace: "nowrap",
  },
  // Inline form row
  formRow: {
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: 8,
    padding: "16px 20px",
    marginBottom: 16,
  },
  formRowHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 16,
  },
  formGrid: {
    display: "flex",
    flexDirection: "column",
    gap: 14,
  },
  formRow2: {
    display: "grid",
    gridTemplateColumns: "1fr 1fr",
    gap: 12,
  },
  label: {
    display: "flex",
    flexDirection: "column",
    gap: 5,
    fontSize: 12,
    color: "var(--havn-text-secondary)",
    fontWeight: 500,
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
  cronDesc: {
    fontSize: 12,
    color: "var(--havn-accent)",
    marginTop: 4,
    fontStyle: "italic",
  },
  toggle: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    cursor: "pointer",
    fontSize: 12,
  },
  // DAG picker
  dagPicker: {
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    background: "var(--havn-bg-tertiary)",
    overflow: "hidden",
  },
  dagPickerHeader: {
    display: "flex",
    gap: 10,
    alignItems: "center",
    padding: "10px 14px",
    borderBottom: "1px solid var(--havn-border)",
    background: "var(--havn-bg-secondary)",
  },
  dagPickerFooter: {
    padding: "8px 14px",
    borderTop: "1px solid var(--havn-border)",
    background: "var(--havn-bg-secondary)",
  },
  dagGroup: {
    borderBottom: "1px solid var(--havn-border)",
  },
  dagGroupHeader: {
    padding: "6px 14px",
    fontSize: 11,
    fontWeight: 700,
    textTransform: "uppercase",
    letterSpacing: "0.4px",
    background: "color-mix(in srgb, var(--havn-text) 3%, transparent)",
  },
  dagItem: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "6px 14px 6px 22px",
    cursor: "pointer",
    fontSize: 13,
    borderBottom: "1px solid color-mix(in srgb, var(--havn-border) 50%, transparent)",
    transition: "background 0.1s",
  },
  // Inline preview card
  inlineCard: {
    marginTop: 16,
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    overflow: "hidden",
  },
  inlineCardHeader: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "10px 14px",
    background: "var(--havn-bg-secondary)",
    borderBottom: "1px solid var(--havn-border)",
    flexWrap: "wrap",
  },
  // Step rows in plan/detail
  stepRow: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "8px 12px",
    fontSize: 13,
  },
  // Detail card (run summary)
  detailCard: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    padding: "12px 16px",
    border: "1px solid var(--havn-border)",
    borderRadius: 6,
    marginBottom: 12,
    background: "var(--havn-bg-secondary)",
  },
  summaryBar: {
    padding: "8px 16px",
    fontSize: 13,
    marginBottom: 12,
    background: "color-mix(in srgb, var(--havn-text) 4%, transparent)",
    borderRadius: 6,
  },
  // Error states
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
    borderRadius: 6,
    fontSize: 12,
  },
  errorClose: {
    background: "transparent",
    border: "none",
    color: "var(--havn-red)",
    cursor: "pointer",
    fontSize: 14,
    padding: "0 4px",
    flexShrink: 0,
  },
  errorDetail: {
    padding: "8px 16px 10px 56px",
    fontFamily: "var(--havn-font-mono)",
    fontSize: 11,
    color: "var(--havn-red)",
    whiteSpace: "pre-wrap",
    background: "color-mix(in srgb, var(--havn-red) 4%, transparent)",
  },
  // Empty states (matches QualityPanel)
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
  // Mono code
  mono: {
    fontFamily: "var(--havn-font-mono)",
    fontSize: 12,
    background: "color-mix(in srgb, var(--havn-text) 6%, transparent)",
    padding: "2px 6px",
    borderRadius: 3,
  },
};

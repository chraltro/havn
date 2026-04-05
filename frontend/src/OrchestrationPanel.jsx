import React, { useState, useEffect, useCallback } from "react";
import { api } from "./api";

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
  if (ms == null) return "-";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const min = Math.floor(ms / 60000);
  const sec = Math.floor((ms % 60000) / 1000);
  return `${min}m ${sec}s`;
}

function statusStyle(status) {
  if (status === "success") {
    return { background: "color-mix(in srgb, var(--havn-green) 12%, transparent)", color: "var(--havn-green)" };
  }
  if (status === "failure" || status === "error") {
    return { background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)" };
  }
  if (status === "running") {
    return { background: "color-mix(in srgb, var(--havn-accent) 12%, transparent)", color: "var(--havn-accent)" };
  }
  if (status === "timeout") {
    return { background: "color-mix(in srgb, orange 12%, transparent)", color: "orange" };
  }
  return { background: "color-mix(in srgb, var(--havn-text-dim) 12%, transparent)", color: "var(--havn-text-secondary)" };
}

function statusIcon(status) {
  if (status === "success") return "\u2713";
  if (status === "failure" || status === "error") return "\u2717";
  if (status === "running") return "\u25CB";
  if (status === "timeout") return "\u23F0";
  return "\u2013";
}

const TYPE_BADGE = {
  ingest: { bg: "color-mix(in srgb, cyan 18%, transparent)", color: "cyan", label: "ING" },
  transform: { bg: "color-mix(in srgb, var(--havn-accent) 18%, transparent)", color: "var(--havn-accent)", label: "TRF" },
  export: { bg: "color-mix(in srgb, orchid 18%, transparent)", color: "orchid", label: "EXP" },
};

function TypeBadge({ type }) {
  const cfg = TYPE_BADGE[type] || { bg: "transparent", color: "var(--havn-text-secondary)", label: type.toUpperCase() };
  return (
    <span style={{ ...s.badge, background: cfg.bg, color: cfg.color }}>{cfg.label}</span>
  );
}

function StatusBadge({ status }) {
  return (
    <span style={{ ...s.badge, ...statusStyle(status) }}>
      {statusIcon(status)} {status}
    </span>
  );
}

export default function OrchestrationPanel() {
  const [subTab, setSubTab] = useState("plan-jobs");
  return (
    <div style={s.container}>
      <div style={s.header}>
        <div style={s.subTabs}>
          <button
            style={subTab === "plan-jobs" ? s.subTabActive : s.subTab}
            onClick={() => setSubTab("plan-jobs")}
          >
            Plan Jobs
          </button>
          <button
            style={subTab === "job-results" ? s.subTabActive : s.subTab}
            onClick={() => setSubTab("job-results")}
          >
            Job Results
          </button>
        </div>
      </div>
      {subTab === "plan-jobs" && <PlanJobsTab onSwitchToResults={() => setSubTab("job-results")} />}
      {subTab === "job-results" && <JobResultsTab />}
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
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [editJob, setEditJob] = useState(null);
  const [previewData, setPreviewData] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

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

  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  const openCreate = () => {
    setEditJob(null);
    setForm(EMPTY_FORM);
    setShowForm(true);
  };

  const openEdit = (job) => {
    setEditJob(job);
    setForm({
      name: job.name || "",
      target: job.target || "",
      resolve: job.resolve || "upstream",
      cron: job.cron || "",
      enabled: job.enabled !== false,
      retry: job.retry || 0,
      retry_delay: job.retry_delay || 10,
      timeout_minutes: job.timeout_minutes || 60,
      description: job.description || "",
      notify: job.notify || [],
    });
    setShowForm(true);
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
    <div style={s.tabContent}>
      <div style={s.toolbar}>
        <button style={s.btnPrimary} onClick={openCreate}>+ New Job</button>
        <button style={s.btnSecondary} onClick={loadJobs} disabled={loading}>
          {loading ? "Loading\u2026" : "Refresh"}
        </button>
      </div>

      {error && <div style={s.errorBox}>{error}</div>}

      {loading && jobs.length === 0 ? (
        <div style={s.empty}>Loading jobs\u2026</div>
      ) : jobs.length === 0 ? (
        <div style={s.empty}>
          <div style={{ marginBottom: 12 }}>No orchestration jobs yet.</div>
          <div style={{ fontSize: 12, color: "var(--havn-text-dim)" }}>
            Create YAML files in <code>orchestration/</code> or click &quot;+ New Job&quot;.
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
                <td style={s.td}><strong>{job.name}</strong>
                  {job.description && (
                    <div style={{ fontSize: 11, color: "var(--havn-text-dim)" }}>{job.description}</div>
                  )}
                </td>
                <td style={s.td}><code style={s.code}>{job.target}</code></td>
                <td style={s.td}>{job.cron ? <code style={s.code}>{job.cron}</code> : <span style={s.dim}>-</span>}</td>
                <td style={s.td}>{job.resolve}</td>
                <td style={s.td}>
                  <label style={s.switch}>
                    <input
                      type="checkbox"
                      checked={job.enabled}
                      onChange={(e) => handleToggle(job.name, e.target.checked)}
                    />
                    <span>{job.enabled ? "on" : "off"}</span>
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
                  ) : <span style={s.dim}>never</span>}
                </td>
                <td style={s.td}>
                  {job.next_run ? (
                    <span style={{ fontSize: 11 }}>{new Date(job.next_run).toLocaleString()}</span>
                  ) : <span style={s.dim}>-</span>}
                </td>
                <td style={{ ...s.td, textAlign: "right" }}>
                  <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                    <button style={s.btnSmall} onClick={() => handleRun(job.name)} title="Run now">Run</button>
                    <button style={s.btnSmall} onClick={() => handlePreview(job.name)} title="Preview plan">Preview</button>
                    <button style={s.btnSmall} onClick={() => openEdit(job)} title="Edit">Edit</button>
                    <button style={s.btnSmallDanger} onClick={() => handleDelete(job.name)} title="Delete">\u00D7</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {showForm && (
        <div style={s.modalOverlay} onClick={() => setShowForm(false)}>
          <div style={s.modalCard} onClick={(e) => e.stopPropagation()}>
            <h3 style={s.modalTitle}>{editJob ? "Edit Job" : "New Job"}</h3>
            <div style={s.formGrid}>
              <label style={s.label}>Name
                <input
                  style={s.input}
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="daily-refresh"
                  disabled={!!editJob}
                />
              </label>
              <label style={s.label}>Target
                <input
                  style={s.input}
                  value={form.target}
                  onChange={(e) => setForm({ ...form, target: e.target.value })}
                  placeholder="silver.orders | ingest/orders.py | bronze.*"
                />
              </label>
              <label style={s.label}>Cron Schedule
                <input
                  style={s.input}
                  value={form.cron}
                  onChange={(e) => setForm({ ...form, cron: e.target.value })}
                  placeholder="0 6 * * * (optional)"
                />
              </label>
              <label style={s.label}>Resolve
                <select style={s.input} value={form.resolve} onChange={(e) => setForm({ ...form, resolve: e.target.value })}>
                  <option value="upstream">upstream (include deps)</option>
                  <option value="none">none (target only)</option>
                </select>
              </label>
              <label style={s.label}>Description
                <input
                  style={s.input}
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  placeholder="Optional"
                />
              </label>
              <div style={{ display: "flex", gap: 12 }}>
                <label style={{ ...s.label, flex: 1 }}>Retry
                  <input
                    type="number" min="0" max="10"
                    style={s.input}
                    value={form.retry}
                    onChange={(e) => setForm({ ...form, retry: parseInt(e.target.value) || 0 })}
                  />
                </label>
                <label style={{ ...s.label, flex: 1 }}>Retry Delay (s)
                  <input
                    type="number" min="0"
                    style={s.input}
                    value={form.retry_delay}
                    onChange={(e) => setForm({ ...form, retry_delay: parseInt(e.target.value) || 0 })}
                  />
                </label>
                <label style={{ ...s.label, flex: 1 }}>Timeout (min)
                  <input
                    type="number" min="1"
                    style={s.input}
                    value={form.timeout_minutes}
                    onChange={(e) => setForm({ ...form, timeout_minutes: parseInt(e.target.value) || 60 })}
                  />
                </label>
              </div>
              <label style={{ ...s.label, flexDirection: "row", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                />
                Enabled
              </label>
            </div>
            <div style={s.modalActions}>
              <button style={s.btnSecondary} onClick={() => setShowForm(false)}>Cancel</button>
              <button style={s.btnPrimary} onClick={handleSave} disabled={saving}>
                {saving ? "Saving\u2026" : editJob ? "Save" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}

      {previewData && (
        <div style={s.modalOverlay} onClick={() => setPreviewData(null)}>
          <div style={{ ...s.modalCard, width: 560 }} onClick={(e) => e.stopPropagation()}>
            <h3 style={s.modalTitle}>Execution Plan: {previewData.name}</h3>
            <div style={{ fontSize: 12, color: "var(--havn-text-dim)", marginBottom: 12 }}>
              {previewData.plan.total_steps} steps
              {" \u00B7 "}
              {previewData.plan.ingest_count} ingest, {previewData.plan.transform_count} transform, {previewData.plan.export_count} export
              {previewData.plan.total_estimated_ms > 0 && (
                <> {" \u00B7 "} ~{formatDuration(previewData.plan.total_estimated_ms)} estimated</>
              )}
            </div>
            <div style={{ maxHeight: 400, overflow: "auto", border: "1px solid var(--havn-border)", borderRadius: 4 }}>
              {previewData.plan.steps.map((step) => (
                <div key={step.step} style={s.stepRow}>
                  <span style={{ color: "var(--havn-text-dim)", minWidth: 24 }}>{step.step}.</span>
                  <TypeBadge type={step.type} />
                  <code style={{ ...s.code, flex: 1 }}>{step.target}</code>
                  {step.estimated_duration_ms > 0 && (
                    <span style={{ fontSize: 11, color: "var(--havn-text-dim)" }}>
                      ~{formatDuration(step.estimated_duration_ms)}
                    </span>
                  )}
                </div>
              ))}
              {previewData.plan.steps.length === 0 && (
                <div style={{ padding: 12, textAlign: "center", color: "var(--havn-text-dim)" }}>No steps in plan</div>
              )}
            </div>
            <div style={s.modalActions}>
              <button style={s.btnSecondary} onClick={() => setPreviewData(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- */
/* Job Results Tab                                                  */
/* --------------------------------------------------------------- */

function JobResultsTab() {
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedRun, setSelectedRun] = useState(null);
  const [statusFilter, setStatusFilter] = useState("all");
  const [error, setError] = useState(null);

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

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

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
    <div style={s.tabContent}>
      <div style={s.toolbar}>
        <select style={s.input} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="all">All statuses</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
          <option value="running">Running</option>
          <option value="cancelled">Cancelled</option>
          <option value="timeout">Timeout</option>
        </select>
        <button style={s.btnSecondary} onClick={loadRuns} disabled={loading}>
          {loading ? "Loading\u2026" : "Refresh"}
        </button>
      </div>

      {error && <div style={s.errorBox}>{error}</div>}

      {loading && runs.length === 0 ? (
        <div style={s.empty}>Loading job runs\u2026</div>
      ) : filteredRuns.length === 0 ? (
        <div style={s.empty}>No job runs yet.</div>
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
                  {run.steps_completed}/{run.steps_total}
                  {run.steps_failed > 0 && (
                    <span style={{ color: "var(--havn-red)", marginLeft: 6 }}>({run.steps_failed} failed)</span>
                  )}
                  {run.steps_skipped > 0 && (
                    <span style={{ color: "var(--havn-text-dim)", marginLeft: 6 }}>({run.steps_skipped} skipped)</span>
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
                      style={s.btnSmallDanger}
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

function JobRunDetail({ run, onBack, onRerun }) {
  const completed = run.steps_completed || 0;
  const failed = run.steps_failed || 0;
  const total = run.steps_total || 0;
  const skipped = run.steps_skipped || 0;

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
    <div style={s.tabContent}>
      <div style={{ ...s.toolbar, alignItems: "center" }}>
        <button style={s.btnSecondary} onClick={onBack}>\u2190 Back</button>
        <button style={s.btnPrimary} onClick={handleRerun}>Rerun</button>
      </div>

      <div style={s.detailHeader}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 600 }}>{run.job_name}</div>
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
        <div>
          <strong>{completed}</strong>/<strong>{total}</strong> completed
          {failed > 0 && <span style={{ color: "var(--havn-red)", marginLeft: 8 }}>{failed} failed</span>}
          {skipped > 0 && <span style={{ color: "var(--havn-text-dim)", marginLeft: 8 }}>{skipped} skipped</span>}
        </div>
      </div>

      {run.error && (
        <div style={s.errorBox}>{run.error}</div>
      )}

      <div style={{ border: "1px solid var(--havn-border)", borderRadius: 4, overflow: "hidden" }}>
        {(run.step_details || []).length === 0 ? (
          <div style={{ padding: 16, textAlign: "center", color: "var(--havn-text-dim)" }}>No steps yet</div>
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
        <span style={{ color: "var(--havn-text-dim)", minWidth: 24 }}>{step.step}.</span>
        <TypeBadge type={step.type} />
        <code style={{ ...s.code, flex: 1 }}>{step.target}</code>
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
/* Styles                                                           */
/* --------------------------------------------------------------- */

const s = {
  container: { height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" },
  header: { padding: "12px 16px", borderBottom: "1px solid var(--havn-border)", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 },
  subTabs: { display: "flex", gap: 4 },
  subTab: {
    padding: "6px 16px",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    background: "transparent",
    color: "var(--havn-text-secondary)",
  },
  subTabActive: {
    padding: "6px 16px",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    background: "var(--havn-accent)",
    color: "#fff",
    fontWeight: 500,
  },
  tabContent: { flex: 1, overflow: "auto", padding: 16 },
  toolbar: { display: "flex", gap: 8, marginBottom: 12, alignItems: "center" },
  empty: { padding: 40, textAlign: "center", color: "var(--havn-text-secondary)" },
  errorBox: {
    padding: "8px 12px",
    marginBottom: 12,
    background: "color-mix(in srgb, var(--havn-red) 10%, transparent)",
    color: "var(--havn-red)",
    border: "1px solid color-mix(in srgb, var(--havn-red) 30%, transparent)",
    borderRadius: 4,
    fontSize: 12,
  },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 13 },
  th: {
    padding: "8px 12px",
    textAlign: "left",
    borderBottom: "1px solid var(--havn-border)",
    fontWeight: 600,
    fontSize: 11,
    textTransform: "uppercase",
    color: "var(--havn-text-secondary)",
  },
  td: { padding: "10px 12px", borderBottom: "1px solid var(--havn-border)", verticalAlign: "top" },
  code: {
    fontFamily: "var(--havn-font-mono)",
    fontSize: 12,
    background: "color-mix(in srgb, var(--havn-text) 6%, transparent)",
    padding: "2px 6px",
    borderRadius: 3,
  },
  dim: { color: "var(--havn-text-dim)" },
  badge: {
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: 4,
    fontSize: 11,
    fontWeight: 600,
  },
  switch: {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    cursor: "pointer",
    fontSize: 12,
  },
  btnPrimary: {
    padding: "6px 14px",
    borderRadius: 4,
    border: "none",
    cursor: "pointer",
    fontSize: 13,
    background: "var(--havn-accent)",
    color: "#fff",
    fontWeight: 500,
  },
  btnSecondary: {
    padding: "6px 14px",
    borderRadius: 4,
    border: "1px solid var(--havn-border)",
    cursor: "pointer",
    fontSize: 13,
    background: "transparent",
    color: "var(--havn-text)",
  },
  btnSmall: {
    padding: "3px 10px",
    borderRadius: 3,
    border: "1px solid var(--havn-border)",
    cursor: "pointer",
    fontSize: 11,
    background: "transparent",
    color: "var(--havn-text)",
  },
  btnSmallDanger: {
    padding: "3px 10px",
    borderRadius: 3,
    border: "1px solid color-mix(in srgb, var(--havn-red) 40%, transparent)",
    cursor: "pointer",
    fontSize: 11,
    background: "transparent",
    color: "var(--havn-red)",
  },
  modalOverlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.5)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  modalCard: {
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border)",
    borderRadius: 8,
    padding: 24,
    width: 480,
    maxHeight: "85vh",
    overflow: "auto",
    color: "var(--havn-text)",
  },
  modalTitle: { margin: "0 0 16px", fontSize: 16 },
  modalActions: { display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 20 },
  formGrid: { display: "flex", flexDirection: "column", gap: 12 },
  label: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
    fontSize: 12,
    color: "var(--havn-text-secondary)",
  },
  input: {
    width: "100%",
    padding: "6px 10px",
    background: "var(--havn-bg-secondary, var(--havn-bg))",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    color: "var(--havn-text)",
    fontSize: 13,
    boxSizing: "border-box",
  },
  stepRow: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "8px 12px",
    fontSize: 13,
  },
  detailHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    padding: "12px 16px",
    border: "1px solid var(--havn-border)",
    borderRadius: 4,
    marginBottom: 12,
  },
  summaryBar: {
    padding: "8px 16px",
    fontSize: 13,
    marginBottom: 12,
    background: "color-mix(in srgb, var(--havn-text) 4%, transparent)",
    borderRadius: 4,
  },
  errorDetail: {
    padding: "8px 16px 10px 56px",
    fontFamily: "var(--havn-font-mono)",
    fontSize: 11,
    color: "var(--havn-red)",
    whiteSpace: "pre-wrap",
    background: "color-mix(in srgb, var(--havn-red) 4%, transparent)",
  },
};

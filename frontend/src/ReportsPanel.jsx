import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { useAuth } from "./AuthContext";
import FocusTrap from "./FocusTrap";

/*
 * Scheduled reports: deliver a dashboard (or one widget) by email and Slack
 * on a cron schedule, optionally only when a condition holds. Reports run as
 * their owner, so only the owner or an admin can change, send or preview one.
 */

export const SCHEDULE_PRESETS = [
  { id: "manual", label: "Manual only", cron: "" },
  { id: "daily", label: "Every day at 07:00", cron: "0 7 * * *" },
  { id: "weekdays", label: "Weekdays at 07:00", cron: "0 7 * * 1-5" },
  { id: "weekly", label: "Mondays at 07:00", cron: "0 7 * * 1" },
  { id: "monthly", label: "1st of the month at 07:00", cron: "0 7 1 * *" },
  { id: "hourly", label: "Every hour", cron: "0 * * * *" },
  { id: "custom", label: "Custom (cron)", cron: null },
];

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Plain-language reading of common cron shapes; anything else is shown as cron. */
export function describeCron(cron) {
  if (!cron) return "Manual only";
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [min, hour, dom, mon, dow] = parts;
  const isNum = (v) => /^\d+$/.test(v);
  const time = isNum(min) && isNum(hour) ? `${hour.padStart(2, "0")}:${min.padStart(2, "0")}` : null;
  if (min === "0" && hour === "*" && dom === "*" && mon === "*" && dow === "*") return "Every hour";
  if (!time || mon !== "*") return cron;
  if (dom === "*" && dow === "*") return `Daily at ${time}`;
  if (dom === "*" && dow === "1-5") return `Weekdays at ${time}`;
  if (dom === "*" && isNum(dow) && Number(dow) <= 6) return `${DAYS[Number(dow)]}s at ${time}`;
  if (isNum(dom) && dow === "*") return `Monthly on day ${dom} at ${time}`;
  return cron;
}

const OPS = [
  { id: "gt", label: "is above" },
  { id: "gte", label: "is at least" },
  { id: "lt", label: "is below" },
  { id: "lte", label: "is at most" },
  { id: "eq", label: "equals" },
  { id: "ne", label: "is not" },
  { id: "has_rows", label: "returns rows" },
  { id: "no_rows", label: "returns no rows" },
];

const STATUS = {
  sent: { color: "var(--havn-green)", label: "Sent" },
  skipped: { color: "var(--havn-text-secondary)", label: "Skipped" },
  partial: { color: "var(--havn-yellow)", label: "Partly sent" },
  failed: { color: "var(--havn-red)", label: "Failed" },
};

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function emptyDraft(dashboardId = "") {
  return {
    name: "", dashboard_id: dashboardId, widget_id: "", schedule: "0 7 * * 1-5", enabled: true,
    emails: "", slackDefault: false, slackExtra: "", formats: ["pdf", "csv"],
    filters: {}, condition: null, subject: "", message: "",
  };
}

function draftFromReport(r) {
  const slack = r.recipients?.slack || [];
  return {
    name: r.name, dashboard_id: r.dashboard_id, widget_id: r.widget_id || "", schedule: r.schedule || "",
    enabled: r.enabled, emails: (r.recipients?.email || []).join(", "),
    slackDefault: slack.includes("default"), slackExtra: slack.filter(t => t !== "default").join("\n"),
    formats: r.formats || [], filters: r.filters || {}, condition: r.condition || null,
    subject: r.subject || "", message: r.message || "",
  };
}

export function draftToBody(d) {
  const slack = [];
  if (d.slackDefault) slack.push("default");
  for (const t of d.slackExtra.split(/[\n,]+/).map(x => x.trim()).filter(Boolean)) slack.push(t);
  const filters = {};
  for (const [k, v] of Object.entries(d.filters || {})) {
    if (v === "" || v === null || v === undefined) continue;
    if (typeof v === "object" && !Array.isArray(v) && Object.values(v).every(x => x === "" || x === null || x === undefined)) continue;
    filters[k] = v;
  }
  return {
    name: d.name.trim(),
    dashboard_id: d.dashboard_id,
    widget_id: d.widget_id || null,
    schedule: d.schedule.trim() || null,
    enabled: d.enabled,
    recipients: { email: d.emails.split(/[\s,;]+/).map(x => x.trim()).filter(Boolean), slack },
    formats: d.formats,
    filters,
    condition: d.condition && d.condition.widget_id ? {
      widget_id: d.condition.widget_id,
      op: d.condition.op,
      ...(d.condition.column ? { column: d.condition.column } : {}),
      ...(["has_rows", "no_rows"].includes(d.condition.op) ? {} : { value: Number(d.condition.value) }),
    } : null,
    subject: d.subject,
    message: d.message,
  };
}

export default function ReportsPanel() {
  const { currentUser } = useAuth();
  const me = currentUser?.username || "local";
  const isAdmin = !currentUser || currentUser.role === "admin";
  const [reports, setReports] = useState(null);
  const [caps, setCaps] = useState(null);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null); // {report|null, draft}
  const [preview, setPreview] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [notice, setNotice] = useState(null);
  const [expanded, setExpanded] = useState(null);

  const load = useCallback(async () => {
    try {
      setReports(await api.listReports());
      setError(null);
    } catch (e) {
      setError(e.message);
      setReports([]);
    }
  }, []);

  useEffect(() => {
    load();
    api.reportCapabilities().then(setCaps).catch(() => {});
    // Arriving from a dashboard's Share dialog: open a new report for it.
    const dash = new URLSearchParams(window.location.search).get("dashboard");
    if (dash) {
      setEditing({ report: null, draft: emptyDraft(dash) });
      try { window.history.replaceState(null, "", window.location.pathname); } catch { /* not a browser */ }
    }
  }, [load]);

  const canChange = (r) => isAdmin || r.owner === me;

  async function send(r, force = false) {
    setBusyId(r.id);
    setNotice(null);
    try {
      const d = await api.sendReport(r.id, force);
      const label = STATUS[d.status]?.label || d.status;
      setNotice({
        kind: d.status === "sent" ? "ok" : d.status === "skipped" ? "info" : "error",
        text: d.status === "skipped"
          ? `${r.name}: not sent, ${d.summary?.condition || "its condition was not met"}.`
          : `${r.name}: ${label}${d.error ? ` — ${d.error}` : ""}`,
        report: d.status === "skipped" ? r : null,
      });
      await load();
    } catch (e) {
      setNotice({ kind: "error", text: e.message });
    } finally {
      setBusyId(null);
    }
  }

  async function openPreview(r) {
    setBusyId(r.id);
    try {
      setPreview({ report: r, data: await api.previewReport(r.id) });
    } catch (e) {
      setNotice({ kind: "error", text: e.message });
    } finally {
      setBusyId(null);
    }
  }

  async function remove(r) {
    if (!window.confirm(`Delete the report "${r.name}"? Its delivery history goes with it.`)) return;
    try {
      await api.deleteReport(r.id);
      await load();
    } catch (e) {
      setNotice({ kind: "error", text: e.message });
    }
  }

  async function toggle(r) {
    try {
      await api.updateReport(r.id, { enabled: !r.enabled });
      await load();
    } catch (e) {
      setNotice({ kind: "error", text: e.message });
    }
  }

  return (
    <div style={st.page}>
      <div style={st.header}>
        <div>
          <h2 style={st.h2}>Reports</h2>
          <p style={st.lede}>Deliver a dashboard by email or Slack on a schedule, or only when a number crosses a line. Each report runs as its owner, with the owner's masking.</p>
        </div>
        <button style={st.primary} onClick={() => setEditing({ report: null, draft: emptyDraft() })}>New report</button>
      </div>

      {caps && (!caps.email_configured || !caps.charts) && (
        <div style={st.capBar}>
          {!caps.email_configured && <span>Email is not set up: add <code>reports.smtp</code> to project.yml.</span>}
          {!caps.charts && <span>Charts are off on this server: reports carry tables, KPI values and a text-only PDF. <code>pip install "havn[reports]"</code> adds charts and PNG snapshots.</span>}
        </div>
      )}

      {error && <div style={st.error} role="alert">{error}</div>}
      {notice && (
        <div style={{ ...st.notice, ...(notice.kind === "error" ? st.noticeError : notice.kind === "ok" ? st.noticeOk : {}) }} role="status">
          <span>{notice.text}</span>
          {notice.report && <button style={st.linkBtn} onClick={() => send(notice.report, true)}>Send anyway</button>}
          <button style={st.dismiss} onClick={() => setNotice(null)} aria-label="Dismiss">×</button>
        </div>
      )}

      {reports === null && <div style={st.muted}>Loading…</div>}
      {reports && reports.length === 0 && !error && (
        <div style={st.empty}>
          <div style={st.emptyTitle}>No reports yet</div>
          <div style={st.muted}>Open a dashboard and choose Share › Schedule a report, or start here.</div>
        </div>
      )}

      {reports && reports.length > 0 && (
        <div style={st.list}>
          {reports.map(r => {
            const status = STATUS[r.last_status];
            const rec = r.recipients || {};
            const recipients = [...(rec.email || []), ...(rec.slack || []).map(t => (t === "default" ? "Slack (default)" : `Slack ${t}`))];
            const mine = canChange(r);
            return (
              <article key={r.id} style={{ ...st.card, ...(r.enabled ? {} : st.cardOff) }}>
                <div style={st.cardMain}>
                  <div style={st.cardTitleRow}>
                    <h3 style={st.cardTitle}>{r.name}</h3>
                    {!r.enabled && <span style={st.pill}>paused</span>}
                    {r.condition && <span style={st.pill} title="Sent only when its condition holds">conditional</span>}
                  </div>
                  <div style={st.cardMeta}>
                    <span>{r.dashboard_name || "Deleted dashboard"}{r.widget_id ? " · one widget" : ""}</span>
                    <span>{describeCron(r.schedule)}</span>
                    {r.next_run_at && <span>next {fmtWhen(r.next_run_at)}</span>}
                    <span>owner {r.owner}</span>
                  </div>
                  <div style={st.recipients} title={recipients.join(", ")}>
                    {recipients.length ? recipients.join(", ") : <em>No recipients</em>}
                  </div>
                </div>
                <div style={st.cardSide}>
                  <button style={st.statusBtn} onClick={() => setExpanded(expanded === r.id ? null : r.id)} aria-expanded={expanded === r.id}>
                    <span style={{ ...st.dot, background: status?.color || "var(--havn-border)" }} />
                    {status ? `${status.label} ${fmtWhen(r.last_run_at)}` : "Not run yet"}
                  </button>
                  {mine && (
                    <div style={st.actions}>
                      <button style={st.secondary} disabled={busyId === r.id} onClick={() => send(r)}>{busyId === r.id ? "…" : "Send now"}</button>
                      <button style={st.secondary} disabled={busyId === r.id} onClick={() => openPreview(r)}>Preview</button>
                      <button style={st.secondary} onClick={() => setEditing({ report: r, draft: draftFromReport(r) })}>Edit</button>
                      <button style={st.secondary} onClick={() => toggle(r)}>{r.enabled ? "Pause" : "Resume"}</button>
                      <button style={st.danger} onClick={() => remove(r)}>Delete</button>
                    </div>
                  )}
                </div>
                {r.last_error && r.last_status !== "sent" && <div style={st.lastError}>{r.last_error}</div>}
                {expanded === r.id && <DeliveryHistory reportId={r.id} />}
              </article>
            );
          })}
        </div>
      )}

      {editing && (
        <ReportEditor
          report={editing.report}
          initial={editing.draft}
          caps={caps}
          onClose={() => setEditing(null)}
          onSaved={async () => { setEditing(null); await load(); }}
        />
      )}
      {preview && <PreviewModal preview={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}

function DeliveryHistory({ reportId }) {
  const [rows, setRows] = useState(null);
  useEffect(() => {
    api.getReport(reportId).then(r => setRows(r.deliveries || [])).catch(() => setRows([]));
  }, [reportId]);
  if (rows === null) return <div style={st.history}><span style={st.muted}>Loading…</span></div>;
  if (rows.length === 0) return <div style={st.history}><span style={st.muted}>No deliveries yet.</span></div>;
  return (
    <div style={st.history}>
      <table style={st.table}>
        <thead>
          <tr><th style={st.th}>When</th><th style={st.th}>Trigger</th><th style={st.th}>Result</th><th style={st.th}>Channels</th></tr>
        </thead>
        <tbody>
          {rows.map(d => (
            <tr key={d.id}>
              <td style={st.td}>{fmtWhen(d.started_at)}</td>
              <td style={st.td}>{d.trigger}</td>
              <td style={st.td}>
                <span style={{ color: STATUS[d.status]?.color }}>{STATUS[d.status]?.label || d.status}</span>
                {d.status === "skipped" && d.summary?.condition && <div style={st.small}>{d.summary.condition}</div>}
                {d.error && <div style={{ ...st.small, color: "var(--havn-red)" }}>{d.error}</div>}
              </td>
              <td style={st.td}>
                {(d.channels || []).map((c, i) => (
                  <div key={i} style={st.small}>{c.channel} {c.target}: <span style={{ color: c.status === "sent" ? "var(--havn-green)" : "var(--havn-red)" }}>{c.status}</span></div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReportEditor({ report, initial, caps, onClose, onSaved }) {
  const [d, setD] = useState(initial);
  const [dashboards, setDashboards] = useState([]);
  const [full, setFull] = useState(null);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const preset = SCHEDULE_PRESETS.find(p => p.cron === d.schedule) || (d.schedule ? SCHEDULE_PRESETS.find(p => p.id === "custom") : SCHEDULE_PRESETS[0]);
  const [customCron, setCustomCron] = useState(preset.id === "custom");

  useEffect(() => { api.listDashboards().then(setDashboards).catch(() => setDashboards([])); }, []);
  useEffect(() => {
    if (!d.dashboard_id) { setFull(null); return; }
    api.getDashboard(d.dashboard_id).then(setFull).catch(() => setFull(null));
  }, [d.dashboard_id]);

  const set = (patch) => setD(prev => ({ ...prev, ...patch }));
  const queryWidgets = useMemo(
    () => (full?.widgets || []).filter(w => w.sql_query && !["text", "image", "divider"].includes(w.widget_type)),
    [full],
  );

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const body = draftToBody(d);
      if (report) await api.updateReport(report.id, body);
      else await api.createReport(body);
      await onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  const toggleFormat = (f) => set({ formats: d.formats.includes(f) ? d.formats.filter(x => x !== f) : [...d.formats, f] });

  return (
    <FocusTrap labelledBy="havn-report-editor" style={st.overlay} onClick={(e) => e.target === e.currentTarget && onClose()} onEscape={onClose}>
      <div style={st.modal}>
        <div style={st.modalHeader}>
          <h3 id="havn-report-editor" style={st.modalTitle}>{report ? `Edit “${report.name}”` : "New report"}</h3>
          <button style={st.close} onClick={onClose} aria-label="Close">×</button>
        </div>
        <div style={st.modalBody}>
          {error && <div style={st.error} role="alert">{error}</div>}

          <Field label="Name">
            <input style={st.input} value={d.name} onChange={e => set({ name: e.target.value })} placeholder="Daily sales" maxLength={100} autoFocus />
          </Field>

          <div style={st.twoCol}>
            <Field label="Dashboard">
              <select style={st.input} value={d.dashboard_id} onChange={e => set({ dashboard_id: e.target.value, widget_id: "", filters: {}, condition: null })}>
                <option value="">Choose a dashboard…</option>
                {dashboards.map(x => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select>
            </Field>
            <Field label="Content">
              <select style={st.input} value={d.widget_id} onChange={e => set({ widget_id: e.target.value })} disabled={!full}>
                <option value="">Whole dashboard</option>
                {(full?.widgets || []).filter(w => w.widget_type !== "divider").map(w => <option key={w.id} value={w.id}>Only: {w.title || w.widget_type}</option>)}
              </select>
            </Field>
          </div>

          <div style={st.twoCol}>
            <Field label="Schedule" hint={d.schedule ? describeCron(d.schedule) + " (server time)" : "Sent only with Send now or havn reports send"}>
              <select
                style={st.input}
                value={customCron ? "custom" : preset.id}
                onChange={e => {
                  const p = SCHEDULE_PRESETS.find(x => x.id === e.target.value);
                  if (p.id === "custom") { setCustomCron(true); return; }
                  setCustomCron(false);
                  set({ schedule: p.cron });
                }}
              >
                {SCHEDULE_PRESETS.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
            </Field>
            {customCron && (
              <Field label="Cron expression" hint="minute hour day month weekday">
                <input style={{ ...st.input, fontFamily: "var(--havn-font-mono)" }} value={d.schedule} onChange={e => set({ schedule: e.target.value })} placeholder="0 7 * * 1-5" />
              </Field>
            )}
          </div>

          <Field label="Email recipients" hint={caps?.allowed_recipient_domains?.length ? `Allowed domains: ${caps.allowed_recipient_domains.join(", ")}` : "Comma or space separated"}>
            <textarea style={{ ...st.input, minHeight: 54, resize: "vertical" }} value={d.emails} onChange={e => set({ emails: e.target.value })} placeholder="finance@example.com, ceo@example.com" />
          </Field>
          <Field label="Slack" hint="A ${VAR} from .env or a webhook URL, one per line. Saved URLs are shown shortened.">
            <label style={st.check}>
              <input type="checkbox" checked={d.slackDefault} onChange={e => set({ slackDefault: e.target.checked })} />
              Project default webhook{caps && !caps.slack_default_configured ? " (not configured)" : ""}
            </label>
            <textarea style={{ ...st.input, minHeight: 40, resize: "vertical", marginTop: 6, fontFamily: "var(--havn-font-mono)", fontSize: 12 }} value={d.slackExtra} onChange={e => set({ slackExtra: e.target.value })} placeholder="${SALES_SLACK_WEBHOOK}" />
          </Field>

          <Field label="Attachments" hint="Every report has an inline summary with the KPI values.">
            <div style={st.checks}>
              {[["pdf", "PDF"], ["png", "PNG snapshot"], ["csv", "CSV of each widget"]].map(([f, label]) => (
                <label key={f} style={st.check}>
                  <input type="checkbox" checked={d.formats.includes(f)} onChange={() => toggleFormat(f)} />
                  {label}{f === "png" && caps && !caps.charts ? " (needs charts)" : ""}
                </label>
              ))}
            </div>
          </Field>

          {full && (full.filters || []).length > 0 && (
            <Field label="Filters" hint="Fixed values applied to every delivery.">
              <div style={st.filterGrid}>
                {full.filters.map(f => (
                  <FilterInput key={f.id || f.column} filter={f} value={d.filters[f.column]} onChange={v => set({ filters: { ...d.filters, [f.column]: v } })} />
                ))}
              </div>
            </Field>
          )}

          <Field label="Only send when" hint="Leave empty to send every time. A skipped delivery is still recorded.">
            <div style={st.condRow}>
              <select style={{ ...st.input, flex: 2, minWidth: 140 }} value={d.condition?.widget_id || ""} onChange={e => set({ condition: e.target.value ? { op: "gt", value: "", ...(d.condition || {}), widget_id: e.target.value } : null })} disabled={!full}>
                <option value="">Always send</option>
                {queryWidgets.map(w => <option key={w.id} value={w.id}>{w.title || w.widget_type}</option>)}
              </select>
              {d.condition?.widget_id && (
                <>
                  <select style={{ ...st.input, flex: 1, minWidth: 120 }} value={d.condition.op} onChange={e => set({ condition: { ...d.condition, op: e.target.value } })}>
                    {OPS.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select>
                  {!["has_rows", "no_rows"].includes(d.condition.op) && (
                    <input style={{ ...st.input, flex: 1, minWidth: 90 }} type="number" value={d.condition.value ?? ""} onChange={e => set({ condition: { ...d.condition, value: e.target.value } })} placeholder="1000" aria-label="Threshold" />
                  )}
                </>
              )}
            </div>
          </Field>

          <div style={st.twoCol}>
            <Field label="Subject (optional)">
              <input style={st.input} value={d.subject} onChange={e => set({ subject: e.target.value })} placeholder={d.name || "Report name"} maxLength={300} />
            </Field>
            <Field label="Status">
              <label style={st.check}><input type="checkbox" checked={d.enabled} onChange={e => set({ enabled: e.target.checked })} /> Scheduled deliveries on</label>
            </Field>
          </div>
          <Field label="Message (optional)">
            <textarea style={{ ...st.input, minHeight: 54, resize: "vertical" }} value={d.message} onChange={e => set({ message: e.target.value })} placeholder="A line of context for the people who receive it" maxLength={4000} />
          </Field>
        </div>
        <div style={st.modalFooter}>
          <span style={st.muted}>{report ? `Runs as ${report.owner}` : "Runs as you"}</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button style={st.secondary} onClick={onClose}>Cancel</button>
            <button style={st.primary} onClick={save} disabled={saving || !d.name.trim() || !d.dashboard_id}>{saving ? "Saving…" : report ? "Save" : "Create report"}</button>
          </div>
        </div>
      </div>
    </FocusTrap>
  );
}

function FilterInput({ filter, value, onChange }) {
  const label = <span style={st.small}>{filter.label || filter.column}</span>;
  if (filter.type === "toggle") {
    return <label style={st.check}><input type="checkbox" checked={!!value} onChange={e => onChange(e.target.checked || null)} />{filter.label || filter.column}</label>;
  }
  if (filter.type === "date_range" || filter.type === "number_range") {
    const [lo, hi] = filter.type === "date_range" ? ["from", "to"] : ["min", "max"];
    const t = filter.type === "date_range" ? "date" : "number";
    const v = value || {};
    const put = (k, x) => onChange({ ...v, [k]: x === "" ? null : (t === "number" ? Number(x) : x) });
    return (
      <div>
        {label}
        <div style={{ display: "flex", gap: 6 }}>
          <input style={st.input} type={t} value={v[lo] ?? ""} onChange={e => put(lo, e.target.value)} aria-label={`${filter.label} ${lo}`} />
          <input style={st.input} type={t} value={v[hi] ?? ""} onChange={e => put(hi, e.target.value)} aria-label={`${filter.label} ${hi}`} />
        </div>
      </div>
    );
  }
  if (filter.type === "multi_select") {
    return (
      <div>
        {label}
        <input style={st.input} value={Array.isArray(value) ? value.join(", ") : ""} onChange={e => onChange(e.target.value ? e.target.value.split(",").map(x => x.trim()).filter(Boolean) : null)} placeholder="a, b, c" />
      </div>
    );
  }
  return (
    <div>
      {label}
      <input style={st.input} value={value ?? ""} onChange={e => onChange(e.target.value || null)} placeholder="All" />
    </div>
  );
}

function PreviewModal({ preview, onClose }) {
  const { report, data } = preview;
  const [dlError, setDlError] = useState(null);
  async function download(format) {
    setDlError(null);
    try {
      const blob = await api.downloadReport(report.id, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${report.name.replace(/[^\w.-]+/g, "_")}.${format}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setDlError(e.message);
    }
  }
  return (
    <FocusTrap labelledBy="havn-report-preview" style={st.overlay} onClick={(e) => e.target === e.currentTarget && onClose()} onEscape={onClose}>
      <div style={{ ...st.modal, width: 760 }}>
        <div style={st.modalHeader}>
          <div style={{ minWidth: 0 }}>
            <h3 id="havn-report-preview" style={st.modalTitle}>{data.subject}</h3>
            <div style={st.muted}>
              Preview as {report.owner}. Nothing was sent.
              {data.condition && ` Condition: ${data.condition.description} — ${data.condition.met ? "would send" : "would be skipped"}.`}
            </div>
          </div>
          <button style={st.close} onClick={onClose} aria-label="Close">×</button>
        </div>
        <div style={{ ...st.modalBody, padding: 0, background: "#f3f2ef" }}>
          {/* The report HTML is rendered in a sandboxed frame: no scripts, no access to this page. */}
          <iframe title="Report preview" sandbox="" srcDoc={data.html} style={{ border: 0, width: "100%", height: "60vh", display: "block" }} />
        </div>
        <div style={st.modalFooter}>
          <div style={st.attachments}>
            {data.attachments.length === 0 ? <span style={st.muted}>No attachments</span> : data.attachments.map(a => (
              <span key={a.filename} style={st.attachment}>{a.filename} · {Math.max(1, Math.round(a.bytes / 1024))} KB</span>
            ))}
            {dlError && <span style={{ ...st.small, color: "var(--havn-red)" }}>{dlError}</span>}
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button style={st.secondary} onClick={() => download("pdf")}>Download PDF</button>
            <button style={st.secondary} onClick={() => download("png")}>PNG</button>
          </div>
        </div>
      </div>
    </FocusTrap>
  );
}

function Field({ label, hint, children }) {
  return (
    <div style={st.field}>
      <div style={st.label}>{label}</div>
      {children}
      {hint && <div style={st.hint}>{hint}</div>}
    </div>
  );
}

const st = {
  page: { padding: "20px 24px 40px", overflowY: "auto", height: "100%", color: "var(--havn-text)", maxWidth: 1100 },
  header: { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, marginBottom: 16, flexWrap: "wrap" },
  h2: { margin: 0, fontSize: 18, fontWeight: 600 },
  lede: { margin: "6px 0 0", fontSize: 13, color: "var(--havn-text-secondary)", maxWidth: 620, lineHeight: 1.5 },
  capBar: { display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: "var(--havn-text-secondary)", border: "1px dashed var(--havn-border)", borderRadius: 8, padding: "10px 12px", marginBottom: 14, lineHeight: 1.5 },
  list: { display: "flex", flexDirection: "column", gap: 10 },
  card: { border: "1px solid var(--havn-border)", borderRadius: 10, padding: "14px 16px", background: "var(--havn-bg-secondary, var(--havn-bg))", display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-start" },
  cardOff: { opacity: 0.7 },
  cardMain: { flex: "1 1 320px", minWidth: 0 },
  cardSide: { display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 8, flex: "0 1 auto" },
  cardTitleRow: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" },
  cardTitle: { margin: 0, fontSize: 14.5, fontWeight: 600 },
  cardMeta: { display: "flex", gap: 12, flexWrap: "wrap", fontSize: 12, color: "var(--havn-text-secondary)", marginTop: 4 },
  recipients: { fontSize: 12.5, marginTop: 6, color: "var(--havn-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  pill: { fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.04em", border: "1px solid var(--havn-border)", borderRadius: 999, padding: "1px 7px", color: "var(--havn-text-secondary)" },
  statusBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 12, cursor: "pointer", fontFamily: "inherit", display: "inline-flex", alignItems: "center", gap: 6, padding: 0 },
  dot: { width: 8, height: 8, borderRadius: "50%", display: "inline-block" },
  actions: { display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" },
  lastError: { flexBasis: "100%", fontSize: 12, color: "var(--havn-red)" },
  history: { flexBasis: "100%", borderTop: "1px solid var(--havn-border)", paddingTop: 10, overflowX: "auto" },
  table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
  th: { textAlign: "left", fontWeight: 500, color: "var(--havn-text-secondary)", padding: "4px 8px", borderBottom: "1px solid var(--havn-border)" },
  td: { padding: "6px 8px", borderBottom: "1px solid var(--havn-border)", verticalAlign: "top" },
  small: { fontSize: 11.5, color: "var(--havn-text-secondary)" },
  empty: { border: "1px dashed var(--havn-border)", borderRadius: 10, padding: "32px 20px", textAlign: "center" },
  emptyTitle: { fontSize: 14, fontWeight: 600, marginBottom: 4 },
  muted: { fontSize: 12.5, color: "var(--havn-text-secondary)" },
  error: { background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)", border: "1px solid color-mix(in srgb, var(--havn-red) 35%, transparent)", borderRadius: 6, padding: "8px 10px", fontSize: 12.5, marginBottom: 12 },
  notice: { display: "flex", gap: 12, alignItems: "center", border: "1px solid var(--havn-border)", borderRadius: 8, padding: "8px 12px", fontSize: 13, marginBottom: 12 },
  noticeError: { borderColor: "color-mix(in srgb, var(--havn-red) 45%, transparent)", color: "var(--havn-red)" },
  noticeOk: { borderColor: "color-mix(in srgb, var(--havn-green) 45%, transparent)" },
  dismiss: { marginLeft: "auto", background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontFamily: "inherit", fontSize: 16 },
  primary: { background: "var(--havn-accent)", color: "var(--havn-bg)", border: "none", borderRadius: 6, padding: "8px 14px", fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" },
  secondary: { background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text)", borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" },
  danger: { background: "none", border: "1px solid color-mix(in srgb, var(--havn-red) 50%, transparent)", color: "var(--havn-red)", borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit" },
  linkBtn: { background: "none", border: "none", color: "var(--havn-accent)", cursor: "pointer", fontFamily: "inherit", fontSize: 13, padding: 0 },
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 10000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 },
  modal: { background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 12, width: 640, maxWidth: "100%", maxHeight: "92vh", display: "flex", flexDirection: "column", boxShadow: "0 12px 40px rgba(0,0,0,0.35)", color: "var(--havn-text)" },
  modalHeader: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, padding: "16px 20px 12px", borderBottom: "1px solid var(--havn-border)" },
  modalTitle: { margin: 0, fontSize: 15.5, fontWeight: 600, overflowWrap: "anywhere" },
  modalBody: { padding: "14px 20px", overflowY: "auto", display: "flex", flexDirection: "column", gap: 2 },
  modalFooter: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, padding: "12px 20px", borderTop: "1px solid var(--havn-border)", flexWrap: "wrap" },
  close: { background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 22, cursor: "pointer", fontFamily: "inherit", lineHeight: 1 },
  field: { display: "flex", flexDirection: "column", gap: 4, marginBottom: 12, minWidth: 0, flex: 1 },
  label: { fontSize: 12, fontWeight: 500, color: "var(--havn-text-secondary)" },
  hint: { fontSize: 11.5, color: "var(--havn-text-secondary)", opacity: 0.85, lineHeight: 1.4 },
  input: { background: "var(--havn-bg-secondary, var(--havn-bg))", border: "1px solid var(--havn-border)", borderRadius: 6, color: "var(--havn-text)", padding: "7px 9px", fontSize: 13, minWidth: 0, fontFamily: "inherit", width: "100%", boxSizing: "border-box" },
  twoCol: { display: "flex", gap: 12, flexWrap: "wrap" },
  checks: { display: "flex", gap: 14, flexWrap: "wrap" },
  check: { display: "inline-flex", gap: 6, alignItems: "center", fontSize: 13, color: "var(--havn-text)" },
  filterGrid: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 },
  condRow: { display: "flex", gap: 8, flexWrap: "wrap" },
  attachments: { display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" },
  attachment: { fontSize: 11.5, border: "1px solid var(--havn-border)", borderRadius: 999, padding: "2px 8px", color: "var(--havn-text-secondary)" },
};

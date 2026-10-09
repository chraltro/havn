import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { useAuth } from "./AuthContext";
import FocusTrap from "./FocusTrap";
import { tabToPath } from "./navigation";

/*
 * Publish / Share dialog for a dashboard: signed-in and public links, their
 * expiry and revocation, the iframe embed snippet, and a way into the
 * dashboard's scheduled reports.
 */

const EXPIRY_CHOICES = [
  { value: 0, label: "Never" },
  { value: 1, label: "1 day" },
  { value: 7, label: "7 days" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: 365, label: "1 year" },
];

export function embedSnippet(url, title) {
  const esc = (v) => String(v).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  const sep = url.includes("?") ? "&" : "?";
  return `<iframe src="${esc(url + sep + "embed=1")}" title="${esc(title)}" width="100%" height="600" style="border:0" loading="lazy" referrerpolicy="no-referrer"></iframe>`;
}

function fmtDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function CopyButton({ text, label = "Copy" }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      style={st.secondary}
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1800); }).catch(() => {});
      }}
    >
      {done ? "Copied" : label}
    </button>
  );
}

export default function ShareDialog({ dashboard, onClose }) {
  const { currentUser } = useAuth();
  const isAdmin = !currentUser || currentUser.role === "admin";
  const [shares, setShares] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [mode, setMode] = useState("signed_in");
  const [label, setLabel] = useState("");
  const [expiry, setExpiry] = useState(0);
  const [viewAsKind, setViewAsKind] = useState("role");
  const [viewAsRole, setViewAsRole] = useState("viewer");
  const [viewAsUser, setViewAsUser] = useState("");
  const [users, setUsers] = useState([]);
  const [created, setCreated] = useState(null);
  const [busy, setBusy] = useState(false);
  const [reportCount, setReportCount] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setShares(await api.listDashboardShares(dashboard.id));
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [dashboard.id]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api.listReports().then(rs => setReportCount(rs.filter(r => r.dashboard_id === dashboard.id).length)).catch(() => {});
  }, [dashboard.id]);
  useEffect(() => {
    if (mode === "public" && isAdmin && users.length === 0) {
      api.listUsers().then(setUsers).catch(() => setUsers([]));
    }
  }, [mode, isAdmin]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    // Public links default to expiring; a signed-in link can live as long as the dashboard.
    setExpiry(mode === "public" ? 30 : 0);
  }, [mode]);

  const origin = window.location.origin;

  async function createLink() {
    setBusy(true);
    setError(null);
    try {
      const body = { mode, label: label.trim() };
      if (expiry) body.expires_in_days = expiry;
      if (mode === "public") {
        if (viewAsKind === "user") body.view_as_user = viewAsUser;
        else body.view_as_role = viewAsRole;
      }
      const res = await api.createDashboardShare(dashboard.id, body);
      setCreated({ ...res, url: res.url || origin + res.path });
      setLabel("");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(share) {
    if (!window.confirm(`Revoke this ${share.mode === "public" ? "public" : "signed-in"} link? Anyone using it loses access immediately.`)) return;
    try {
      await api.revokeDashboardShare(share.id);
      if (created?.id === share.id) setCreated(null);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function changeExpiry(share, days) {
    try {
      await api.updateDashboardShare(share.id, days ? { expires_in_days: days } : { clear_expiry: true });
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  const active = shares.filter(s => s.status === "active");
  const inactive = shares.filter(s => s.status !== "active");
  const canCreatePublic = isAdmin;
  const viewAsReady = mode !== "public" || viewAsKind === "role" || viewAsUser;

  return (
    <FocusTrap
      labelledBy="havn-share-title"
      style={st.overlay}
      onClick={(e) => e.target === e.currentTarget && onClose()}
      onEscape={onClose}
    >
      <div style={st.panel}>
        <div style={st.header}>
          <div>
            <h3 id="havn-share-title" style={st.heading}>Share “{dashboard.name}”</h3>
            <div style={st.sub}>Published views are read-only. Filters work; editing and SQL do not.</div>
          </div>
          <button style={st.close} onClick={onClose} aria-label="Close">×</button>
        </div>

        <div style={st.body}>
          {error && <div style={st.error} role="alert">{error}</div>}

          {created && (
            <section style={st.created} aria-live="polite">
              <div style={st.createdTitle}>
                {created.mode === "public" ? "Public link created" : "Link created"}
              </div>
              {created.mode === "public" && (
                <div style={st.warn}>
                  Copy it now: the link is shown only once. Anyone who has it sees this dashboard as{" "}
                  <strong>{created.view_as_user || `a ${created.view_as_role}`}</strong>
                  {created.expires_at ? ` until ${fmtDate(created.expires_at)}` : ""}.
                </div>
              )}
              <div style={st.row}>
                <input style={{ ...st.input, flex: 1, fontFamily: "var(--havn-font-mono)", fontSize: 12 }} readOnly value={created.url} onFocus={e => e.target.select()} aria-label="Link" />
                <CopyButton text={created.url} label="Copy link" />
              </div>
              <EmbedBlock url={created.url} title={dashboard.name} />
            </section>
          )}

          <section>
            <h4 style={st.sectionTitle}>New link</h4>
            <div style={st.segmented} role="radiogroup" aria-label="Who can open the link">
              <button
                type="button" role="radio" aria-checked={mode === "signed_in"}
                style={{ ...st.segment, ...(mode === "signed_in" ? st.segmentOn : {}) }}
                onClick={() => setMode("signed_in")}
              >
                <span style={st.segmentTitle}>Signed-in viewers</span>
                <span style={st.segmentHint}>Anyone with a havn account and read access, with their own masking</span>
              </button>
              <button
                type="button" role="radio" aria-checked={mode === "public"}
                disabled={!canCreatePublic}
                title={canCreatePublic ? undefined : "Only admins can create public links"}
                style={{ ...st.segment, ...(mode === "public" ? st.segmentOn : {}) }}
                onClick={() => setMode("public")}
              >
                <span style={st.segmentTitle}>Public link</span>
                <span style={st.segmentHint}>{canCreatePublic ? "No account needed. Runs as an identity you choose" : "Admins only"}</span>
              </button>
            </div>

            {mode === "public" && (
              <div style={st.field}>
                <label style={st.label}>View as</label>
                <div style={st.row}>
                  <select style={st.select} value={viewAsKind} onChange={e => setViewAsKind(e.target.value)} aria-label="View as a role or a user">
                    <option value="role">Role</option>
                    <option value="user">User</option>
                  </select>
                  {viewAsKind === "role" ? (
                    <select style={{ ...st.select, flex: 1 }} value={viewAsRole} onChange={e => setViewAsRole(e.target.value)} aria-label="Role">
                      <option value="viewer">viewer</option>
                      <option value="editor">editor</option>
                      <option value="admin">admin (no masking)</option>
                    </select>
                  ) : (
                    <select style={{ ...st.select, flex: 1 }} value={viewAsUser} onChange={e => setViewAsUser(e.target.value)} aria-label="User">
                      <option value="">Choose a user…</option>
                      {users.map(u => <option key={u.username} value={u.username}>{u.username} ({u.role})</option>)}
                    </select>
                  )}
                </div>
                <div style={st.hint}>
                  Masking and other governance for this identity apply to everyone with the link.
                  {viewAsKind === "role" && viewAsRole === "admin" && <strong style={{ color: "var(--havn-yellow)" }}> Admins are exempt from masking.</strong>}
                </div>
              </div>
            )}

            <div style={st.fieldRow}>
              <div style={{ ...st.field, flex: 1, minWidth: 140 }}>
                <label style={st.label} htmlFor="share-expiry">Expires</label>
                <select id="share-expiry" style={st.select} value={expiry} onChange={e => setExpiry(Number(e.target.value))}>
                  {EXPIRY_CHOICES.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
                </select>
              </div>
              <div style={{ ...st.field, flex: 2, minWidth: 180 }}>
                <label style={st.label} htmlFor="share-label">Label (optional)</label>
                <input id="share-label" style={st.input} value={label} onChange={e => setLabel(e.target.value)} placeholder="e.g. Board pack, Intranet" maxLength={200} />
              </div>
            </div>
            <button style={st.primary} onClick={createLink} disabled={busy || !viewAsReady}>
              {busy ? "Creating…" : mode === "public" ? "Create public link" : "Create link"}
            </button>
          </section>

          <section>
            <h4 style={st.sectionTitle}>Links {active.length > 0 && <span style={st.count}>{active.length} active</span>}</h4>
            {loading && <div style={st.hint}>Loading…</div>}
            {!loading && shares.length === 0 && <div style={st.hint}>Not published yet.</div>}
            {[...active, ...inactive].map(sh => (
              <ShareRow
                key={sh.id}
                share={sh}
                origin={origin}
                title={dashboard.name}
                canManage={sh.mode !== "public" || isAdmin}
                onRevoke={() => revoke(sh)}
                onExpiry={(d) => changeExpiry(sh, d)}
              />
            ))}
          </section>

          <section style={st.reports}>
            <div>
              <h4 style={{ ...st.sectionTitle, margin: 0 }}>Scheduled reports</h4>
              <div style={st.hint}>
                {reportCount === null ? "Email or Slack this dashboard on a schedule." :
                  reportCount === 0 ? "None yet. Email or Slack this dashboard on a schedule, or only when a KPI crosses a threshold." :
                    `${reportCount} report${reportCount === 1 ? "" : "s"} deliver this dashboard.`}
              </div>
            </div>
            <a style={st.secondaryLink} href={`${tabToPath("Reports")}?dashboard=${encodeURIComponent(dashboard.id)}`}>
              {reportCount ? "Manage reports" : "Schedule a report"}
            </a>
          </section>
        </div>
      </div>
    </FocusTrap>
  );
}

function EmbedBlock({ url, title }) {
  const [open, setOpen] = useState(false);
  const snippet = embedSnippet(url, title);
  return (
    <div style={{ marginTop: 10 }}>
      <button type="button" style={st.linkBtn} onClick={() => setOpen(o => !o)} aria-expanded={open}>
        {open ? "Hide embed code" : "Embed in another site"}
      </button>
      {open && (
        <div style={{ marginTop: 8 }}>
          <textarea style={st.code} readOnly value={snippet} onFocus={e => e.target.select()} aria-label="iframe embed snippet" />
          <div style={{ ...st.row, justifyContent: "space-between" }}>
            <span style={st.hint}>The host site must be listed under <code>sharing.embed.allowed_origins</code> in project.yml.</span>
            <CopyButton text={snippet} label="Copy snippet" />
          </div>
        </div>
      )}
    </div>
  );
}

function ShareRow({ share, origin, title, canManage, onRevoke, onExpiry }) {
  const url = share.path ? origin + share.path : null;
  const statusColor = share.status === "active" ? "var(--havn-green)" : "var(--havn-text-dim, var(--havn-text-secondary))";
  const who = share.mode === "public"
    ? `Public · views as ${share.view_as_user || share.view_as_role}`
    : "Signed-in viewers";
  return (
    <div style={{ ...st.shareRow, opacity: share.status === "active" ? 1 : 0.6 }}>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={st.shareTitle}>
          <span style={{ ...st.badge, color: statusColor, borderColor: statusColor }}>{share.status}</span>
          <span>{who}</span>
          {share.label && <span style={st.shareLabel}>{share.label}</span>}
        </div>
        <div style={st.shareMeta}>
          {share.mode === "public" && <span>token …{share.token_hint}</span>}
          <span>{share.expires_at ? `${share.status === "expired" ? "expired" : "expires"} ${fmtDate(share.expires_at)}` : "no expiry"}</span>
          <span>{share.view_count} view{share.view_count === 1 ? "" : "s"}</span>
          <span>by {share.created_by}</span>
        </div>
      </div>
      {share.status === "active" && (
        <div style={st.shareActions}>
          {url && <CopyButton text={url} label="Copy link" />}
          {url && <CopyButton text={embedSnippet(url, title)} label="Embed" />}
          {canManage && (
            <>
              <select
                style={{ ...st.select, padding: "4px 6px", fontSize: 12 }}
                value=""
                onChange={e => { if (e.target.value !== "") onExpiry(Number(e.target.value)); }}
                aria-label="Change expiry"
              >
                <option value="">Expiry…</option>
                {EXPIRY_CHOICES.map(c => <option key={c.value} value={c.value}>{c.value ? `${c.label} from now` : "Never"}</option>)}
              </select>
              <button style={st.danger} onClick={onRevoke}>Revoke</button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

const st = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 10000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 },
  panel: { background: "var(--havn-bg)", border: "1px solid var(--havn-border)", borderRadius: 12, width: 640, maxWidth: "100%", maxHeight: "90vh", display: "flex", flexDirection: "column", boxShadow: "0 12px 40px rgba(0,0,0,0.35)" },
  header: { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, padding: "18px 20px 14px", borderBottom: "1px solid var(--havn-border)" },
  heading: { margin: 0, fontSize: 16, fontWeight: 600, color: "var(--havn-text)" },
  sub: { fontSize: 12.5, color: "var(--havn-text-secondary)", marginTop: 4 },
  close: { background: "none", border: "none", color: "var(--havn-text-secondary)", fontSize: 22, cursor: "pointer", fontFamily: "inherit", lineHeight: 1 },
  body: { padding: "6px 20px 20px", overflowY: "auto", display: "flex", flexDirection: "column", gap: 18 },
  sectionTitle: { margin: "12px 0 10px", fontSize: 13, fontWeight: 600, color: "var(--havn-text)", display: "flex", alignItems: "center", gap: 8 },
  count: { fontSize: 11, fontWeight: 500, color: "var(--havn-text-secondary)" },
  segmented: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 8, marginBottom: 12 },
  segment: { textAlign: "left", background: "var(--havn-bg-secondary, var(--havn-bg))", border: "1px solid var(--havn-border)", borderRadius: 8, padding: "10px 12px", cursor: "pointer", fontFamily: "inherit", display: "flex", flexDirection: "column", gap: 3, color: "var(--havn-text)" },
  segmentOn: { borderColor: "var(--havn-accent)", boxShadow: "inset 0 0 0 1px var(--havn-accent)" },
  segmentTitle: { fontSize: 13, fontWeight: 600 },
  segmentHint: { fontSize: 11.5, color: "var(--havn-text-secondary)", lineHeight: 1.4 },
  field: { display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 },
  fieldRow: { display: "flex", gap: 10, flexWrap: "wrap" },
  label: { fontSize: 12, color: "var(--havn-text-secondary)" },
  hint: { fontSize: 12, color: "var(--havn-text-secondary)", lineHeight: 1.45, marginTop: 2 },
  input: { background: "var(--havn-bg-secondary, var(--havn-bg))", border: "1px solid var(--havn-border)", borderRadius: 6, color: "var(--havn-text)", padding: "7px 9px", fontSize: 13, minWidth: 0, fontFamily: "inherit" },
  select: { background: "var(--havn-bg-secondary, var(--havn-bg))", border: "1px solid var(--havn-border)", borderRadius: 6, color: "var(--havn-text)", padding: "7px 8px", fontSize: 13, minWidth: 0, fontFamily: "inherit" },
  row: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
  primary: { background: "var(--havn-accent)", color: "var(--havn-bg)", border: "none", borderRadius: 6, padding: "8px 14px", fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" },
  secondary: { background: "none", border: "1px solid var(--havn-border)", color: "var(--havn-text)", borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap" },
  secondaryLink: { border: "1px solid var(--havn-border)", color: "var(--havn-text)", borderRadius: 6, padding: "6px 12px", fontSize: 12.5, textDecoration: "none", whiteSpace: "nowrap" },
  danger: { background: "none", border: "1px solid color-mix(in srgb, var(--havn-red) 50%, transparent)", color: "var(--havn-red)", borderRadius: 6, padding: "5px 10px", fontSize: 12, cursor: "pointer", fontFamily: "inherit" },
  linkBtn: { background: "none", border: "none", color: "var(--havn-accent)", padding: 0, fontSize: 12.5, cursor: "pointer", fontFamily: "inherit" },
  code: { width: "100%", minHeight: 64, resize: "vertical", fontFamily: "var(--havn-font-mono)", fontSize: 11.5, background: "var(--havn-bg-secondary, var(--havn-bg))", color: "var(--havn-text)", border: "1px solid var(--havn-border)", borderRadius: 6, padding: 8, marginBottom: 6 },
  error: { background: "color-mix(in srgb, var(--havn-red) 12%, transparent)", color: "var(--havn-red)", border: "1px solid color-mix(in srgb, var(--havn-red) 35%, transparent)", borderRadius: 6, padding: "8px 10px", fontSize: 12.5, marginTop: 10 },
  warn: { fontSize: 12.5, color: "var(--havn-text)", lineHeight: 1.5, margin: "4px 0 10px" },
  created: { marginTop: 12, border: "1px solid var(--havn-accent)", borderRadius: 8, padding: 14, background: "color-mix(in srgb, var(--havn-accent) 6%, transparent)" },
  createdTitle: { fontSize: 13, fontWeight: 600, color: "var(--havn-text)", marginBottom: 6 },
  shareRow: { display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", padding: "10px 0", borderTop: "1px solid var(--havn-border)", flexWrap: "wrap" },
  shareTitle: { display: "flex", gap: 8, alignItems: "center", fontSize: 13, color: "var(--havn-text)", flexWrap: "wrap" },
  shareLabel: { color: "var(--havn-text-secondary)", fontSize: 12 },
  shareMeta: { display: "flex", gap: 10, flexWrap: "wrap", fontSize: 11.5, color: "var(--havn-text-secondary)", marginTop: 4 },
  shareActions: { display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" },
  badge: { fontSize: 10.5, textTransform: "uppercase", letterSpacing: "0.04em", border: "1px solid", borderRadius: 999, padding: "1px 7px", fontWeight: 600 },
  reports: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, borderTop: "1px solid var(--havn-border)", paddingTop: 14, flexWrap: "wrap" },
};

import React, { useState, useEffect, useCallback } from 'react';
import { api } from './api';
import { useAuth } from './AuthContext';

const ROLES = ['admin', 'editor', 'viewer'];
const csv = (s) => s.split(',').map((x) => x.trim()).filter(Boolean);
const list = (a) => (a && a.length ? a.join(', ') : '—');

function emptyPolicy() {
  return {
    name: '', schema_name: '', table_name: '', filter_sql: '', description: '',
    applies_to_roles: [], applies_to_users: '', exempted_roles: ['admin'], exempted_users: '',
    follow_lineage: true, enabled: true,
  };
}

function fromPolicy(p) {
  return {
    ...emptyPolicy(), ...p,
    name: p.name || '', description: p.description || '',
    applies_to_users: (p.applies_to_users || []).join(', '),
    exempted_users: (p.exempted_users || []).join(', '),
    applies_to_roles: p.applies_to_roles || [], exempted_roles: p.exempted_roles || [],
  };
}

function RoleChecks({ value, onChange, label }) {
  return (
    <div style={{ display: 'flex', gap: 10 }} role="group" aria-label={label}>
      {ROLES.map((r) => (
        <label key={r} style={s.checkLabel}>
          <input type="checkbox" checked={value.includes(r)}
            onChange={() => onChange(value.includes(r) ? value.filter((x) => x !== r) : [...value, r])} />
          {r}
        </label>
      ))}
    </div>
  );
}

function FunctionHelp() {
  return (
    <div style={s.help}>
      The filter is a SQL boolean over the table's columns. It reads the viewer through:
      <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
        <li><code style={s.code}>havn_user()</code> the viewer's username</li>
        <li><code style={s.code}>havn_role()</code> the viewer's role</li>
        <li><code style={s.code}>havn_attr('key')</code> a user attribute (NULL when unset, so the row is hidden)</li>
        <li><code style={s.code}>havn_attr_list('key')</code> an attribute as a list</li>
      </ul>
      Example: <code style={s.code}>region = havn_attr('region')</code>. Naming no roles or users applies the policy to everybody
      except the exemptions.
    </div>
  );
}

function PolicyForm({ initial, tables, onSave, onCancel, saving }) {
  const [f, setF] = useState(initial);
  const isEdit = !!initial.id;
  const set = (k, v) => setF((x) => ({ ...x, [k]: v }));
  const canSave = f.schema_name.trim() && f.table_name.trim() && f.filter_sql.trim() && !saving;

  function submit() {
    onSave({
      name: f.name.trim() || null,
      schema_name: f.schema_name.trim(), table_name: f.table_name.trim(),
      filter_sql: f.filter_sql.trim(), description: f.description.trim() || null,
      applies_to_roles: f.applies_to_roles, applies_to_users: csv(f.applies_to_users),
      exempted_roles: f.exempted_roles, exempted_users: csv(f.exempted_users),
      follow_lineage: f.follow_lineage, enabled: f.enabled,
    });
  }

  return (
    <div style={s.form}>
      <div style={s.formTitle}>{isEdit ? 'Edit row policy' : 'New row policy'}</div>
      <div style={s.fieldRow}>
        <div style={s.field}>
          <label style={s.label} htmlFor="rp-schema">Schema</label>
          <input id="rp-schema" style={s.input} list="rp-schemas" value={f.schema_name} onChange={(e) => set('schema_name', e.target.value)} placeholder="silver" />
          <datalist id="rp-schemas">{[...new Set(tables.map((t) => t.schema))].map((x) => <option key={x} value={x} />)}</datalist>
        </div>
        <div style={s.field}>
          <label style={s.label} htmlFor="rp-table">Table</label>
          <input id="rp-table" style={s.input} list="rp-tables" value={f.table_name} onChange={(e) => set('table_name', e.target.value)} placeholder="customers" />
          <datalist id="rp-tables">{tables.filter((t) => !f.schema_name || t.schema === f.schema_name).map((t) => <option key={t.schema + t.name} value={t.name} />)}</datalist>
        </div>
        <div style={s.field}>
          <label style={s.label} htmlFor="rp-name">Name (optional)</label>
          <input id="rp-name" style={s.input} value={f.name} onChange={(e) => set('name', e.target.value)} placeholder="regional access" />
        </div>
      </div>
      <div>
        <label style={s.label} htmlFor="rp-filter">Filter SQL</label>
        <textarea id="rp-filter" style={{ ...s.input, fontFamily: 'var(--havn-font-mono)', minHeight: 52, resize: 'vertical' }}
          value={f.filter_sql} onChange={(e) => set('filter_sql', e.target.value)} placeholder="region = havn_attr('region')" />
      </div>
      <FunctionHelp />
      <div style={s.fieldRow}>
        <div style={s.field}>
          <label style={s.label}>Applies to roles</label>
          <RoleChecks label="Applies to roles" value={f.applies_to_roles} onChange={(v) => set('applies_to_roles', v)} />
        </div>
        <div style={s.field}>
          <label style={s.label} htmlFor="rp-users">Applies to users</label>
          <input id="rp-users" style={s.input} value={f.applies_to_users} onChange={(e) => set('applies_to_users', e.target.value)} placeholder="comma separated; empty = all" />
        </div>
      </div>
      <div style={s.fieldRow}>
        <div style={s.field}>
          <label style={s.label}>Exempt roles</label>
          <RoleChecks label="Exempt roles" value={f.exempted_roles} onChange={(v) => set('exempted_roles', v)} />
        </div>
        <div style={s.field}>
          <label style={s.label} htmlFor="rp-exusers">Exempt users</label>
          <input id="rp-exusers" style={s.input} value={f.exempted_users} onChange={(e) => set('exempted_users', e.target.value)} placeholder="comma separated" />
        </div>
      </div>
      <div style={{ display: 'flex', gap: 18 }}>
        <label style={s.checkLabel}><input type="checkbox" checked={f.enabled} onChange={(e) => set('enabled', e.target.checked)} /> Enabled</label>
        <label style={s.checkLabel}><input type="checkbox" checked={f.follow_lineage} onChange={(e) => set('follow_lineage', e.target.checked)} /> Follow lineage into downstream models</label>
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button style={s.btnCancel} onClick={onCancel}>Cancel</button>
        <button style={s.btnPrimary} onClick={submit} disabled={!canSave}>{saving ? 'Saving...' : isEdit ? 'Save' : 'Create'}</button>
      </div>
    </div>
  );
}

function RowPolicies({ canManage, showConfirm, summary, onChanged }) {
  const [policies, setPolicies] = useState([]);
  const [tables, setTables] = useState([]);
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    if (!canManage) return;
    try { setPolicies((await api.listRowPolicies()) || []); }
    catch (e) { setError(e.message || 'Failed to load row policies'); }
  }, [canManage]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (canManage) api.listTables().then((t) => setTables(t || [])).catch(() => {}); }, [canManage]);

  async function save(payload) {
    setSaving(true); setError(null);
    try {
      if (form.id) await api.updateRowPolicy(form.id, payload); else await api.createRowPolicy(payload);
      setForm(null); await load(); onChanged();
    } catch (e) { setError(e.message || 'Save failed'); }
    finally { setSaving(false); }
  }

  async function remove(p) {
    if (showConfirm && !(await showConfirm('Delete Row Policy', `Delete the row policy on ${p.schema_name}.${p.table_name}? Viewers it restricted will see every row.`, 'Delete', true))) return;
    try { await api.deleteRowPolicy(p.id); await load(); onChanged(); }
    catch (e) { setError(e.message || 'Delete failed'); }
  }

  async function toggle(p) {
    try { await api.updateRowPolicy(p.id, { enabled: !p.enabled }); await load(); onChanged(); }
    catch (e) { setError(e.message || 'Update failed'); }
  }

  // A non-admin gets the summary only: relation, where it came from, whether it denies.
  const readOnly = !canManage ? (summary?.row_policies || []) : [];

  return (
    <div>
      <div style={s.sectionHead}>
        <div>
          <div style={s.h2}>Row policies</div>
          <div style={s.desc}>Filter the rows a viewer sees in a table. They follow lineage into downstream models and apply on every surface: queries, exports, dashboards and the API.</div>
        </div>
        {canManage && !form && <button style={s.btnPrimary} onClick={() => setForm(emptyPolicy())}>Add Row Policy</button>}
      </div>
      {error && <div style={s.error}>{error}</div>}
      {form && <PolicyForm initial={form} tables={tables} onSave={save} onCancel={() => setForm(null)} saving={saving} />}
      {canManage && policies.length === 0 && !form && (
        <div style={s.empty}>No row policies yet. Everyone who can read a table sees all of its rows.</div>
      )}
      {canManage && policies.length > 0 && (
        <table style={s.table}>
          <thead><tr>
            <th style={s.th}>Table</th><th style={s.th}>Filter</th><th style={s.th}>Applies to</th><th style={s.th}>Exempt</th><th style={s.th}>Status</th><th style={s.th}></th>
          </tr></thead>
          <tbody>
            {policies.map((p) => (
              <tr key={p.id}>
                <td style={s.td}><strong>{p.schema_name}.{p.table_name}</strong>{p.name ? <div style={s.dim}>{p.name}</div> : null}</td>
                <td style={s.td}><code style={s.code}>{p.filter_sql}</code></td>
                <td style={s.td}>
                  {(p.applies_to_roles || []).length + (p.applies_to_users || []).length === 0
                    ? <span style={s.dim}>everybody</span>
                    : <>{list(p.applies_to_roles)}{(p.applies_to_users || []).length ? <div style={s.dim}>users: {list(p.applies_to_users)}</div> : null}</>}
                </td>
                <td style={s.td}>{list(p.exempted_roles)}{(p.exempted_users || []).length ? <div style={s.dim}>users: {list(p.exempted_users)}</div> : null}</td>
                <td style={s.td}>
                  <span style={{ ...s.badge, ...(p.enabled ? s.badgeOn : {}) }}>{p.enabled ? 'enabled' : 'disabled'}</span>
                </td>
                <td style={{ ...s.td, textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <button style={s.actionBtn} onClick={() => toggle(p)} disabled={!!form}>{p.enabled ? 'Disable' : 'Enable'}</button>{' '}
                  <button style={s.actionBtn} onClick={() => setForm(fromPolicy(p))} disabled={!!form}>Edit</button>{' '}
                  <button style={s.actionBtnDanger} onClick={() => remove(p)} disabled={!!form}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!canManage && (
        readOnly.length === 0
          ? <div style={s.empty}>No row policies are in effect.</div>
          : <table style={s.table}>
              <thead><tr><th style={s.th}>Table</th><th style={s.th}>Source</th><th style={s.th}>Effect</th></tr></thead>
              <tbody>{readOnly.map((p, i) => (
                <tr key={i}>
                  <td style={s.td}><strong>{p.relation}</strong></td>
                  <td style={s.td}>{p.inherited_from ? `inherited from ${p.inherited_from}` : 'explicit'}</td>
                  <td style={s.td}>{p.deny ? 'no rows' : 'filtered'}</td>
                </tr>))}</tbody>
            </table>
      )}
    </div>
  );
}

// Which policies would touch this user: enabled, targeted at them, not exempt.
export function policiesFor(policies, user) {
  if (!user) return [];
  return policies.filter((p) => {
    if (p.enabled === false) return false;
    const roles = p.applies_to_roles || [], users = p.applies_to_users || [];
    const targeted = roles.length + users.length === 0 || roles.includes(user.role) || users.includes(user.username);
    const exempt = (p.exempted_roles || []).includes(user.role) || (p.exempted_users || []).includes(user.username);
    return targeted && !exempt;
  });
}

function Preview({ canManage, summary }) {
  const [users, setUsers] = useState([]);
  const [tables, setTables] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [username, setUsername] = useState('');
  const [sql, setSql] = useState('');
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!canManage) return;
    api.listUsers().then((u) => { setUsers(u || []); if (u?.length) setUsername((x) => x || u[0].username); }).catch(() => {});
    api.listTables().then((t) => setTables(t || [])).catch(() => {});
    api.listRowPolicies().then((p) => setPolicies(p || [])).catch(() => {});
  }, [canManage]);

  if (!canManage) {
    return <div style={s.empty}>Previewing a query as another user is available to admins.</div>;
  }

  async function run() {
    setRunning(true); setError(null); setResult(null);
    try { setResult(await api.previewAsUser(username, sql)); }
    catch (e) { setError(e.message || 'Preview failed'); }
    finally { setRunning(false); }
  }

  const target = users.find((u) => u.username === username);
  const applied = policiesFor(policies, target);
  const inherited = (summary?.row_policies || []).filter((p) => p.inherited_from);

  return (
    <div>
      <div style={s.h2}>Preview as user</div>
      <div style={s.desc}>Run a read-only query the way another user would see it, with masking and row policies applied.</div>
      <div style={s.fieldRow}>
        <div style={{ ...s.field, flex: '0 0 200px' }}>
          <label style={s.label} htmlFor="pv-user">User</label>
          <select id="pv-user" style={s.input} value={username} onChange={(e) => setUsername(e.target.value)}>
            {users.map((u) => <option key={u.username} value={u.username}>{u.username} ({u.role})</option>)}
          </select>
        </div>
        <div style={{ ...s.field, flex: '0 0 240px' }}>
          <label style={s.label} htmlFor="pv-table">Pick a table</label>
          <select id="pv-table" style={s.input} value="" onChange={(e) => e.target.value && setSql(`SELECT * FROM ${e.target.value}`)}>
            <option value="">Choose to fill the query...</option>
            {tables.filter((t) => t.schema !== '_havn').map((t) => <option key={t.schema + t.name} value={`${t.schema}.${t.name}`}>{t.schema}.{t.name}</option>)}
          </select>
        </div>
      </div>
      <label style={s.label} htmlFor="pv-sql">Query</label>
      <textarea id="pv-sql" style={{ ...s.input, fontFamily: 'var(--havn-font-mono)', minHeight: 70, resize: 'vertical' }}
        value={sql} onChange={(e) => setSql(e.target.value)} placeholder="SELECT * FROM silver.customers" />
      <div style={{ margin: '8px 0 12px' }}>
        <button style={s.btnPrimary} onClick={run} disabled={!username || !sql.trim() || running}>{running ? 'Running...' : 'Run as user'}</button>
      </div>
      {error && <div style={s.error}>{error}</div>}
      {result?.refused && <div style={s.error}>Refused for {result.username}: {result.refused}</div>}
      {result && !result.refused && (
        <div>
          <div style={s.note}>
            Viewed as <strong>{result.username}</strong> ({result.role}), {result.rows.length} row{result.rows.length === 1 ? '' : 's'}.
            {Object.keys(result.attributes || {}).length > 0 && <> Attributes: <code style={s.code}>{JSON.stringify(result.attributes)}</code></>}
          </div>
          <div style={s.note}>
            {applied.length === 0 && inherited.length === 0
              ? 'No explicit row policy applies to this user.'
              : <>Row policies that apply to this user:
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  {applied.map((p) => <li key={p.id}><strong>{p.schema_name}.{p.table_name}</strong>: <code style={s.code}>{p.filter_sql}</code></li>)}
                  {inherited.map((p, i) => <li key={'i' + i}><strong>{p.relation}</strong>: inherited from {p.inherited_from}{p.deny ? ' (denies all rows)' : ''}</li>)}
                </ul>
                <div style={s.dim}>Only policies on tables the query reads take effect.</div></>}
          </div>
          <div style={{ overflow: 'auto', maxHeight: 360, border: '1px solid var(--havn-border)', borderRadius: 'var(--havn-radius)' }}>
            <table style={s.table}>
              <thead><tr>{result.columns.map((c) => <th key={c} style={s.th}>{c}</th>)}</tr></thead>
              <tbody>
                {result.rows.map((r, i) => (
                  <tr key={i}>{(Array.isArray(r) ? r : result.columns.map((c) => r[c])).map((v, j) => <td key={j} style={s.td}>{v === null ? <span style={s.dim}>NULL</span> : String(v)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Classifications({ summary }) {
  const tables = summary?.classifications || [];
  const declassified = summary?.declassified || [];
  return (
    <div>
      <div style={s.h2}>Classifications</div>
      <div style={s.desc}>Columns that carry personal data, tagged with <code style={s.code}>@pii</code>, set by a masking policy, or inherited through lineage. Read-only: change them in the model SQL or the masking policies.</div>
      {tables.length === 0 ? <div style={s.empty}>No classified columns.</div> : (
        <table style={s.table}>
          <thead><tr><th style={s.th}>Table</th><th style={s.th}>Column</th><th style={s.th}>Origin</th><th style={s.th}>Masked</th></tr></thead>
          <tbody>
            {tables.flatMap((t) => t.columns.map((c) => (
              <tr key={t.relation + c.column}>
                <td style={s.td}>{t.relation}</td>
                <td style={s.td}><code style={s.code}>{c.column}</code></td>
                <td style={s.td}>{(c.from || []).length ? `inherited from ${c.from.join(', ')}` : 'declared'}</td>
                <td style={s.td}><span style={{ ...s.badge, ...(c.masked ? s.badgeOn : {}) }}>{c.masked ? 'masked' : 'not masked'}</span></td>
              </tr>
            )))}
          </tbody>
        </table>
      )}
      {declassified.length > 0 && (
        <>
          <div style={{ ...s.h2, marginTop: 20 }}>Declassified</div>
          <table style={s.table}>
            <tbody>{declassified.map((d, i) => (
              <tr key={i}><td style={s.td}>{d.relation}</td><td style={s.td}><code style={s.code}>{d.column}</code></td><td style={s.td}>{d.reason}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}
      {(summary?.notes || []).length > 0 && (
        <ul style={{ ...s.desc, paddingLeft: 18, marginTop: 16 }}>{summary.notes.map((n, i) => <li key={i}>{typeof n === 'string' ? n : JSON.stringify(n)}</li>)}</ul>
      )}
    </div>
  );
}

const VIEWS = ['Row policies', 'Preview as user', 'Classifications'];

export default function GovernancePanel({ showConfirm }) {
  const { currentUser } = useAuth();
  const canManage = currentUser?.role === 'admin';
  const [view, setView] = useState(VIEWS[0]);
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  const loadSummary = useCallback(() => {
    api.getGovernance().then(setSummary).catch((e) => setError(e.message || 'Failed to load governance summary'));
  }, []);
  useEffect(() => { loadSummary(); }, [loadSummary]);

  return (
    <div style={s.container}>
      <div style={s.header}>
        <span style={s.headerTitle}>Governance</span>
        <div style={s.tabs} role="tablist">
          {VIEWS.map((v) => (
            <button key={v} role="tab" aria-selected={view === v} onClick={() => setView(v)}
              style={{ ...s.tab, ...(view === v ? s.tabActive : {}) }}>{v}</button>
          ))}
        </div>
      </div>
      <div style={s.content}>
        {error && <div style={s.error}>{error}</div>}
        {view === 'Row policies' && <RowPolicies canManage={canManage} showConfirm={showConfirm} summary={summary} onChanged={loadSummary} />}
        {view === 'Preview as user' && <Preview canManage={canManage} summary={summary} />}
        {view === 'Classifications' && <Classifications summary={summary} />}
      </div>
    </div>
  );
}

const s = {
  container: { display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden', background: 'var(--havn-bg)' },
  header: { display: 'flex', alignItems: 'center', gap: 16, padding: '8px 12px', borderBottom: '1px solid var(--havn-border)' },
  headerTitle: { fontSize: 13, fontWeight: 600, color: 'var(--havn-text)' },
  tabs: { display: 'flex', gap: 4 },
  tab: { padding: '4px 12px', background: 'none', border: '1px solid transparent', borderRadius: 'var(--havn-radius)', color: 'var(--havn-text-secondary)', cursor: 'pointer', fontSize: 12 },
  tabActive: { background: 'color-mix(in srgb, var(--havn-accent) 12%, transparent)', color: 'var(--havn-accent)', borderColor: 'var(--havn-border-light)' },
  content: { flex: 1, overflow: 'auto', padding: 20 },
  sectionHead: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, marginBottom: 12 },
  h2: { fontSize: 15, fontWeight: 600, color: 'var(--havn-text)', marginBottom: 4 },
  desc: { fontSize: 12, color: 'var(--havn-text-secondary)', lineHeight: 1.6, marginBottom: 12, maxWidth: 720 },
  help: { fontSize: 11, color: 'var(--havn-text-secondary)', lineHeight: 1.6, padding: '8px 10px', background: 'var(--havn-bg-tertiary)', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius)' },
  note: { fontSize: 12, color: 'var(--havn-text-secondary)', marginBottom: 8, lineHeight: 1.6 },
  dim: { fontSize: 11, color: 'var(--havn-text-dim)' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', padding: '8px 12px', borderBottom: '1px solid var(--havn-border-light)', color: 'var(--havn-text-secondary)', fontSize: 11, textTransform: 'uppercase' },
  td: { padding: '8px 12px', borderBottom: '1px solid var(--havn-border)', color: 'var(--havn-text)', verticalAlign: 'top' },
  code: { background: 'color-mix(in srgb, var(--havn-accent) 8%, transparent)', padding: '1px 5px', borderRadius: 3, fontSize: 12, fontFamily: 'var(--havn-font-mono)', color: 'var(--havn-accent)' },
  badge: { fontSize: 10, padding: '1px 6px', borderRadius: 3, background: 'color-mix(in srgb, var(--havn-text-secondary) 15%, transparent)', color: 'var(--havn-text-secondary)' },
  badgeOn: { background: 'color-mix(in srgb, var(--havn-green) 18%, transparent)', color: 'var(--havn-green)' },
  btnPrimary: { padding: '5px 14px', background: 'var(--havn-green)', color: '#fff', border: '1px solid var(--havn-green-border)', borderRadius: 'var(--havn-radius-lg)', cursor: 'pointer', fontSize: 11, fontWeight: 500, whiteSpace: 'nowrap' },
  btnCancel: { padding: '5px 14px', background: 'none', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius-lg)', color: 'var(--havn-text-secondary)', cursor: 'pointer', fontSize: 11, fontWeight: 500 },
  actionBtn: { padding: '3px 10px', background: 'var(--havn-btn-bg)', border: '1px solid var(--havn-btn-border)', borderRadius: 'var(--havn-radius)', cursor: 'pointer', fontSize: 11, fontWeight: 500, color: 'var(--havn-text)' },
  actionBtnDanger: { padding: '3px 10px', background: 'none', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius)', cursor: 'pointer', fontSize: 11, fontWeight: 500, color: 'var(--havn-red)' },
  error: { padding: '8px 12px', background: 'color-mix(in srgb, var(--havn-red) 12%, transparent)', color: 'var(--havn-red)', borderRadius: 'var(--havn-radius-lg)', marginBottom: 12, fontSize: 13 },
  empty: { padding: '40px 20px', textAlign: 'center', color: 'var(--havn-text-secondary)', fontSize: 13 },
  form: { display: 'flex', flexDirection: 'column', gap: 10, padding: '12px 16px', marginBottom: 16, background: 'color-mix(in srgb, var(--havn-accent) 4%, var(--havn-bg))', border: '1px solid var(--havn-accent)', borderRadius: 'var(--havn-radius)' },
  formTitle: { fontSize: 12, fontWeight: 600, color: 'var(--havn-accent)', textTransform: 'uppercase', letterSpacing: '0.3px' },
  fieldRow: { display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-start', marginBottom: 6 },
  field: { flex: 1, minWidth: 160 },
  label: { display: 'block', fontSize: 11, color: 'var(--havn-text-secondary)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.3px' },
  input: { padding: '5px 8px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius)', fontSize: 12, width: '100%', boxSizing: 'border-box' },
  checkLabel: { display: 'flex', alignItems: 'center', gap: 4, color: 'var(--havn-text)', fontSize: 12, cursor: 'pointer' },
};

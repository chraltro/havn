import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from './api';
import { useAuth } from './AuthContext';
import { schemaCompare } from './schemaOrder';

const ROLES = ['admin', 'editor', 'viewer'];

const CATEGORY_COLORS = {
  general: { bg: 'color-mix(in srgb, var(--havn-text-secondary) 15%, transparent)', color: 'var(--havn-text-secondary)' },
  pii: { bg: 'color-mix(in srgb, var(--havn-accent) 15%, transparent)', color: 'var(--havn-accent)' },
  financial: { bg: 'color-mix(in srgb, var(--havn-yellow) 15%, transparent)', color: 'var(--havn-yellow)' },
  analytics: { bg: 'color-mix(in srgb, var(--havn-purple) 15%, transparent)', color: 'var(--havn-purple)' },
};

const CATEGORY_ORDER = ['general', 'pii', 'financial', 'analytics'];
const CATEGORY_LABELS = { general: 'General', pii: 'PII', financial: 'Financial', analytics: 'Analytics' };

function emptyPolicy(authRequired) {
  // When auth is disabled the local user is auto-admin, so the legacy default
  // of exempted_roles=['admin'] makes new policies silently inert for the
  // only user who exists. Default to no exemption in no-auth mode so the
  // policy actually masks for the caller.
  return {
    schema_name: '', table_name: '', column_name: '', method: 'hash',
    method_config: {}, condition_column: '', condition_value: '',
    exempted_roles: authRequired ? ['admin'] : [],
  };
}

function formatConfig(method, config) {
  if (!config || Object.keys(config).length === 0) return '\u2014';
  const parts = [];
  for (const [key, val] of Object.entries(config)) {
    if (val === '' || val === null || val === undefined) continue;
    const label = key.replace(/_/g, ' ');
    parts.push(`${label}: ${val}`);
  }
  return parts.length > 0 ? parts.join(', ') : '\u2014';
}

function useSortable(defaultKey, defaultDir = 'asc') {
  const [sortKey, setSortKey] = useState(defaultKey);
  const [sortDir, setSortDir] = useState(defaultDir);
  const toggle = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('asc'); }
  };
  return { sortKey, sortDir, toggle };
}

function sortData(data, key, dir, getter) {
  if (!key) return data;
  return [...data].sort((a, b) => {
    const va = getter(a, key);
    const vb = getter(b, key);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return dir === 'asc' ? va - vb : vb - va;
    const sa = String(va).toLowerCase(), sb = String(vb).toLowerCase();
    return dir === 'asc' ? sa.localeCompare(sb) : sb.localeCompare(sa);
  });
}

function SortTh({ label, sortKey, current, dir, onToggle, style }) {
  const arrow = current === sortKey ? (dir === 'asc' ? ' \u25B4' : ' \u25BE') : '';
  return (
    <th style={{ ...style, cursor: 'pointer', userSelect: 'none' }} onClick={() => onToggle(sortKey)}>
      {label}{arrow}
    </th>
  );
}

function MethodBadge({ method, methods }) {
  const info = methods.find(m => m.id === method);
  const category = info?.category || 'general';
  const colors = CATEGORY_COLORS[category] || CATEGORY_COLORS.general;
  return (
    <span style={{ padding: '2px 8px', borderRadius: 4, fontSize: 11, fontWeight: 500, background: colors.bg, color: colors.color }}>
      {info?.name || method}
    </span>
  );
}

function RoleBadge({ role }) {
  return (
    <span style={{ padding: '1px 6px', borderRadius: 3, fontSize: 10, fontWeight: 500, background: 'color-mix(in srgb, var(--havn-accent) 12%, transparent)', color: 'var(--havn-accent)', marginRight: 4 }}>
      {role}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Inline form row (replaces modal)                                    */
/* ------------------------------------------------------------------ */

function PolicyFormRow({ initial, methods, onSave, onCancel, saving, colSpan }) {
  const [form, setForm] = useState(initial);
  const [showCondition, setShowCondition] = useState(!!(initial.condition_column));
  const isEdit = !!(initial.id);

  // Cascading dropdowns: schemas -> tables -> columns
  const [schemas, setSchemas] = useState([]);
  const [tablesForSchema, setTablesForSchema] = useState([]);
  const [columnsForTable, setColumnsForTable] = useState([]);

  useEffect(() => {
    api.listTables().then(tables => {
      const unique = [...new Set(tables.map(t => t.schema))].filter(s => s !== '_havn' && s !== 'information_schema').sort(schemaCompare);
      setSchemas(unique);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (!form.schema_name) { setTablesForSchema([]); setColumnsForTable([]); return; }
    api.listTables(form.schema_name).then(tables => {
      setTablesForSchema(tables.filter(t => t.schema === form.schema_name).map(t => t.name).sort());
    }).catch(() => setTablesForSchema([]));
    if (!isEdit) setColumnsForTable([]);
  }, [form.schema_name, isEdit]);

  useEffect(() => {
    if (!form.schema_name || !form.table_name) { setColumnsForTable([]); return; }
    api.describeTable(form.schema_name, form.table_name).then(desc => {
      setColumnsForTable((desc.columns || []).map(c => c.name).sort());
    }).catch(() => setColumnsForTable([]));
  }, [form.schema_name, form.table_name]);

  const updateForm = useCallback((key, value) => setForm(f => ({ ...f, [key]: value })), []);

  const toggleRole = useCallback((role) => {
    setForm(f => ({
      ...f,
      exempted_roles: f.exempted_roles.includes(role)
        ? f.exempted_roles.filter(r => r !== role)
        : [...f.exempted_roles, role]
    }));
  }, []);

  const selectedMethod = useMemo(() => methods.find(m => m.id === form.method), [methods, form.method]);

  const handleMethodChange = useCallback((methodId) => {
    const newMethod = methods.find(m => m.id === methodId);
    const newConfig = {};
    if (newMethod?.config) {
      for (const field of newMethod.config) {
        newConfig[field.key] = field.default ?? '';
      }
    }
    setForm(f => ({ ...f, method: methodId, method_config: newConfig }));
  }, [methods]);

  const updateConfig = useCallback((key, value, type) => {
    setForm(f => ({
      ...f,
      method_config: {
        ...f.method_config,
        [key]: type === 'int' ? (parseInt(value) || 0) : type === 'float' ? (parseFloat(value) || 0) : value,
      }
    }));
  }, []);

  const grouped = useMemo(() => {
    const groups = {};
    for (const cat of CATEGORY_ORDER) groups[cat] = [];
    for (const m of methods) {
      const cat = m.category || 'general';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(m);
    }
    return groups;
  }, [methods]);

  const handleSubmit = useCallback(() => {
    const payload = { ...form };
    delete payload.id;
    if (!selectedMethod?.config?.length) delete payload.method_config;
    else {
      const cleaned = {};
      for (const [k, v] of Object.entries(payload.method_config || {})) {
        if (v !== '' && v !== null && v !== undefined) cleaned[k] = v;
      }
      payload.method_config = Object.keys(cleaned).length > 0 ? cleaned : undefined;
      if (!payload.method_config) delete payload.method_config;
    }
    if (!payload.condition_column) { delete payload.condition_column; delete payload.condition_value; }
    onSave(payload);
  }, [form, selectedMethod, onSave]);

  const canSave = form.schema_name && form.table_name && form.column_name && !saving;

  return (
    <tr>
      <td colSpan={colSpan} style={{ padding: 0, borderBottom: '1px solid var(--havn-border)' }}>
        <div style={s.formRow}>
          <div style={s.formRowHeader}>
            <span style={s.formRowTitle}>{isEdit ? 'Edit Policy' : 'New Policy'}</span>
          </div>
          <div style={s.formRowBody}>
            {/* Target row */}
            <div style={s.formFieldRow}>
              <div style={s.formField}>
                <label style={s.label}>Schema</label>
                <select style={s.select} value={form.schema_name} onChange={e => { updateForm('schema_name', e.target.value); updateForm('table_name', ''); updateForm('column_name', ''); }}>
                  <option value="">Select...</option>
                  {schemas.map(sc => <option key={sc} value={sc}>{sc}</option>)}
                </select>
              </div>
              <div style={s.formField}>
                <label style={s.label}>Table</label>
                <select style={s.select} value={form.table_name} onChange={e => { updateForm('table_name', e.target.value); updateForm('column_name', ''); }} disabled={!form.schema_name}>
                  <option value="">Select...</option>
                  {tablesForSchema.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <div style={s.formField}>
                <label style={s.label}>Column</label>
                <select style={s.select} value={form.column_name} onChange={e => updateForm('column_name', e.target.value)} disabled={!form.table_name}>
                  <option value="">Select...</option>
                  {columnsForTable.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
              </div>
              <div style={{ ...s.formField, flex: 1.5 }}>
                <label style={s.label}>Masking Method</label>
                <select style={s.select} value={form.method} onChange={e => handleMethodChange(e.target.value)}>
                  {CATEGORY_ORDER.map(cat => (
                    grouped[cat]?.length > 0 && (
                      <optgroup key={cat} label={CATEGORY_LABELS[cat]}>
                        {grouped[cat].map(m => (
                          <option key={m.id} value={m.id}>{m.name}</option>
                        ))}
                      </optgroup>
                    )
                  ))}
                </select>
              </div>
            </div>

            {/* Method info */}
            {selectedMethod && (
              <div style={s.methodInfo}>
                <span style={s.methodDescription}>{selectedMethod.description}</span>
                {selectedMethod.example && (
                  <span style={{ marginLeft: 12 }}>
                    <code style={s.exampleCode}>{selectedMethod.example.input}</code>
                    <span style={s.exampleArrow}>{' \u2192 '}</span>
                    <code style={s.exampleCode}>{selectedMethod.example.output}</code>
                  </span>
                )}
              </div>
            )}

            {/* Config fields + roles on one row */}
            <div style={s.formFieldRow}>
              {selectedMethod?.config?.length > 0 && selectedMethod.config.map(field => (
                <div key={field.key} style={s.formField}>
                  <label style={s.label}>{field.label}</label>
                  <input
                    style={s.input}
                    type={field.type === 'int' || field.type === 'float' ? 'number' : 'text'}
                    step={field.type === 'float' ? '0.1' : undefined}
                    value={form.method_config[field.key] ?? field.default ?? ''}
                    onChange={e => updateConfig(field.key, e.target.value, field.type)}
                    placeholder={field.default != null ? String(field.default) : ''}
                  />
                </div>
              ))}
              <div style={s.formField}>
                <label style={s.label}>Exempted Roles</label>
                <div style={{ display: 'flex', gap: 10, paddingTop: 4 }}>
                  {ROLES.map(role => (
                    <label key={role} style={s.checkLabel}>
                      <input type="checkbox" checked={form.exempted_roles.includes(role)} onChange={() => toggleRole(role)} />
                      <span>{role}</span>
                    </label>
                  ))}
                </div>
              </div>
              <div style={s.formField}>
                <label style={{ ...s.label, cursor: 'pointer', userSelect: 'none' }} onClick={() => setShowCondition(v => !v)}>
                  {showCondition ? '\u25BE' : '\u25B8'} Condition
                </label>
                {showCondition && (
                  <div style={{ display: 'flex', gap: 6 }}>
                    <input style={{ ...s.input, flex: 1 }} value={form.condition_column} onChange={e => updateForm('condition_column', e.target.value)} placeholder="column" />
                    <input style={{ ...s.input, flex: 1 }} value={form.condition_value} onChange={e => updateForm('condition_value', e.target.value)} placeholder="value" />
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Actions */}
          <div style={s.formRowActions}>
            <button style={s.btnCancel} onClick={onCancel}>Cancel</button>
            <button style={s.btnPrimary} onClick={handleSubmit} disabled={!canSave}>
              {saving ? 'Saving...' : isEdit ? 'Save' : 'Create'}
            </button>
          </div>
        </div>
      </td>
    </tr>
  );
}


/* ------------------------------------------------------------------ */
/* Main Panel                                                          */
/* ------------------------------------------------------------------ */

export default function MaskingPanel({ showConfirm }) {
  const { authRequired } = useAuth();
  const [policies, setPolicies] = useState([]);
  const [methods, setMethods] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  // Inline form state: null | 'new' | policy id (for edit)
  const [formMode, setFormMode] = useState(null);

  // Filters
  const [search, setSearch] = useState('');
  const [filterSchema, setFilterSchema] = useState('all');
  const [filterMethod, setFilterMethod] = useState('all');

  // Sort
  const sort = useSortable('schema_name');

  useEffect(() => {
    api.getMaskingMethods().then(setMethods).catch(() => {});
  }, []);

  const loadPolicies = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listMaskingPolicies();
      setPolicies(data || []);
    } catch (e) {
      setError(e.message || 'Failed to load policies');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadPolicies(); }, [loadPolicies]);

  const schemas = useMemo(() => {
    const set = new Set(policies.map(p => p.schema_name).filter(Boolean));
    return [...set].sort(schemaCompare);
  }, [policies]);

  const methodIds = useMemo(() => {
    const set = new Set(policies.map(p => p.method).filter(Boolean));
    return [...set].sort();
  }, [policies]);

  const filteredPolicies = useMemo(() => {
    let data = policies;
    if (search) {
      const q = search.toLowerCase();
      data = data.filter(p =>
        (p.schema_name || '').toLowerCase().includes(q) ||
        (p.table_name || '').toLowerCase().includes(q) ||
        (p.column_name || '').toLowerCase().includes(q) ||
        (p.method || '').toLowerCase().includes(q)
      );
    }
    if (filterSchema !== 'all') data = data.filter(p => p.schema_name === filterSchema);
    if (filterMethod !== 'all') data = data.filter(p => p.method === filterMethod);
    return sortData(data, sort.sortKey, sort.sortDir, (item, key) => {
      if (key === 'target') return `${item.schema_name}.${item.table_name}`;
      if (key === 'column_name') return item.column_name || '';
      if (key === 'method') return item.method || '';
      if (key === 'created_at') return item.created_at || '';
      return item[key];
    });
  }, [policies, search, filterSchema, filterMethod, sort.sortKey, sort.sortDir]);

  const handleOpenNew = useCallback(() => setFormMode('new'), []);

  const handleEdit = useCallback((policy) => setFormMode(policy.id), []);

  const handleCloseForm = useCallback(() => setFormMode(null), []);

  const handleSave = useCallback(async (payload) => {
    setSaving(true);
    setError(null);
    try {
      if (formMode === 'new') {
        await api.createMaskingPolicy(payload);
      } else {
        await api.updateMaskingPolicy(formMode, payload);
      }
      setFormMode(null);
      await loadPolicies();
    } catch (e) {
      setError(e.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  }, [formMode, loadPolicies]);

  const handleDelete = useCallback(async (id) => {
    if (showConfirm) {
      const ok = await showConfirm("Delete Masking Policy", "Are you sure you want to delete this masking policy? This action cannot be undone.", "Delete", true);
      if (!ok) return;
    }
    try {
      await api.deleteMaskingPolicy(id);
      await loadPolicies();
    } catch (e) {
      setError(e.message || 'Delete failed');
    }
  }, [loadPolicies, showConfirm]);

  // Build initial form data for the inline form
  const formInitial = useMemo(() => {
    if (formMode === 'new') return emptyPolicy(authRequired);
    if (formMode != null) {
      const p = policies.find(pol => pol.id === formMode);
      if (p) return {
        id: p.id,
        schema_name: p.schema_name || '',
        table_name: p.table_name || '',
        column_name: p.column_name || '',
        method: p.method || 'hash',
        method_config: p.method_config || {},
        condition_column: p.condition_column || '',
        condition_value: p.condition_value || '',
        exempted_roles: p.exempted_roles || ['admin'],
      };
    }
    return emptyPolicy(authRequired);
  }, [formMode, policies, authRequired]);

  const COL_COUNT = 7;

  return (
    <div style={s.container}>
      {/* Header */}
      <div style={s.header}>
        <div style={s.headerLeft}>
          <span style={s.headerTitle}>Masking Policies</span>
          {policies.length > 0 && (
            <span style={s.countBadge}>{policies.length}</span>
          )}
        </div>
        <button style={s.btnPrimary} onClick={handleOpenNew} disabled={formMode === 'new'}>+ Add Policy</button>
      </div>

      {/* Filter bar */}
      {!loading && policies.length > 0 && (
        <div style={s.toolbar}>
          <input
            style={s.filterInput}
            placeholder="Search schema, table, column, method..."
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          <select style={s.filterSelect} value={filterSchema} onChange={e => setFilterSchema(e.target.value)}>
            <option value="all">All Schemas</option>
            {schemas.map(sc => <option key={sc} value={sc}>{sc}</option>)}
          </select>
          <select style={s.filterSelect} value={filterMethod} onChange={e => setFilterMethod(e.target.value)}>
            <option value="all">All Methods</option>
            {methodIds.map(m => {
              const info = methods.find(mt => mt.id === m);
              return <option key={m} value={m}>{info?.name || m}</option>;
            })}
          </select>
          <span style={s.count}>{filteredPolicies.length} of {policies.length}</span>
        </div>
      )}

      {/* Content */}
      <div style={s.content}>
        {error && (
          <div style={s.error}>
            <span>{error}</span>
            <div style={{ display: 'flex', gap: 8, marginLeft: 'auto' }}>
              <button onClick={loadPolicies} style={s.retryBtn}>Retry</button>
              <button onClick={() => setError(null)} style={s.dismissErrorBtn}>&times;</button>
            </div>
          </div>
        )}

        {loading ? (
          <div style={s.emptyState}>
            <div style={s.emptyText}>Loading masking policies...</div>
          </div>
        ) : policies.length === 0 && formMode !== 'new' ? (
          <div style={s.emptyState}>
            <div style={s.emptyIcon}>--</div>
            <div style={s.emptyTitle}>No masking policies configured</div>
            <div style={s.emptyText}>Add a policy to protect sensitive data in query results.</div>
            <button style={{ ...s.btnPrimary, marginTop: 12 }} onClick={handleOpenNew}>+ Add Policy</button>
          </div>
        ) : (
          <table style={s.table}>
            <thead>
              <tr>
                <SortTh label="Schema.Table" sortKey="target" current={sort.sortKey} dir={sort.sortDir} onToggle={sort.toggle} style={s.th} />
                <SortTh label="Column" sortKey="column_name" current={sort.sortKey} dir={sort.sortDir} onToggle={sort.toggle} style={s.th} />
                <SortTh label="Method" sortKey="method" current={sort.sortKey} dir={sort.sortDir} onToggle={sort.toggle} style={s.th} />
                <th style={s.th}>Config</th>
                <th style={s.th}>Exempted Roles</th>
                <SortTh label="Created" sortKey="created_at" current={sort.sortKey} dir={sort.sortDir} onToggle={sort.toggle} style={s.th} />
                <th style={{ ...s.th, textAlign: 'right' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {/* Inline new form at top */}
              {formMode === 'new' && (
                <PolicyFormRow
                  initial={formInitial}
                  methods={methods}
                  onSave={handleSave}
                  onCancel={handleCloseForm}
                  saving={saving}
                  colSpan={COL_COUNT}
                />
              )}
              {filteredPolicies.length === 0 && formMode !== 'new' ? (
                <tr><td colSpan={COL_COUNT} style={{ ...s.td, color: 'var(--havn-text-dim)', textAlign: 'center' }}>No policies match filters</td></tr>
              ) : filteredPolicies.map(p => {
                // Inline edit form replaces this row
                if (formMode === p.id) {
                  return (
                    <PolicyFormRow
                      key={p.id}
                      initial={formInitial}
                      methods={methods}
                      onSave={handleSave}
                      onCancel={handleCloseForm}
                      saving={saving}
                      colSpan={COL_COUNT}
                    />
                  );
                }
                return (
                  <tr key={p.id} style={s.row}>
                    <td style={s.td}>
                      <span style={{ fontWeight: 500 }}>{p.schema_name}</span>
                      <span style={{ color: 'var(--havn-text-dim)' }}>.</span>
                      <span>{p.table_name}</span>
                    </td>
                    <td style={s.td}>
                      <code style={s.code}>{p.column_name}</code>
                    </td>
                    <td style={s.td}>
                      <MethodBadge method={p.method} methods={methods} />
                    </td>
                    <td style={s.td}>
                      <span style={{ fontSize: 12, color: 'var(--havn-text-secondary)' }}>
                        {formatConfig(p.method, p.method_config)}
                      </span>
                    </td>
                    <td style={s.td}>
                      {(p.exempted_roles || []).map(r => <RoleBadge key={r} role={r} />)}
                      {(!p.exempted_roles || p.exempted_roles.length === 0) && <span style={{ color: 'var(--havn-text-dim)', fontSize: 12 }}>{'\u2014'}</span>}
                    </td>
                    <td style={s.td}>
                      <span style={{ fontSize: 12, color: 'var(--havn-text-secondary)' }}>
                        {p.created_at ? p.created_at.slice(0, 16).replace('T', ' ') : '\u2014'}
                      </span>
                    </td>
                    <td style={{ ...s.td, textAlign: 'right' }}>
                      <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
                        <button style={s.actionBtn} onClick={() => handleEdit(p)} disabled={formMode != null} title="Edit">Edit</button>
                        <button style={s.actionBtnDanger} onClick={() => handleDelete(p.id)} disabled={formMode != null} title="Delete">Delete</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

const s = {
  // Layout
  container: { display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden', background: 'var(--havn-bg)' },
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 12px', borderBottom: '1px solid var(--havn-border)' },
  headerLeft: { display: 'flex', alignItems: 'center', gap: 8 },
  headerTitle: { fontSize: 13, fontWeight: 600, color: 'var(--havn-text)' },
  countBadge: { fontSize: 11, fontWeight: 600, padding: '1px 7px', borderRadius: 10, background: 'color-mix(in srgb, var(--havn-accent) 15%, transparent)', color: 'var(--havn-accent)' },
  content: { flex: 1, overflow: 'auto', padding: 20 },

  // Toolbar / filters
  toolbar: { display: 'flex', gap: 8, padding: '8px 20px 0', alignItems: 'center', flexWrap: 'wrap' },
  filterInput: { padding: '5px 10px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 6, fontSize: 12, width: 260 },
  filterSelect: { padding: '5px 8px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 6, fontSize: 12 },
  count: { fontSize: 12, color: 'var(--havn-text-dim)', marginLeft: 'auto' },

  // Table
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', padding: '8px 12px', borderBottom: '1px solid var(--havn-border-light)', color: 'var(--havn-text-secondary)', fontSize: 11, textTransform: 'uppercase' },
  td: { padding: '8px 12px', borderBottom: '1px solid var(--havn-border)', color: 'var(--havn-text)' },
  row: {},
  code: { background: 'color-mix(in srgb, var(--havn-accent) 8%, transparent)', padding: '1px 5px', borderRadius: 3, fontSize: 12, fontFamily: 'var(--havn-font-mono)', color: 'var(--havn-accent)' },

  // Buttons
  btnPrimary: { padding: '5px 14px', background: 'var(--havn-green)', color: '#fff', border: '1px solid var(--havn-green-border)', borderRadius: 'var(--havn-radius-lg)', cursor: 'pointer', fontSize: 11, fontWeight: 500, whiteSpace: 'nowrap' },
  btnCancel: { padding: '5px 14px', background: 'none', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius-lg)', color: 'var(--havn-text-secondary)', cursor: 'pointer', fontSize: 11, fontWeight: 500 },
  actionBtn: { padding: '3px 10px', background: 'var(--havn-btn-bg)', border: '1px solid var(--havn-btn-border)', borderRadius: 'var(--havn-radius)', cursor: 'pointer', fontSize: 11, fontWeight: 500, color: 'var(--havn-text)' },
  actionBtnDanger: { padding: '3px 10px', background: 'none', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius)', cursor: 'pointer', fontSize: 11, fontWeight: 500, color: 'var(--havn-red)' },

  // Error
  error: { display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: 'color-mix(in srgb, var(--havn-red) 12%, transparent)', color: 'var(--havn-red)', borderRadius: 'var(--havn-radius-lg)', marginBottom: 16, fontSize: 13 },
  retryBtn: { padding: '2px 10px', background: 'none', border: '1px solid var(--havn-red-border)', borderRadius: 'var(--havn-radius)', color: 'var(--havn-red)', cursor: 'pointer', fontSize: 11, fontWeight: 500 },
  dismissErrorBtn: { background: 'none', border: 'none', color: 'var(--havn-red)', cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: '0 4px' },

  // Empty state
  emptyState: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '60px 20px', color: 'var(--havn-text-secondary)' },
  emptyIcon: { fontSize: 32, marginBottom: 12, opacity: 0.5, filter: 'grayscale(1)' },
  emptyTitle: { fontSize: 15, fontWeight: 600, marginBottom: 6, color: 'var(--havn-text)' },
  emptyText: { fontSize: 13, color: 'var(--havn-text-secondary)' },

  // Inline form row
  formRow: { padding: '12px 16px', background: 'color-mix(in srgb, var(--havn-accent) 4%, var(--havn-bg))', borderTop: '1px solid var(--havn-accent)', borderBottom: '1px solid var(--havn-accent)' },
  formRowHeader: { marginBottom: 10 },
  formRowTitle: { fontSize: 12, fontWeight: 600, color: 'var(--havn-accent)', textTransform: 'uppercase', letterSpacing: '0.3px' },
  formRowBody: { display: 'flex', flexDirection: 'column', gap: 10 },
  formFieldRow: { display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-start' },
  formField: { flex: 1, minWidth: 120 },
  formRowActions: { display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 },

  // Form fields
  label: { display: 'block', fontSize: 11, color: 'var(--havn-text-secondary)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.3px' },
  input: { padding: '5px 8px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius)', fontSize: 12, width: '100%', boxSizing: 'border-box' },
  select: { padding: '5px 8px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius)', fontSize: 12, width: '100%', boxSizing: 'border-box' },
  checkLabel: { display: 'flex', alignItems: 'center', gap: 4, color: 'var(--havn-text)', fontSize: 12, cursor: 'pointer' },

  // Method info
  methodInfo: { padding: '6px 10px', background: 'var(--havn-bg-tertiary)', borderRadius: 'var(--havn-radius)', border: '1px solid var(--havn-border-light)', fontSize: 11 },
  methodDescription: { color: 'var(--havn-text-secondary)' },
  exampleCode: { fontSize: 11, fontFamily: 'var(--havn-font-mono)', padding: '1px 4px', background: 'var(--havn-bg)', borderRadius: 3, color: 'var(--havn-text)' },
  exampleArrow: { color: 'var(--havn-text-dim)', fontSize: 12 },
};

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { api } from './api';
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

function emptyPolicy() {
  return {
    schema_name: '', table_name: '', column_name: '', method: 'hash',
    method_config: {}, condition_column: '', condition_value: '', exempted_roles: ['admin'],
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

// --- Modal ---

function PolicyModal({ mode, initial, methods, onSave, onClose, saving }) {
  const [form, setForm] = useState(initial);
  const [showCondition, setShowCondition] = useState(!!(initial.condition_column));

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

  const handleSubmit = useCallback(() => {
    const payload = { ...form };
    if (!selectedMethod?.config?.length) delete payload.method_config;
    else {
      // Remove empty string config values
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

  const canSave = form.schema_name && form.table_name && form.column_name && !saving;

  return (
    <div style={s.overlay} onClick={onClose}>
      <div style={s.modal} onClick={e => e.stopPropagation()}>
        <div style={s.modalHeader}>
          <span style={s.modalTitle}>{mode === 'edit' ? 'Edit Masking Policy' : 'New Masking Policy'}</span>
          <button style={s.closeBtn} onClick={onClose}>&times;</button>
        </div>

        <div style={s.modalBody}>
          {/* Target fields */}
          <div style={s.fieldGroup}>
            <div style={s.fieldGroupLabel}>Target</div>
            <div style={s.formGrid3}>
              <div>
                <label style={s.label}>Schema</label>
                <input style={s.input} value={form.schema_name} onChange={e => updateForm('schema_name', e.target.value)} placeholder="e.g. gold" />
              </div>
              <div>
                <label style={s.label}>Table</label>
                <input style={s.input} value={form.table_name} onChange={e => updateForm('table_name', e.target.value)} placeholder="e.g. customers" />
              </div>
              <div>
                <label style={s.label}>Column</label>
                <input style={s.input} value={form.column_name} onChange={e => updateForm('column_name', e.target.value)} placeholder="e.g. email" />
              </div>
            </div>
          </div>

          {/* Method selector */}
          <div style={s.fieldGroup}>
            <div style={s.fieldGroupLabel}>Masking Method</div>
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
            {selectedMethod && (
              <div style={s.methodInfo}>
                <div style={s.methodDescription}>{selectedMethod.description}</div>
                {selectedMethod.example && (
                  <div style={s.methodExample}>
                    <code style={s.exampleCode}>{selectedMethod.example.input}</code>
                    <span style={s.exampleArrow}>&rarr;</span>
                    <code style={s.exampleCode}>{selectedMethod.example.output}</code>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Dynamic config fields */}
          {selectedMethod?.config?.length > 0 && (
            <div style={s.fieldGroup}>
              <div style={s.fieldGroupLabel}>Configuration</div>
              <div style={s.formGrid2}>
                {selectedMethod.config.map(field => (
                  <div key={field.key}>
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
              </div>
            </div>
          )}

          {/* Condition (collapsible) */}
          <div style={s.fieldGroup}>
            <div
              style={{ ...s.fieldGroupLabel, cursor: 'pointer', userSelect: 'none' }}
              onClick={() => setShowCondition(v => !v)}
            >
              {showCondition ? '\u25BE' : '\u25B8'} Condition (optional)
            </div>
            {showCondition && (
              <div style={s.formGrid2}>
                <div>
                  <label style={s.label}>Condition Column</label>
                  <input style={s.input} value={form.condition_column} onChange={e => updateForm('condition_column', e.target.value)} placeholder="e.g. region" />
                </div>
                <div>
                  <label style={s.label}>Condition Value</label>
                  <input style={s.input} value={form.condition_value} onChange={e => updateForm('condition_value', e.target.value)} placeholder="e.g. EU" />
                </div>
              </div>
            )}
          </div>

          {/* Exempted roles */}
          <div style={s.fieldGroup}>
            <div style={s.fieldGroupLabel}>Exempted Roles</div>
            <div style={{ display: 'flex', gap: 16 }}>
              {ROLES.map(role => (
                <label key={role} style={s.checkLabel}>
                  <input type="checkbox" checked={form.exempted_roles.includes(role)} onChange={() => toggleRole(role)} />
                  <span>{role}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        <div style={s.modalFooter}>
          <button style={s.btnCancel} onClick={onClose}>Cancel</button>
          <button style={s.btnPrimary} onClick={handleSubmit} disabled={!canSave}>
            {saving ? 'Saving...' : mode === 'edit' ? 'Update Policy' : 'Create Policy'}
          </button>
        </div>
      </div>
    </div>
  );
}


// --- Main Panel ---

export default function MaskingPanel() {
  const [policies, setPolicies] = useState([]);
  const [methods, setMethods] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(null);
  const [deleting, setDeleting] = useState(false);

  // Modal state
  const [modalMode, setModalMode] = useState(null); // null | 'new' | 'edit'
  const [editTarget, setEditTarget] = useState(null); // policy id for edit

  // Filters
  const [search, setSearch] = useState('');
  const [filterSchema, setFilterSchema] = useState('all');
  const [filterMethod, setFilterMethod] = useState('all');

  // Sort
  const sort = useSortable('schema_name');

  // Load methods on mount (cached)
  useEffect(() => {
    api.getMaskingMethods().then(setMethods).catch(() => {});
  }, []);

  // Load policies
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

  // Derived data
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

  // Handlers
  const handleOpenNew = useCallback(() => {
    setEditTarget(null);
    setModalMode('new');
  }, []);

  const handleEdit = useCallback((policy) => {
    setEditTarget(policy.id);
    setModalMode('edit');
  }, []);

  const handleCloseModal = useCallback(() => {
    setModalMode(null);
    setEditTarget(null);
  }, []);

  const handleSave = useCallback(async (payload) => {
    setSaving(true);
    setError(null);
    try {
      if (modalMode === 'new') {
        await api.createMaskingPolicy(payload);
      } else {
        await api.updateMaskingPolicy(editTarget, payload);
      }
      handleCloseModal();
      await loadPolicies();
    } catch (e) {
      setError(e.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  }, [modalMode, editTarget, handleCloseModal, loadPolicies]);

  const handleDelete = useCallback(async (id) => {
    setDeleting(true);
    try {
      await api.deleteMaskingPolicy(id);
      setDeleteConfirm(null);
      await loadPolicies();
    } catch (e) {
      setError(e.message || 'Delete failed');
    } finally {
      setDeleting(false);
    }
  }, [loadPolicies]);

  // Build initial form for modal
  const modalInitial = useMemo(() => {
    if (modalMode === 'edit' && editTarget != null) {
      const p = policies.find(pol => pol.id === editTarget);
      if (p) return {
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
    return emptyPolicy();
  }, [modalMode, editTarget, policies]);

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
        <button style={s.btnPrimary} onClick={handleOpenNew}>+ Add Policy</button>
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
            {schemas.map(s => <option key={s} value={s}>{s}</option>)}
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
        ) : policies.length === 0 ? (
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
              {filteredPolicies.length === 0 ? (
                <tr><td colSpan={7} style={{ ...s.td, color: 'var(--havn-text-dim)', textAlign: 'center' }}>No policies match filters</td></tr>
              ) : filteredPolicies.map(p => (
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
                      <button style={s.actionBtn} onClick={() => handleEdit(p)} title="Edit">Edit</button>
                      <button style={s.actionBtnDanger} onClick={() => setDeleteConfirm(p.id)} title="Delete">Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Policy modal */}
      {modalMode && (
        <PolicyModal
          mode={modalMode}
          initial={modalInitial}
          methods={methods}
          onSave={handleSave}
          onClose={handleCloseModal}
          saving={saving}
        />
      )}

      {/* Delete confirmation modal */}
      {deleteConfirm != null && (
        <div style={s.overlay} onClick={() => setDeleteConfirm(null)}>
          <div style={s.deleteDialog} onClick={e => e.stopPropagation()}>
            <div style={s.deleteTitle}>Delete Masking Policy</div>
            <div style={s.deleteBody}>
              Are you sure you want to delete this masking policy? This action cannot be undone.
            </div>
            <div style={s.deleteFooter}>
              <button style={s.btnCancel} onClick={() => setDeleteConfirm(null)}>Cancel</button>
              <button style={s.btnDanger} onClick={() => handleDelete(deleteConfirm)} disabled={deleting}>
                {deleting ? 'Deleting...' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
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
  btnCancel: { padding: '6px 14px', background: 'none', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius-lg)', color: 'var(--havn-text-secondary)', cursor: 'pointer', fontSize: 12, fontWeight: 500 },
  btnDanger: { padding: '6px 14px', background: 'var(--havn-red)', color: '#fff', border: '1px solid var(--havn-red-border)', borderRadius: 'var(--havn-radius-lg)', cursor: 'pointer', fontSize: 12, fontWeight: 500 },
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

  // Modal overlay & dialog
  overlay: { position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 },
  modal: { background: 'var(--havn-bg-secondary)', border: '1px solid var(--havn-border)', borderRadius: 8, width: 540, maxWidth: '92vw', maxHeight: '85vh', display: 'flex', flexDirection: 'column', overflow: 'hidden' },
  modalHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '14px 20px', borderBottom: '1px solid var(--havn-border)' },
  modalTitle: { fontSize: 14, fontWeight: 600, color: 'var(--havn-text)' },
  closeBtn: { background: 'none', border: 'none', color: 'var(--havn-text-secondary)', cursor: 'pointer', fontSize: 20, lineHeight: 1, padding: '0 4px' },
  modalBody: { flex: 1, overflow: 'auto', padding: '16px 20px' },
  modalFooter: { display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 20px', borderTop: '1px solid var(--havn-border)' },

  // Delete dialog
  deleteDialog: { background: 'var(--havn-bg-secondary)', border: '1px solid var(--havn-border)', borderRadius: 8, padding: 20, width: 420, maxWidth: '90vw' },
  deleteTitle: { fontSize: 14, fontWeight: 600, color: 'var(--havn-text)', marginBottom: 8 },
  deleteBody: { fontSize: 13, color: 'var(--havn-text-secondary)', marginBottom: 16, lineHeight: 1.5 },
  deleteFooter: { display: 'flex', justifyContent: 'flex-end', gap: 8 },

  // Form fields
  fieldGroup: { marginBottom: 16 },
  fieldGroupLabel: { fontSize: 11, fontWeight: 600, color: 'var(--havn-text-secondary)', textTransform: 'uppercase', marginBottom: 8 },
  formGrid3: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 },
  formGrid2: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 },
  label: { display: 'block', fontSize: 12, color: 'var(--havn-text-secondary)', marginBottom: 4 },
  input: { padding: '6px 10px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius-lg)', fontSize: 13, width: '100%', boxSizing: 'border-box' },
  select: { padding: '6px 10px', background: 'var(--havn-bg-tertiary)', color: 'var(--havn-text)', border: '1px solid var(--havn-border-light)', borderRadius: 'var(--havn-radius-lg)', fontSize: 13, width: '100%', boxSizing: 'border-box' },
  checkLabel: { display: 'flex', alignItems: 'center', gap: 6, color: 'var(--havn-text)', fontSize: 13, cursor: 'pointer' },

  // Method info
  methodInfo: { marginTop: 8, padding: '8px 10px', background: 'var(--havn-bg-tertiary)', borderRadius: 'var(--havn-radius-lg)', border: '1px solid var(--havn-border-light)' },
  methodDescription: { fontSize: 12, color: 'var(--havn-text-secondary)', lineHeight: 1.4 },
  methodExample: { marginTop: 6, display: 'flex', alignItems: 'center', gap: 8 },
  exampleCode: { fontSize: 12, fontFamily: 'var(--havn-font-mono)', padding: '1px 5px', background: 'var(--havn-bg)', borderRadius: 3, color: 'var(--havn-text)' },
  exampleArrow: { color: 'var(--havn-text-dim)', fontSize: 13 },
};

import React, { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";

const CATEGORY_COLORS = {
  transform: "#4f46e5",
  query: "#0891b2",
  streaming: "#059669",
  system: "#9333ea",
};

function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const mins = Math.floor(ms / 60000);
  const secs = Math.floor((ms % 60000) / 1000);
  return `${mins}m ${secs}s`;
}

function CapacityBar({ categories }) {
  const totalMemory = categories.reduce((a, c) => a + c.memory_gb, 0) || 1;
  return (
    <div className="resource-capacity-bar">
      <div className="resource-capacity-bar-inner">
        {categories.map((c) => {
          const pct = (c.memory_gb / totalMemory) * 100;
          return (
            <div
              key={c.name}
              className="resource-capacity-slice"
              style={{
                width: `${pct}%`,
                background: CATEGORY_COLORS[c.name] || "#6b7280",
              }}
              title={`${c.name}: ${c.memory_gb} GB`}
            />
          );
        })}
      </div>
      <div className="resource-capacity-label">
        Total budget: {totalMemory.toFixed(1)} GB
      </div>
    </div>
  );
}

function CategoryCard({ category, onUpdate }) {
  const { name, memory_gb, threads, max_concurrent, active, utilization } = category;
  const color = CATEGORY_COLORS[name] || "#6b7280";
  const pct = Math.round(utilization * 100);

  const [editMemory, setEditMemory] = useState(memory_gb);
  const [editThreads, setEditThreads] = useState(threads);
  const [editConc, setEditConc] = useState(max_concurrent);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!dirty) {
      setEditMemory(memory_gb);
      setEditThreads(threads);
      setEditConc(max_concurrent);
    }
  }, [memory_gb, threads, max_concurrent, dirty]);

  function save() {
    onUpdate({
      category: name,
      memory_gb: Number(editMemory),
      threads: Number(editThreads),
      max_concurrent: Number(editConc),
    });
    setDirty(false);
  }

  return (
    <div className="resource-category-card" style={{ borderColor: color }}>
      <div className="resource-category-head">
        <span
          className="resource-category-dot"
          style={{ background: color }}
          aria-hidden
        />
        <span className="resource-category-name">{name}</span>
        <span className="resource-category-active">
          {active} / {max_concurrent}
        </span>
      </div>
      <div className="resource-category-utilization">
        <div
          className="resource-category-utilization-bar"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <div className="resource-category-dials">
        <label>
          <span>Memory (GB)</span>
          <input
            type="number"
            min="0.25"
            step="0.25"
            value={editMemory}
            onChange={(e) => {
              setEditMemory(e.target.value);
              setDirty(true);
            }}
          />
        </label>
        <label>
          <span>Threads</span>
          <input
            type="number"
            min="1"
            step="1"
            value={editThreads}
            onChange={(e) => {
              setEditThreads(e.target.value);
              setDirty(true);
            }}
          />
        </label>
        <label>
          <span>Max concurrent</span>
          <input
            type="number"
            min="1"
            step="1"
            value={editConc}
            onChange={(e) => {
              setEditConc(e.target.value);
              setDirty(true);
            }}
          />
        </label>
      </div>
      {dirty && (
        <div className="resource-category-actions">
          <button onClick={save}>Save</button>
          <button
            onClick={() => {
              setEditMemory(memory_gb);
              setEditThreads(threads);
              setEditConc(max_concurrent);
              setDirty(false);
            }}
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function TaskTable({ tasks, onCancel, showCancel }) {
  if (!tasks || tasks.length === 0) {
    return <div className="resource-empty">No tasks.</div>;
  }
  return (
    <table className="resource-task-table">
      <thead>
        <tr>
          <th>Category</th>
          <th>Label</th>
          <th>Status</th>
          <th>Duration</th>
          <th>Rows</th>
          {showCancel && <th />}
        </tr>
      </thead>
      <tbody>
        {tasks.map((t) => (
          <tr key={t.task_id}>
            <td>
              <span
                className="resource-category-dot"
                style={{
                  background: CATEGORY_COLORS[t.category] || "#6b7280",
                }}
                aria-hidden
              />
              {t.category}
            </td>
            <td>{t.label}</td>
            <td>{t.status}</td>
            <td>{formatDuration(t.duration_ms)}</td>
            <td>{t.rows_processed || 0}</td>
            {showCancel && (
              <td>
                {t.status === "running" && (
                  <button
                    className="resource-cancel"
                    onClick={() => onCancel(t.task_id)}
                  >
                    Cancel
                  </button>
                )}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ResourcePanel() {
  const [snap, setSnap] = useState(null);
  const [error, setError] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getResources()
      .then((s) => {
        if (!cancelled) setSnap(s);
      })
      .catch((e) => setError(String(e)));

    abortRef.current = api.streamResources((s) => {
      if (!cancelled) setSnap(s);
    });

    return () => {
      cancelled = true;
      if (abortRef.current) abortRef.current();
    };
  }, []);

  const categories = snap?.categories || [];
  const active = snap?.active || [];
  const recent = snap?.recent || [];

  async function handleUpdate(body) {
    try {
      const res = await api.updateResourceAllocation(body);
      setSnap(res.snapshot);
    } catch (e) {
      setError(String(e));
    }
  }

  async function handleCancel(taskId) {
    try {
      await api.cancelResourceTask(taskId);
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="resource-panel">
      <div className="resource-panel-header">
        <h2>Resources</h2>
        <div className="resource-panel-summary">
          {snap
            ? `${snap.total_active} active · ${snap.total_memory_gb.toFixed(1)} GB budget`
            : "Loading…"}
        </div>
      </div>

      {error && <div className="resource-error">{error}</div>}

      <CapacityBar categories={categories} />

      <div className="resource-category-grid">
        {categories.map((c) => (
          <CategoryCard key={c.name} category={c} onUpdate={handleUpdate} />
        ))}
      </div>

      <section className="resource-section">
        <h3>Active tasks</h3>
        <TaskTable tasks={active} onCancel={handleCancel} showCancel />
      </section>

      <section className="resource-section">
        <h3>Recent tasks</h3>
        <TaskTable tasks={recent} showCancel={false} />
      </section>
    </div>
  );
}

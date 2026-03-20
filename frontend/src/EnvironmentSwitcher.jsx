import React, { useState, useEffect, useRef } from "react";
import { api } from "./api";

/**
 * Environment indicator and switcher.
 * Shows the current environment and lets users switch between configured environments.
 */
export default function EnvironmentSwitcher({ showConfirm }) {
  const [env, setEnv] = useState(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    api.getEnvironment().then(setEnv).catch(() => {}).finally(() => setLoading(false));
  }, []);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  if (loading || !env || env.available.length === 0) return null;

  const handleSwitch = async (envName) => {
    if (envName === env.active) { setOpen(false); return; }
    setOpen(false);
    let confirmed;
    if (showConfirm) {
      confirmed = await showConfirm("Switch Environment", `Switch to environment "${envName}"? This will reload the page and any unsaved changes will be lost.`, "Switch", true);
    } else {
      confirmed = window.confirm(`Switch to environment "${envName}"? This will reload the page and any unsaved changes will be lost.`);
    }
    if (!confirmed) return;
    setSwitching(true);
    try {
      const result = await api.switchEnvironment(envName);
      setEnv({ ...env, active: result.active, database_path: result.database_path });
      window.location.reload();
    } catch (e) {
      console.error("Failed to switch environment:", e);
    } finally {
      setSwitching(false);
    }
  };

  // Single environment — just show a label, no dropdown
  if (env.available.length === 1) {
    return (
      <div style={st.badge}>
        <span style={st.dot} />
        {env.active}
      </div>
    );
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen(!open)}
        disabled={switching}
        style={st.trigger}
        aria-label="Switch environment"
        aria-expanded={open}
      >
        <span style={st.dot} />
        <span>{env.active}</span>
        <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ marginLeft: 2 }}>
          <path d="M1.5 3L4 5.5L6.5 3" />
        </svg>
      </button>
      {open && (
        <div style={st.dropdown}>
          {env.available.map((e) => (
            <button
              key={e}
              onClick={() => handleSwitch(e)}
              style={e === env.active ? st.itemActive : st.item}
            >
              {e === env.active && <span style={st.check}>&#10003;</span>}
              <span>{e}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const st = {
  badge: {
    display: "inline-flex", alignItems: "center", gap: "5px",
    padding: "3px 8px",
    fontSize: "11px", fontWeight: 500,
    color: "var(--havn-text-secondary)",
    background: "var(--havn-bg-tertiary)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius-lg)",
  },
  dot: {
    width: 6, height: 6, borderRadius: "50%",
    background: "var(--havn-green)",
    flexShrink: 0,
  },
  trigger: {
    display: "inline-flex", alignItems: "center", gap: "5px",
    padding: "3px 8px",
    fontSize: "11px", fontWeight: 500,
    color: "var(--havn-text-secondary)",
    background: "var(--havn-btn-bg)",
    border: "1px solid var(--havn-btn-border)",
    borderRadius: "var(--havn-radius-lg)",
    cursor: "pointer",
  },
  dropdown: {
    position: "absolute", top: "100%", right: 0, marginTop: 4,
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius)",
    boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
    zIndex: 200, minWidth: 140, overflow: "hidden",
  },
  item: {
    display: "flex", alignItems: "center", gap: "6px",
    width: "100%", padding: "6px 12px",
    background: "none", border: "none",
    color: "var(--havn-text)", fontSize: "12px",
    cursor: "pointer", textAlign: "left",
  },
  itemActive: {
    display: "flex", alignItems: "center", gap: "6px",
    width: "100%", padding: "6px 12px",
    background: "var(--havn-btn-bg)", border: "none",
    color: "var(--havn-accent)", fontSize: "12px", fontWeight: 600,
    cursor: "pointer", textAlign: "left",
  },
  check: { fontSize: "10px", color: "var(--havn-accent)" },
};

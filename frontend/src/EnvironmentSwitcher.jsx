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
    const confirmed = await showConfirm("Switch Environment", `Switch to environment "${envName}"? This will reload the page and any unsaved changes will be lost.`, "Switch", true);
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
      <div style={st.row}>
        <div style={st.badge}>
          <span style={st.dot} />
          {env.active}
        </div>
        <DeferBadge defer={env.defer} />
      </div>
    );
  }

  return (
    <div style={st.row}>
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
      <DeferBadge defer={env.defer} />
    </div>
  );
}

/**
 * The active environment's defer target, if it has one.
 *
 * The dot is green when the target can be attached read-only right now and
 * amber when it cannot, which is what decides whether the next `--defer` run
 * starts at all. `lockable` is probed by opening the file, so it describes
 * this instant; the tooltip points at `--defer-snapshot` for the amber case.
 */
export function DeferBadge({ defer }) {
  if (!defer || !defer.target) return null;
  const readable = !!defer.lockable;
  const where = defer.path ? ` (${defer.path})` : "";
  const title = readable
    ? `Defer target: ${defer.target}${where}. Unbuilt upstreams are read from it. `
      + "Readable now, so `havn transform --defer` will attach it read-only."
    : `Defer target: ${defer.target}${where}. Not readable right now`
      + `${defer.reason ? `: ${defer.reason}` : ""}. DuckDB refuses a read-only attach `
      + "while another process holds the file open for writing. Use "
      + "`havn transform --defer-snapshot` to read through a copy instead.";
  return (
    <div style={st.badge} title={title} data-testid="defer-badge">
      <span
        style={{ ...st.dot, background: readable ? "var(--havn-green)" : "var(--havn-yellow)" }}
        data-testid="defer-dot"
        data-state={readable ? "readable" : "locked"}
      />
      defer: {defer.target}
    </div>
  );
}

const st = {
  row: { display: "inline-flex", alignItems: "center", gap: "6px" },
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

import React, { useState, useEffect, useRef, useCallback } from "react";
import { api } from "./api";

/** How often the top bar asks which git branch is checked out. */
export const BRANCH_POLL_MS = 5000;

/** The event GitPanel fires after a checkout, so the bar follows at once. */
export const GIT_CHECKOUT_EVENT = "havn:git-checkout";

/**
 * Environment indicator and switcher.
 * Shows the current environment and lets users switch between configured environments.
 *
 * With branch warehouses on (`branches.enabled` in project.yml) it also shows
 * the checked-out git branch, because the branch decides which warehouse the
 * UI is showing. The server follows a checkout by itself; this component
 * polls `/api/branch` so the bar, and through `onBranchChange` the tables and
 * files, follow it too.
 */
export default function EnvironmentSwitcher({ showConfirm, onBranchChange }) {
  const [env, setEnv] = useState(null);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const headRef = useRef(null);

  useEffect(() => {
    api.getEnvironment()
      .then((e) => { headRef.current = e?.branch?.server?.head ?? null; setEnv(e); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const branchesOn = !!env?.branch?.enabled;

  const checkBranch = useCallback(async () => {
    let b;
    try {
      b = await api.getBranch();
    } catch {
      return;
    }
    const head = b?.server?.head ?? null;
    const moved = headRef.current !== null && head !== headRef.current;
    headRef.current = head;
    if (!moved) {
      setEnv((cur) => (cur ? { ...cur, branch: b } : cur));
      return;
    }
    // The server switched warehouses: re-read the environment (database
    // path, defer target) and let the app reload tables and files.
    try {
      const fresh = await api.getEnvironment();
      setEnv(fresh);
    } catch {
      setEnv((cur) => (cur ? { ...cur, branch: b } : cur));
    }
    onBranchChange?.(b);
  }, [onBranchChange]);

  useEffect(() => {
    if (!branchesOn) return undefined;
    const t = setInterval(checkBranch, BRANCH_POLL_MS);
    window.addEventListener("focus", checkBranch);
    window.addEventListener(GIT_CHECKOUT_EVENT, checkBranch);
    return () => {
      clearInterval(t);
      window.removeEventListener("focus", checkBranch);
      window.removeEventListener(GIT_CHECKOUT_EVENT, checkBranch);
    };
  }, [branchesOn, checkBranch]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  if (loading || !env) return null;
  const branch = env.branch?.enabled ? env.branch : null;
  if (env.available.length === 0 && !branch) return null;

  const handleSwitch = async (envName) => {
    if (envName === env.active) { setOpen(false); return; }
    setOpen(false);
    const note = branch?.active
      ? " An explicit environment replaces the branch warehouse until you switch back."
      : "";
    const confirmed = await showConfirm("Switch Environment", `Switch to environment "${envName}"? This will reload the page and any unsaved changes will be lost.${note}`, "Switch", true);
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

  // On a branch warehouse there is no active environment: the branch is
  // what the bar names, and the defer badge names its base.
  if (branch && (branch.active || env.available.length === 0)) {
    return (
      <div style={st.row}>
        <BranchBadge branch={branch} databasePath={env.database_path} />
        {branch.active && <DeferBadge defer={env.defer} />}
        {env.available.length > 0 && (
          <div ref={ref} style={{ position: "relative" }}>
            <button
              onClick={() => setOpen(!open)}
              disabled={switching}
              style={st.trigger}
              aria-label="Use an environment instead of the branch warehouse"
              aria-expanded={open}
              title="Use an environment instead of the branch warehouse"
            >
              env
              <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ marginLeft: 2 }}>
                <path d="M1.5 3L4 5.5L6.5 3" />
              </svg>
            </button>
            {open && (
              <div style={st.dropdown}>
                {env.available.map((e) => (
                  <button key={e} onClick={() => handleSwitch(e)} style={st.item}>
                    <span>{e}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  const prod = isProductionEnv(env.active);
  const pill = prod ? st.prodPill : null;
  const dot = { ...st.dot, background: prod ? "var(--havn-red)" : "var(--havn-green)" };
  const envTitle = prod
    ? `Environment: ${env.active}. Runs and builds write to production.`
    : `Environment: ${env.active}`;

  // Single environment — just show a label, no dropdown
  if (env.available.length === 1) {
    return (
      <div style={st.row}>
        {branch && <BranchBadge branch={branch} databasePath={env.database_path} />}
        <div style={{ ...st.badge, ...pill }} title={envTitle} data-env-kind={prod ? "prod" : "other"}>
          <span style={dot} />
          {env.active}
        </div>
        <DeferBadge defer={env.defer} />
      </div>
    );
  }

  return (
    <div style={st.row}>
      {branch && <BranchBadge branch={branch} databasePath={env.database_path} />}
      <div ref={ref} style={{ position: "relative" }}>
        <button
          onClick={() => setOpen(!open)}
          disabled={switching}
          style={{ ...st.trigger, ...pill }}
          aria-label={`Environment: ${env.active}. Switch environment`}
          aria-expanded={open}
          title={envTitle}
          data-env-kind={prod ? "prod" : "other"}
        >
          <span style={dot} />
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
 * The checked-out git branch and whether it has its own warehouse.
 *
 * Accent when the UI shows a branch warehouse, plain on a main branch (or
 * when the branch warehouse is off for another reason, which the tooltip
 * gives). Amber while the server waits to switch: it does not move the
 * warehouse while a build or another request is using it.
 */
export function BranchBadge({ branch, databasePath }) {
  if (!branch) return null;
  const name = branch.branch || (branch.detached ? "detached" : "no branch");
  const pending = branch.server?.pending;
  const base = branch.base?.label || "base";
  let title;
  if (pending) {
    title = `Checked out ${pending.branch || "another branch"}; its warehouse opens when nothing is using the current one (${pending.reason}).`;
  } else if (branch.active) {
    title = `Branch warehouse ${databasePath || branch.warehouse?.path || ""}. Models this branch has not built are read from ${base} (${branch.base?.path || ""}), read-only.`;
  } else {
    title = `Git branch ${name}: ${branch.reason}.`;
  }
  const kind = pending ? "pending" : branch.active ? "branch" : "main";
  return (
    <div
      style={{ ...st.badge, ...(branch.active && !pending ? st.branchPill : null), ...(pending ? st.pendingPill : null) }}
      title={title}
      data-testid="branch-badge"
      data-kind={kind}
    >
      <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        <circle cx="4" cy="3.5" r="1.8" /><circle cx="4" cy="12.5" r="1.8" /><circle cx="12" cy="5.5" r="1.8" />
        <path d="M4 5.3v5.4M12 7.3c0 2.5-2.5 3-6.4 4" />
      </svg>
      <span style={st.branchName}>{name}</span>
      {pending && <span style={st.pendingText}>switching…</span>}
    </div>
  );
}

/** Whether an environment name reads as production (prod, production, prd, live). */
export function isProductionEnv(name) {
  return /^(prod|production|prd|live)([-_].*)?$/i.test(name || "");
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
    borderRadius: 20,
  },
  // Production is loud: red text and border, so a run there is never a surprise.
  prodPill: {
    color: "var(--havn-red)",
    borderColor: "var(--havn-red)",
    fontWeight: 600,
  },
  // A branch warehouse is the accent colour: the data on screen is the
  // branch's, not the base's.
  branchPill: {
    color: "var(--havn-accent)",
    borderColor: "var(--havn-accent)",
    fontWeight: 600,
  },
  pendingPill: {
    color: "var(--havn-yellow)",
    borderColor: "var(--havn-yellow)",
  },
  branchName: {
    maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
    fontFamily: "var(--havn-font-mono)",
  },
  pendingText: { fontWeight: 400, opacity: 0.85 },
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
    borderRadius: 20,
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

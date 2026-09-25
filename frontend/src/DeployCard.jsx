import React, { useState, useEffect, useCallback, useRef } from "react";
import { api } from "./api";

/*
 * Deploy a git ref to an environment: pick the environment, see which models
 * would rebuild there, deploy, and watch it land or roll back. A failed deploy
 * leaves the environment exactly as it was (see havn.engine.deploy).
 */

const POLL_MS = 1500;
const PLAN_PREVIEW = 8;

function pickDefault(envs) {
  return (envs.find((e) => e.production) || envs.find((e) => e.active) || envs[0])?.name || null;
}

export default function DeployCard({ refName, prId, showConfirm, onDeployed }) {
  const [targets, setTargets] = useState(null);
  const [env, setEnv] = useState(null);
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState(null);
  const [deploy, setDeploy] = useState(null);
  const [error, setError] = useState(null);
  const [history, setHistory] = useState([]);
  const pollRef = useRef(null);

  useEffect(() => {
    api.getDeployTargets()
      .then((t) => { setTargets(t); setEnv((cur) => cur || pickDefault(t.environments)); })
      .catch((e) => setError(e.message));
  }, []);

  const ref = refName || targets?.default_ref;

  const loadPlan = useCallback(async () => {
    if (!env || !ref) return;
    setPlan(null);
    setPlanError(null);
    try {
      setPlan(await api.getDeployPlan(env, ref));
    } catch (e) {
      setPlanError(e.message);
    }
  }, [env, ref]);

  useEffect(() => { loadPlan(); }, [loadPlan]);

  const loadHistory = useCallback(() => {
    api.listDeploys(prId).then((h) => setHistory(h || [])).catch(() => {});
  }, [prId]);
  useEffect(() => { loadHistory(); }, [loadHistory]);

  // Poll the running deploy until it settles.
  useEffect(() => {
    if (deploy?.status !== "running") return;
    pollRef.current = setInterval(async () => {
      try {
        const d = await api.getDeploy(deploy.id);
        if (d.status !== "running") {
          setDeploy(d);
          loadPlan();
          loadHistory();
          onDeployed?.(d);
        }
      } catch (e) {
        setError(e.message);
      }
    }, POLL_MS);
    return () => clearInterval(pollRef.current);
  }, [deploy, loadPlan, loadHistory, onDeployed]);

  const target = targets?.environments.find((e) => e.name === env);

  async function start() {
    if (!target || !plan) return;
    const n = plan.models.length;
    const ok = await showConfirm(
      `Deploy to ${env}`,
      `Build ${n} model${n === 1 ? "" : "s"} from ${ref} (${(plan.commit || "").slice(0, 7)}) in ${env}. `
        + "Those models are snapshotted first; if any of them fails, all of them are put back as they were.",
      `Deploy to ${env}`,
      !!target.production,
    );
    if (!ok) return;
    setError(null);
    try {
      setDeploy(await api.startDeploy({ env, ref, pr_id: prId || undefined }));
    } catch (e) {
      setError(e.message);
    }
  }

  if (error && !targets) return <div style={s.card}><div style={s.bad}>{error}</div></div>;
  if (!targets) return null;
  const running = deploy?.status === "running";
  const n = plan?.models?.length ?? 0;

  return (
    <div style={s.card} aria-label="Deploy">
      <div style={s.head}>
        <b style={{ fontWeight: 500 }}>Deploy</b>
        <span style={s.dim}><span style={s.ref}>{ref}</span> to</span>
        <select
          value={env || ""}
          onChange={(e) => { setEnv(e.target.value); setDeploy(null); }}
          style={{ ...s.select, ...(target?.production ? s.prodSelect : null) }}
          aria-label="Environment"
          disabled={running}
        >
          {targets.environments.map((e) => (
            <option key={e.name} value={e.name}>{e.name}{e.active ? " (this server)" : ""}</option>
          ))}
        </select>
      </div>

      {deploy && !running && <div style={{ marginBottom: 10 }}><DeployResult d={deploy} /></div>}
      {planError && <div style={s.bad}>{planError}</div>}
      {!planError && !plan && <div style={s.dim}>Working out what would change…</div>}
      {plan && !running && (
        n === 0
          ? <div style={s.dim}><span style={s.ok}>{"✓"}</span> {env} is up to date with {ref}.</div>
          : (
            <div style={s.dim}>
              {n} model{n === 1 ? "" : "s"} will rebuild{target && !target.exists ? " (new warehouse)" : ""}:
              <div style={s.models}>
                {plan.models.slice(0, PLAN_PREVIEW).map((m) => <span key={m} style={s.model}>{m}</span>)}
                {n > PLAN_PREVIEW && <span style={s.dim}>+ {n - PLAN_PREVIEW} more</span>}
              </div>
            </div>
          )
      )}

      {running && <div style={s.dim}>Deploying {deploy.models ? `${deploy.models.length} models` : ""} to {env}…</div>}
      {error && targets && <div style={s.bad}>{error}</div>}

      <button
        style={target?.production ? s.btnProd : s.btnPrimary}
        onClick={start}
        disabled={running || !plan || n === 0}
      >
        {running ? "Deploying…" : `Deploy to ${env}`}
      </button>

      {history.length > 0 && (
        <div style={s.history}>
          {history.slice(0, 4).map((h) => (
            <div key={h.id} style={s.histRow} title={h.error || ""}>
              <span style={STATUS_STYLE[h.status] || s.dim}>{STATUS_LABEL[h.status] || h.status}</span>
              <span style={s.dim}>{h.env} · {(h.commit || "").slice(0, 7)} · {h.deployed_by || ""}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const STATUS_LABEL = {
  success: "✓ deployed", rolled_back: "↺ rolled back", error: "✗ failed",
  up_to_date: "= up to date", running: "… running",
};

function DeployResult({ d }) {
  if (d.status === "success") {
    return <div style={s.ok} role="status">{"✓"} Deployed {(d.commit || "").slice(0, 7)} to {d.env}: {d.models.length} model{d.models.length === 1 ? "" : "s"} built.</div>;
  }
  if (d.status === "up_to_date") return <div style={s.ok} role="status">{"✓"} {d.env} was already up to date.</div>;
  if (d.status === "rolled_back") {
    return (
      <div role="alert">
        <div style={s.bad}>{"↺"} Rolled back. {d.env} is exactly as it was before the deploy.</div>
        {Object.entries(d.failed || {}).map(([m, f]) => (
          <div key={m} style={s.failRow}>
            <span style={s.model}>{m}</span> <span style={s.dim}>{f.status.replace(/_/g, " ")}{f.error ? `: ${f.error}` : ""}</span>
          </div>
        ))}
        {d.error && <div style={s.dim}>{d.error}</div>}
      </div>
    );
  }
  return <div style={s.bad} role="alert">{"✗"} Deploy failed: {d.error}</div>;
}

const btn = {
  width: "100%", marginTop: 12, padding: "8px 12px", borderRadius: "var(--havn-radius)", fontSize: 13.5,
  fontFamily: "inherit", cursor: "pointer", fontWeight: 500, border: "1px solid",
};

const s = {
  card: {
    marginTop: 16, padding: "12px 14px", fontSize: 13, background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)",
  },
  head: { display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" },
  dim: { color: "var(--havn-text-secondary)", fontSize: 12.5, lineHeight: 1.5 },
  ok: { color: "var(--havn-green)", fontSize: 12.5 },
  bad: { color: "var(--havn-red)", fontSize: 12.5, lineHeight: 1.5 },
  ref: { fontFamily: "var(--havn-font-mono)", fontSize: 12, color: "var(--havn-text)" },
  select: {
    fontFamily: "inherit", fontSize: 12.5, padding: "3px 8px", borderRadius: 20, cursor: "pointer",
    background: "var(--havn-btn-bg)", color: "var(--havn-text)", border: "1px solid var(--havn-btn-border)",
  },
  prodSelect: { color: "var(--havn-red)", borderColor: "var(--havn-red)", fontWeight: 600 },
  models: { display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 },
  model: { fontFamily: "var(--havn-font-mono)", fontSize: 11.5, padding: "1px 7px", borderRadius: 4, background: "var(--havn-bg)", color: "var(--havn-text)" },
  failRow: { marginTop: 4, overflowWrap: "anywhere" },
  btnPrimary: { ...btn, background: "var(--havn-accent)", borderColor: "var(--havn-accent)", color: "#fff" },
  btnProd: { ...btn, background: "var(--havn-red)", borderColor: "var(--havn-red)", color: "#fff" },
  history: { marginTop: 12, borderTop: "1px solid var(--havn-border)", paddingTop: 8 },
  histRow: { display: "flex", justifyContent: "space-between", gap: 8, fontSize: 12, padding: "2px 0" },
};

const STATUS_STYLE = { success: s.ok, up_to_date: s.ok, rolled_back: s.bad, error: s.bad, running: s.dim };

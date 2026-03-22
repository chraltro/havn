import React, { useState, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

/* ------------------------------------------------------------------ */
/* Onboarding Steps                                                    */
/* ------------------------------------------------------------------ */

// position: where to place the card
//   "bottom-center"    — bottom center (default for most steps)
//   "below-overview"   — below the "Overview" nav item, left-aligned
//   "below-actions"    — right below the actions area (top-right)
//   "beside-sidebar"   — right of sidebar, vertically centered
//   "center"           — centered on screen

const ONBOARDING_STEPS = [
  {
    id: "welcome",
    title: "Welcome to havn",
    description: "Your entire data warehouse lives in one file on your machine. No cloud, no accounts, no data leaving your network.\nThis tour takes about two minutes. Let's look around.",
    illustration: "welcome",
    navigate: "Overview",
    highlight: null,
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "navigation",
    title: "Finding Your Way",
    description: "{Overview|Dashboard with stats, pipeline health, and quick actions.} is your home base. {Develop|Write and edit SQL transforms, ingest scripts, and manage git.} is where you build. {Explore|Run queries, browse tables, and visualize your DAG.} is for discovery. {Observe|Monitor data quality, schema changes, and pipeline history.} keeps things healthy. {Configure|Set up connectors, masking policies, themes, and scheduling.} ties it all together.",
    illustration: "welcome",
    navigate: "Overview",
    highlight: '[data-havn-guide="tabs"]',
    position: "below-overview",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "warehouse",
    title: "Your Warehouse",
    description: "All your data lives in one file, organized into layers that refine it step by step: landing, bronze, silver, gold. Powered by {DuckDB|A fast, embedded analytics database. Runs in-process, no server needed.} -- everything stays on your machine.",
    illustration: "warehouse",
    navigate: "Overview",
    highlight: '[data-havn-guide="tables-pane"]',
    position: "beside-sidebar",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "project",
    title: "Your Project",
    description: "{Ingest scripts|Python files in ingest/ that pull raw data from external sources into landing/} bring data in, {SQL transforms|.sql files in transform/ that define how data moves between schema layers} shape it layer by layer, and {export scripts|Python files in export/ that send finished data to external systems} send it out. The file tree on the left is your pipeline.",
    illustration: "project",
    navigate: "Overview",
    highlight: '[data-havn-guide="files-pane"]',
    position: "beside-sidebar",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "transforms",
    title: "Writing Transforms",
    description: "Every .sql file in transform/ becomes a model. Add {-- config:|Sets materialization (table or view), target schema, and incremental strategy} to set options. havn reads your SQL and automatically figures out which models depend on each other. No special syntax, no templates. Just SQL.",
    illustration: "transforms",
    navigate: "Editor",
    highlight: ['[data-havn-guide="main-panel"]', '[data-havn-guide="sub-tab-bar"]', '[data-havn-guide="files-pane"]'],
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: ["transform/silver", "transform/bronze", "transform"],
    prefillQuery: null,
  },
  {
    id: "connectors",
    title: "Connect Your Sources",
    description: "When you're ready for your own data, pick a {connector|Pre-built integrations for databases, APIs, files, and SaaS tools like Stripe and HubSpot.}: Postgres, MySQL, REST APIs, CSV, S3, and more. havn generates the ingest script for you. Just add credentials to your {.env file|A local file for secrets like passwords and API keys. Never committed to git.}.",
    illustration: "connectors",
    navigate: "Data Sources",
    highlight: ['[data-havn-guide="main-panel"]', '[data-havn-guide="sub-tab-bar"]'],
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "explore",
    title: "Exploring Data",
    description: "Write SQL, get results. The query editor has {autocomplete|Suggests table names, columns, and SQL keywords as you type.} for every table, column, and function in your warehouse. Browse tables, build charts, or inspect {query plans|Shows how DuckDB will execute your SQL, useful for spotting bottlenecks.}.\nTry editing the query, or write your own.",
    illustration: "explore",
    navigate: "Query",
    highlight: ['[data-havn-guide="main-panel"]', '[data-havn-guide="sub-tab-bar"]', '[data-havn-guide="tables-pane"]'],
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: "__auto__",
  },
  {
    id: "dag",
    title: "The DAG",
    description: "Every model and its dependencies, visualized. Nodes are colored by layer, the same layers you saw in your warehouse. When you change a SQL file, havn knows exactly which {downstream models|Models that depend on the one you changed. They need rebuilding too.} need rebuilding.\nTry clicking a node to see its {column lineage|Traces each column back to its source, showing exactly where every field comes from across your pipeline.}.",
    illustration: "dag",
    navigate: "DAG",
    highlight: ['[data-havn-guide="main-panel"]', '[data-havn-guide="sub-tab-bar"]'],
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "quality",
    title: "Data Quality",
    description: "Add {assertions|Checks written as SQL comments, like: -- assert: row_count > 0. Validated on every pipeline run.} to your SQL files and havn validates them automatically. Set up {contracts|YAML files that define expected row counts, freshness thresholds, and column rules.} for stricter guarantees across models. When something breaks, you'll know before your stakeholders do.",
    illustration: "quality",
    navigate: "Quality",
    highlight: ['[data-havn-guide="main-panel"]', '[data-havn-guide="sub-tab-bar"]'],
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "pipelines",
    title: "Running Pipelines",
    description: "Hit Run to execute your full pipeline: {ingest|Python scripts that pull raw data from sources into landing/}, then {transforms|SQL models that process data through bronze, silver, gold}, then {exports|Python scripts that push finished data to dashboards, APIs, or files}. Output streams live so you see every step as it happens.\nGo ahead, try it now, or keep exploring.",
    illustration: "pipelines",
    navigate: "Overview",
    highlight: '[data-havn-guide="actions"]',
    position: "below-actions",
    autoSelectTable: false,
    autoOpenFile: null,
    prefillQuery: null,
  },
  {
    id: "ready",
    title: "You're Ready",
    description: "Edit a SQL file. Run the pipeline. Query the results. That's the core loop.\nPress {Ctrl+K|Opens the command palette: search for files, tables, and commands from anywhere.} to find anything fast. Replay this tour anytime from Settings.\nData in safe waters.",
    illustration: "control",
    navigate: "Editor",
    highlight: null,
    position: "center",
    autoSelectTable: false,
    autoOpenFile: ["transform/silver", "transform/bronze", "transform"],
    prefillQuery: null,
  },
];

/* ------------------------------------------------------------------ */
/* File tree search helper                                             */
/* ------------------------------------------------------------------ */

function findFirstFile(entries, pathPrefixes) {
  const prefixes = Array.isArray(pathPrefixes) ? pathPrefixes : [pathPrefixes];
  for (const prefix of prefixes) {
    const found = searchEntries(entries, prefix);
    if (found) return found;
  }
  return null;
}

function searchEntries(entries, pathPrefix) {
  for (const entry of entries) {
    if (entry.type === "file" && entry.path.startsWith(pathPrefix) && entry.path.endsWith(".sql")) {
      return entry.path;
    }
    if (entry.children) {
      const found = searchEntries(entry.children, pathPrefix);
      if (found) return found;
    }
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Glossary term parser                                                */
/* ------------------------------------------------------------------ */

function parseDescription(text) {
  // Split on newlines first to handle multi-paragraph copy
  const lines = text.split("\n");
  const result = [];
  let key = 0;

  lines.forEach((line, lineIdx) => {
    if (lineIdx > 0) {
      result.push(<div key={`br-${lineIdx}`} style={{ height: "8px" }} />);
    }
    const parts = parseGlossaryLine(line, key);
    result.push(...parts.elements);
    key = parts.nextKey;
  });

  return result;
}

function parseGlossaryLine(text, startKey) {
  const elements = [];
  let remaining = text;
  let key = startKey;

  while (remaining.length > 0) {
    const start = remaining.indexOf("{");
    if (start === -1) {
      elements.push(remaining);
      break;
    }

    const pipeIdx = remaining.indexOf("|", start);
    if (pipeIdx === -1) {
      elements.push(remaining);
      break;
    }
    const end = remaining.indexOf("}", pipeIdx);
    if (end === -1) {
      elements.push(remaining);
      break;
    }

    if (start > 0) {
      elements.push(remaining.slice(0, start));
    }

    const term = remaining.slice(start + 1, pipeIdx);
    const explanation = remaining.slice(pipeIdx + 1, end);
    elements.push(
      <GlossaryTerm key={key++} term={term} explanation={explanation} />
    );

    remaining = remaining.slice(end + 1);
  }

  return { elements, nextKey: key };
}

/* ------------------------------------------------------------------ */
/* GlossaryTerm                                                        */
/* ------------------------------------------------------------------ */

function GlossaryTerm({ term, explanation }) {
  const [show, setShow] = useState(false);
  const [above, setAbove] = useState(false);
  const ref = useRef(null);

  function handleMouseEnter() {
    if (ref.current) {
      const rect = ref.current.getBoundingClientRect();
      setAbove(rect.bottom + 100 > window.innerHeight);
    }
    setShow(true);
  }

  return (
    <span
      ref={ref}
      style={glossaryStyles.term}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setShow(false)}
    >
      {term}
      {show && (
        <span style={{
          ...glossaryStyles.tooltip,
          ...(above ? { bottom: "calc(100% + 6px)", top: "auto" } : { top: "calc(100% + 6px)" }),
        }}>
          {explanation}
        </span>
      )}
    </span>
  );
}

const glossaryStyles = {
  term: {
    position: "relative",
    borderBottom: "1.5px dotted var(--havn-accent)",
    color: "var(--havn-accent)",
    cursor: "help",
    display: "inline",
  },
  tooltip: {
    position: "absolute",
    left: "50%",
    transform: "translateX(-50%)",
    width: "260px",
    padding: "10px 12px",
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius-lg, 8px)",
    boxShadow: "0 8px 24px rgba(0, 0, 0, 0.4)",
    color: "var(--havn-text-secondary)",
    fontSize: "12px",
    lineHeight: 1.55,
    zIndex: 10,
    pointerEvents: "none",
    whiteSpace: "normal",
  },
};

/* ------------------------------------------------------------------ */
/* Illustration (bottom-right decorative image)                        */
/* ------------------------------------------------------------------ */

function Illustration({ id }) {
  const [imgError, setImgError] = useState(false);
  const imgSrc = `/onboarding/${id}.png`;

  useEffect(() => {
    setImgError(false);
  }, [id]);

  if (!imgError) {
    return (
      <img
        src={imgSrc}
        alt=""
        onError={() => setImgError(true)}
        style={{
          width: "180px",
          height: "180px",
          objectFit: "contain",
          opacity: 0.85,
          pointerEvents: "none",
          flexShrink: 0,
        }}
      />
    );
  }

  return null;
}

/* ------------------------------------------------------------------ */
/* Spotlight overlay: dims everything except the target element         */
/* ------------------------------------------------------------------ */

const ONBOARDING_KEYFRAMES = `
@keyframes havn-onboarding-card-enter {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
`;

function HighlightOverlay({ selector }) {
  const prevElsRef = useRef([]);
  const styleRef = useRef(null);

  // Normalize selector to array
  const selectors = selector
    ? (Array.isArray(selector) ? selector : [selector])
    : [];

  // Inject keyframes once
  useEffect(() => {
    if (!styleRef.current) {
      const s = document.createElement("style");
      s.textContent = ONBOARDING_KEYFRAMES;
      document.head.appendChild(s);
      styleRef.current = s;
    }
    return () => {
      if (styleRef.current) {
        styleRef.current.remove();
        styleRef.current = null;
      }
    };
  }, []);

  // Lift target elements above the overlay, restore on change/unmount
  useEffect(() => {
    function restore() {
      for (const el of prevElsRef.current) {
        el.style.removeProperty("z-index");
        el.style.removeProperty("position");
      }
      prevElsRef.current = [];
    }

    if (selectors.length === 0) {
      restore();
      return;
    }

    function applyOne(sel) {
      const el = document.querySelector(sel);
      if (!el) return false;
      const cs = getComputedStyle(el);
      if (cs.position === "static") {
        el.style.position = "relative";
      }
      el.style.zIndex = "9999";
      prevElsRef.current.push(el);
      return true;
    }

    function applyAll() {
      restore();
      let allFound = true;
      for (const sel of selectors) {
        if (!applyOne(sel)) allFound = false;
      }
      return allFound;
    }

    // Try immediately, retry briefly if elements aren't rendered yet
    if (!applyAll()) {
      let attempts = 0;
      const interval = setInterval(() => {
        attempts++;
        if (applyAll() || attempts >= 10) clearInterval(interval);
      }, 50);
      return () => { clearInterval(interval); restore(); };
    }

    return () => restore();
  }, [selector]);

  if (selectors.length === 0) return null;

  // Simple full-screen dim — target elements sit above it via z-index
  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0, 0, 0, 0.7)",
        pointerEvents: "none",
        zIndex: 9998,
      }}
    />,
    document.body
  );
}

/* ------------------------------------------------------------------ */
/* Main Onboarding Component                                           */
/* ------------------------------------------------------------------ */

export default function Onboarding({ onComplete, isOpen, onNavigate, tables, onSelectTable, files, onOpenFile, isSample, onClearSample }) {
  const [currentStep, setCurrentStep] = useState(0);
  const [direction, setDirection] = useState(1); // 1 = forward, -1 = backward
  const [contentKey, setContentKey] = useState(0); // triggers content animation
  const [highlightSelector, setHighlightSelector] = useState(null);
  const [cardVisible, setCardVisible] = useState(false);
  const highlightTimerRef = useRef(null);
  const steps = ONBOARDING_STEPS;
  const step = steps[currentStep];
  const transitioning = useRef(false);

  // Reset on open
  useEffect(() => {
    if (isOpen) {
      setCurrentStep(0);
      setDirection(1);
      setContentKey(0);
      setHighlightSelector(null);
      setCardVisible(false);
      // Trigger card entrance animation
      requestAnimationFrame(() => setCardVisible(true));
    }
  }, [isOpen]);

  // Clear highlight when onboarding closes
  useEffect(() => {
    if (!isOpen) {
      setHighlightSelector(null);
      if (highlightTimerRef.current) {
        clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = null;
      }
    }
  }, [isOpen]);

  // Clear highlight if target element is no longer visible
  useEffect(() => {
    if (!isOpen || !highlightSelector) return;

    function checkHighlightValid() {
      const el = document.querySelector(highlightSelector);
      if (!el || el.offsetParent === null) {
        setHighlightSelector(null);
      }
    }

    const interval = setInterval(checkHighlightValid, 500);
    return () => clearInterval(interval);
  }, [isOpen, highlightSelector]);

  // Navigate to relevant page when step changes, then highlight + auto-actions
  useEffect(() => {
    if (!isOpen || !step) return;

    if (step.navigate && onNavigate) {
      onNavigate(step.navigate);
    }

    // Auto-select table with smart fallback
    if (step.autoSelectTable && tables && tables.length > 0 && onSelectTable) {
      setTimeout(() => {
        if (step.autoSelectTable === "gold.order_summary") {
          const goldSummary = tables.find(t => t.schema === "gold" && t.name === "order_summary");
          if (goldSummary) {
            onSelectTable("gold", "order_summary");
          } else {
            const goldTable = tables.find(t => t.schema === "gold");
            if (goldTable) {
              onSelectTable(goldTable.schema, goldTable.name);
            } else if (tables.length > 0) {
              onSelectTable(tables[0].schema, tables[0].name);
            }
          }
        } else {
          const t = tables[0];
          onSelectTable(t.schema, t.name);
        }
      }, 100);
    }

    // Auto-open first matching file (supports array of prefixes)
    if (step.autoOpenFile && files && onOpenFile) {
      const match = findFirstFile(files, step.autoOpenFile);
      if (match) {
        setTimeout(() => onOpenFile(match), 150);
      }
    }

    // Pre-fill query
    if (step.prefillQuery) {
      setTimeout(() => {
        let sql = step.prefillQuery;
        if (sql === "__auto__" && tables && tables.length > 0) {
          // Pick first gold table, then silver, then bronze, then any non-internal table
          const pick = tables.find(t => t.schema === "gold")
            || tables.find(t => t.schema === "silver")
            || tables.find(t => t.schema === "bronze")
            || tables.find(t => t.schema && !t.schema.startsWith("_"));
          if (pick) {
            sql = `SELECT * FROM ${pick.schema}.${pick.name} LIMIT 20`;
          } else {
            sql = `SELECT * FROM ${tables[0].schema}.${tables[0].name} LIMIT 20`;
          }
        } else if (sql === "__auto__") {
          sql = "SELECT 'Run a pipeline first to see data here' AS hint";
        }
        window.__dp_prefill_query = { sql, run: true };
      }, 200);
    }

    // Set highlight immediately — HighlightOverlay retries if element isn't rendered yet
    setHighlightSelector(step.highlight || null);
  }, [currentStep, isOpen]);

  // Keyboard navigation
  useEffect(() => {
    if (!isOpen) return;
    function handleKey(e) {
      if (e.key === "Escape") {
        onComplete();
      } else if (e.key === "ArrowRight" || e.key === "Enter") {
        goNext();
      } else if (e.key === "ArrowLeft") {
        goPrev();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [isOpen, currentStep]);

  function goNext() {
    if (transitioning.current) return;
    if (currentStep < steps.length - 1) {
      transitioning.current = true;
      setDirection(1);
      setHighlightSelector(null);
      setTimeout(() => {
        setCurrentStep((s) => s + 1);
        setContentKey((k) => k + 1);
        transitioning.current = false;
      }, 250);
    } else {
      onComplete();
    }
  }

  function goPrev() {
    if (transitioning.current) return;
    if (currentStep > 0) {
      transitioning.current = true;
      setDirection(-1);
      setHighlightSelector(null);
      setTimeout(() => {
        setCurrentStep((s) => s - 1);
        setContentKey((k) => k + 1);
        transitioning.current = false;
      }, 250);
    }
  }

  function goTo(index) {
    if (transitioning.current || index === currentStep) return;
    transitioning.current = true;
    setDirection(index > currentStep ? 1 : -1);
    setHighlightSelector(null);
    setTimeout(() => {
      setCurrentStep(index);
      setContentKey((k) => k + 1);
      transitioning.current = false;
    }, 250);
  }

  if (!isOpen || !step) return null;

  const isLast = currentStep === steps.length - 1;

  // Content animation: slides left/right + fades based on direction
  const contentAnimStyle = {
    animation: `havn-ob-content-slide 250ms ease-out both`,
  };

  // We inject dynamic keyframes for the content slide direction
  const slideKeyframes = `
    @keyframes havn-ob-content-slide {
      from { opacity: 0; transform: translateX(${direction > 0 ? "20px" : "-20px"}); }
      to { opacity: 1; transform: translateX(0); }
    }
  `;

  // Compute card position based on step.position
  function getPositionStyle() {
    const pos = step.position || "bottom-center";
    switch (pos) {
      case "center":
        return { position: "fixed", top: "50%", left: "50%", transform: "translate(-50%, -50%)", zIndex: 9999 };
      case "bottom-center":
        return { position: "fixed", bottom: "24px", left: "50%", transform: "translateX(-50%)", zIndex: 9999 };
      case "below-overview": {
        // Position under the "Overview" nav button, left-aligned
        const navEl = document.querySelector('[data-havn-guide="tabs"]');
        if (navEl) {
          const firstBtn = navEl.querySelector("button");
          const r = firstBtn ? firstBtn.getBoundingClientRect() : navEl.getBoundingClientRect();
          return { position: "fixed", top: `${r.bottom + 12}px`, left: `${r.left}px`, zIndex: 9999 };
        }
        return { position: "fixed", top: "60px", left: "24px", zIndex: 9999 };
      }
      case "below-actions": {
        const actEl = document.querySelector('[data-havn-guide="actions"]');
        if (actEl) {
          const r = actEl.getBoundingClientRect();
          return { position: "fixed", top: `${r.bottom + 12}px`, right: "24px", zIndex: 9999 };
        }
        return { position: "fixed", top: "60px", right: "24px", zIndex: 9999 };
      }
      case "beside-sidebar": {
        // Right of the sidebar, vertically centered
        const sidebar = document.querySelector('[data-havn-guide="sidebar"]');
        if (sidebar) {
          const r = sidebar.getBoundingClientRect();
          return { position: "fixed", top: "50%", left: `${r.right + 24}px`, transform: "translateY(-50%)", zIndex: 9999 };
        }
        return { position: "fixed", top: "50%", left: "280px", transform: "translateY(-50%)", zIndex: 9999 };
      }
      default:
        return { position: "fixed", bottom: "24px", left: "50%", transform: "translateX(-50%)", zIndex: 9999 };
    }
  }

  return (
    <>
      <HighlightOverlay selector={highlightSelector} />
      <style>{slideKeyframes}</style>
      <div style={{
        ...getPositionStyle(),
        animation: cardVisible ? "havn-onboarding-card-enter 300ms ease-out both" : "none",
        opacity: cardVisible ? undefined : 0,
      }}>
        <div style={styles.card}>
          {/* Top section: step counter */}
          <div style={styles.stepCounter}>
            {currentStep + 1} / {steps.length}
          </div>

          {/* Body: text on left, illustration on right */}
          <div key={contentKey} style={{ ...styles.body, ...contentAnimStyle }}>
            <div style={styles.textArea}>
              <h2 style={styles.title}>{step.title}</h2>
              <p style={styles.description}>
                {parseDescription(step.description)}
              </p>
            </div>
            <div style={styles.illustrationArea}>
              <Illustration id={step.illustration} />
            </div>
          </div>

          {/* Navigation */}
          <div style={styles.nav}>
            <div style={styles.dots}>
              {steps.map((_, i) => (
                <button
                  key={i}
                  onClick={() => goTo(i)}
                  aria-label={`Go to step ${i + 1}`}
                  style={{
                    ...styles.dot,
                    background: i === currentStep ? "var(--havn-accent)" : "var(--havn-border-light)",
                    width: i === currentStep ? "20px" : "8px",
                  }}
                />
              ))}
            </div>

            <div style={styles.buttonGroup}>
              <div style={styles.buttons}>
                {currentStep > 0 && (
                  <button onClick={goPrev} style={styles.btnGhost}>
                    Back
                  </button>
                )}
                <button onClick={goNext} style={styles.btnPrimary}>
                  {isLast ? "Start Building" : "Next"}
                </button>
                {isLast && isSample && onClearSample && (
                  <button onClick={() => { onComplete(); onClearSample(); }} style={styles.btnGhost}>
                    Start Fresh
                  </button>
                )}
              </div>
              {!isLast && (
                <button onClick={onComplete} style={styles.btnSkip}>
                  Skip tour
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const styles = {
  // overlay position is now computed dynamically by getPositionStyle()
  card: {
    width: "520px",
    maxWidth: "calc(100vw - 48px)",
    background: "var(--havn-bg-secondary)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius-lg, 8px)",
    boxShadow: "0 16px 48px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(0, 0, 0, 0.1)",
    padding: "20px 24px 18px",
    display: "flex",
    flexDirection: "column",
  },
  stepCounter: {
    fontSize: "11px",
    color: "var(--havn-text-dim)",
    fontWeight: 600,
    letterSpacing: "0.5px",
    marginBottom: "12px",
    fontFamily: "var(--havn-font-mono, monospace)",
  },
  body: {
    display: "flex",
    gap: "16px",
    alignItems: "flex-start",
    minHeight: "160px",
  },
  textArea: {
    flex: 1,
    minWidth: 0,
  },
  illustrationArea: {
    flexShrink: 0,
    display: "flex",
    alignItems: "flex-end",
    justifyContent: "flex-end",
  },
  title: {
    fontSize: "20px",
    fontWeight: 600,
    color: "var(--havn-text)",
    margin: "0 0 10px",
    lineHeight: 1.3,
  },
  description: {
    fontSize: "14px",
    color: "var(--havn-text-secondary)",
    lineHeight: 1.7,
    margin: 0,
  },
  nav: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginTop: "18px",
    paddingTop: "14px",
    borderTop: "1px solid var(--havn-border)",
  },
  dots: {
    display: "flex",
    gap: "5px",
    alignItems: "center",
  },
  dot: {
    height: "8px",
    borderRadius: "4px",
    border: "none",
    cursor: "pointer",
    padding: 0,
    transition: "width 0.25s ease, background 0.25s ease",
  },
  buttonGroup: {
    display: "flex",
    flexDirection: "column",
    alignItems: "flex-end",
    gap: "6px",
  },
  buttons: {
    display: "flex",
    gap: "8px",
    alignItems: "center",
  },
  btnPrimary: {
    padding: "7px 18px",
    background: "var(--havn-accent)",
    border: "none",
    borderRadius: "var(--havn-radius, 4px)",
    color: "var(--havn-bg)",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 600,
  },
  btnGhost: {
    padding: "7px 14px",
    background: "transparent",
    border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius, 4px)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: "12px",
  },
  btnSkip: {
    padding: "2px 0",
    background: "none",
    border: "none",
    color: "var(--havn-text-dim)",
    cursor: "pointer",
    fontSize: "11px",
    opacity: 0.7,
  },
};

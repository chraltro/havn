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

// Six-step welcome tour. Earlier we shipped 11 steps which advertised
// "about two minutes" but felt longer because every screen got a tooltip
// even when the screen was self-explanatory. The hint system handles
// surface-area discovery on demand (DAG, Quality, Connectors, etc.); the
// tour itself focuses on the four things a first-time user actually needs:
// the layout, the project shape, where transforms go, and how to query.
const ONBOARDING_STEPS = [
  {
    id: "welcome",
    title: "Welcome to havn",
    description: "Your entire data warehouse lives in one file on your machine. No cloud, no accounts, no data leaving your network.\nThis quick tour gets you oriented in under a minute.",
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
    id: "project",
    title: "Your Project",
    description: "{Ingest scripts|Python files in ingest/ that pull raw data from external sources into landing/} bring data in, {SQL transforms|.sql files in transform/ that define how data moves between schema layers} shape it layer by layer through bronze, silver, and gold, and {export scripts|Python files in export/ that send finished data to external systems} send it out. The file tree on the left is your pipeline.",
    illustration: "data-flow",
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
    description: "Every .sql file in transform/ becomes a model. Add {@config|Sets materialization (table, view, or incremental), target schema, and incremental strategy} at the top to set options. Dependencies are picked up from your FROM and JOIN clauses automatically. No templates, no Jinja. Just SQL.",
    illustration: "transforms",
    navigate: "Editor",
    highlight: ['[data-havn-guide="main-panel"]', '[data-havn-guide="sub-tab-bar"]', '[data-havn-guide="files-pane"]'],
    position: "bottom-center",
    autoSelectTable: false,
    autoOpenFile: ["transform/silver", "transform/bronze", "transform"],
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
    id: "ready",
    title: "You're Ready",
    description: "Edit a SQL file. Run the pipeline. Query the results. That's the core loop.\nPress {Ctrl+K|Opens the command palette: search for files, tables, and commands from anywhere.} to find anything fast. Replay this tour anytime from Settings.\nData in safe waters.",
    illustration: "welcome",
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
    fontWeight: 500,
  },
  tooltip: {
    position: "absolute",
    left: "50%",
    transform: "translateX(-50%)",
    width: "240px",
    padding: "10px 12px",
    background: "var(--havn-bg)",
    border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius-lg, 8px)",
    boxShadow: "0 8px 24px rgba(0, 0, 0, 0.45)",
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
          width: "120px",
          height: "120px",
          objectFit: "contain",
          opacity: 0.8,
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
        let autoRun = true;
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
          // No warehouse yet: pre-fill a friendly SQL hint but DON'T auto-run.
          // Auto-running would 404 against an empty warehouse and show a
          // misleading error in the OUTPUT panel before the user has done
          // anything.
          sql = "-- Run a pipeline first, then come back and try a query like:\nSELECT 1 AS placeholder";
          autoRun = false;
        }
        window.__havn_prefill_query = { sql, run: autoRun };
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
          return { position: "fixed", top: `${r.bottom + 12}px`, right: "280px", zIndex: 9999 };
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
          {/* Top section: step counter + progress bar */}
          <div style={styles.stepCounterRow}>
            <span style={styles.stepCounter}>
              Step {currentStep + 1} of {steps.length}
            </span>
            <div style={styles.progressTrack}>
              <div style={{
                ...styles.progressBar,
                width: `${((currentStep + 1) / steps.length) * 100}%`,
              }} />
            </div>
          </div>

          {/* Body: text on left, illustration on right */}
          <div key={contentKey} style={{ ...styles.body, ...contentAnimStyle }}>
            <div style={styles.textArea}>
              <h2 style={styles.title}>{step.title}</h2>
              <p style={styles.description}>
                {parseDescription(step.description)}
              </p>
            </div>
            {step.illustration && (
              <div style={styles.illustrationArea}>
                <Illustration id={step.illustration} />
              </div>
            )}
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
                    background: i === currentStep
                      ? "var(--havn-accent)"
                      : i < currentStep
                        ? "var(--havn-text-dim)"
                        : "var(--havn-border-light)",
                    width: i === currentStep ? "18px" : "6px",
                    height: "6px",
                    opacity: i <= currentStep ? 1 : 0.6,
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
                  <span style={styles.keyHint}>↵</span>
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
    borderTop: "2px solid var(--havn-accent)",
    borderRadius: "var(--havn-radius-lg, 8px)",
    boxShadow: "0 12px 40px rgba(0, 0, 0, 0.45)",
    padding: "20px 24px 18px",
    display: "flex",
    flexDirection: "column",
  },
  stepCounterRow: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    marginBottom: "14px",
  },
  stepCounter: {
    fontSize: "11px",
    color: "var(--havn-text-dim)",
    fontWeight: 500,
    letterSpacing: "0.3px",
    fontFamily: "var(--havn-font-mono, monospace)",
    whiteSpace: "nowrap",
    flexShrink: 0,
  },
  progressTrack: {
    flex: 1,
    height: "2px",
    background: "var(--havn-border)",
    borderRadius: "1px",
    overflow: "hidden",
  },
  progressBar: {
    height: "100%",
    background: "var(--havn-accent)",
    borderRadius: "1px",
    transition: "width 0.3s ease",
  },
  body: {
    display: "flex",
    gap: "20px",
    alignItems: "flex-start",
    minHeight: "140px",
  },
  textArea: {
    flex: 1,
    minWidth: 0,
  },
  illustrationArea: {
    flexShrink: 0,
    display: "flex",
    alignItems: "center",
    justifyContent: "flex-end",
  },
  title: {
    fontSize: "18px",
    fontWeight: 600,
    color: "var(--havn-text)",
    margin: "0 0 10px",
    lineHeight: 1.3,
    letterSpacing: "-0.2px",
  },
  description: {
    fontSize: "13.5px",
    color: "var(--havn-text-secondary)",
    lineHeight: 1.65,
    margin: 0,
  },
  nav: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginTop: "16px",
    paddingTop: "14px",
    borderTop: "1px solid var(--havn-border)",
  },
  dots: {
    display: "flex",
    gap: "4px",
    alignItems: "center",
  },
  dot: {
    borderRadius: "3px",
    border: "none",
    cursor: "pointer",
    padding: 0,
    transition: "width 0.25s ease, background 0.25s ease, opacity 0.25s ease",
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
    padding: "7px 14px 7px 16px",
    background: "var(--havn-accent)",
    border: "none",
    borderRadius: "var(--havn-radius, 4px)",
    color: "var(--havn-bg)",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 600,
    display: "inline-flex",
    alignItems: "center",
    gap: "8px",
    transition: "opacity 0.15s ease",
  },
  btnGhost: {
    padding: "7px 14px",
    background: "transparent",
    border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius, 4px)",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: "12px",
    transition: "border-color 0.15s ease, color 0.15s ease",
  },
  btnSkip: {
    padding: "2px 0",
    background: "none",
    border: "none",
    color: "var(--havn-text-dim)",
    cursor: "pointer",
    fontSize: "11px",
    opacity: 0.65,
    transition: "opacity 0.15s ease",
  },
  keyHint: {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: "18px",
    height: "18px",
    borderRadius: "3px",
    background: "rgba(0, 0, 0, 0.18)",
    color: "inherit",
    fontSize: "12px",
    lineHeight: 1,
    flexShrink: 0,
    opacity: 0.7,
  },
};

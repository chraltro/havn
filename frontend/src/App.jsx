import React, { useState, useEffect, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import { api } from "./api";
import FileTree from "./FileTree";
import Editor from "./Editor";
import OutputPanel from "./OutputPanel";
import QueryPanel from "./QueryPanel";
import TablesPanel from "./TablesPanel";
import HistoryPanel from "./HistoryPanel";
import DAGPanel from "./DAGPanel";
import SentinelPanel from "./SentinelPanel";
import DiffPanel from "./DiffPanel";
import DocsPanel from "./DocsPanel";
import NotebookPanel from "./NotebookPanel";
import DataSourcesPanel from "./DataSourcesPanel";
import OverviewPanel from "./OverviewPanel";
import RunSummary from "./RunSummary";
import SettingsPanel from "./SettingsPanel";
import MaskingPanel from "./MaskingPanel";
import QualityPanel from "./QualityPanel";
import WikiPanel from "./WikiPanel";
import LoginPage from "./LoginPage";
import ResizeHandle from "./ResizeHandle";
import useResizable from "./useResizable";
import SortableTable from "./SortableTable";
import Onboarding from "./Onboarding";
import ErrorBoundary from "./ErrorBoundary";
import Hint from "./Hint";
import { useHintTriggerFn } from "./HintSystem";
import EnvironmentSwitcher from "./EnvironmentSwitcher";
import ModelNotebookView from "./ModelNotebookView";
import NewModelDialog from "./NewModelDialog";
import GitPanel from "./GitPanel";
import AgentSidebar from "./AgentSidebar";
import CommandPalette from "./CommandPalette";
import FocusTrap from "./FocusTrap";
import DashboardListPanel from "./DashboardListPanel";
import DashboardCanvas from "./DashboardCanvas";
import WidgetEditor from "./WidgetEditor";
import DashboardFilterBar from "./DashboardFilterBar";
import { DashboardProvider, useDashboard } from "./DashboardContext";
import { useAuth } from "./AuthContext";
import { WarehouseProvider, useWarehouse } from "./WarehouseContext";
import { schemaCompare } from "./schemaOrder";
import { PipelineProvider, usePipeline } from "./PipelineContext";


/* ------------------------------------------------------------------ */
/* Section-based navigation                                            */
/* ------------------------------------------------------------------ */

const SECTIONS = [
  { id: "Overview", label: "Overview", tabs: [] },
  { id: "Develop", label: "Develop", tabs: ["Editor", "Notebooks", "Data Sources", "Git"] },
  { id: "Explore", label: "Explore", tabs: ["Query", "Tables", "DAG", "Dashboards"] },
  { id: "Observe", label: "Observe", tabs: ["Quality", "Sentinel", "Diff", "Runs"] },
  { id: "Configure", label: "Configure", tabs: ["Masking", "Wiki", "Docs", "Settings"] },
];

// Quick lookup: tab name -> section id
const TAB_TO_SECTION = {};
for (const s of SECTIONS) {
  if (s.tabs.length === 0) TAB_TO_SECTION[s.id] = s.id;
  for (const t of s.tabs) TAB_TO_SECTION[t] = s.id;
}

// Default tab for each section (first sub-tab or the section itself)
const SECTION_DEFAULT = {};
for (const s of SECTIONS) {
  SECTION_DEFAULT[s.id] = s.tabs.length > 0 ? s.tabs[0] : s.id;
}

/* ------------------------------------------------------------------ */
/* Pipeline Run Menu (replaces 5 separate action buttons)              */
/* ------------------------------------------------------------------ */

function PipelineMenu({ running, onRunPipeline, onLint, onContracts, onCancel }) {
  const [open, setOpen] = useState(false);
  const [selectedSteps, setSelectedSteps] = useState(["ingest", "transform", "export"]);
  const [onlyChanged, setOnlyChanged] = useState(false);
  const [autoFix, setAutoFix] = useState(true);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  function toggleStep(step) {
    setSelectedSteps(prev =>
      prev.includes(step)
        ? prev.filter(s => s !== step)
        : [...prev, step]
    );
  }

  const defaultRun = () => {
    onRunPipeline(["ingest", "transform", "export"], !onlyChanged);
  };

  return (
    <div ref={ref} style={pmStyles.wrapper}>
      {running ? (
        <button onClick={onCancel} aria-label="Cancel pipeline" style={{...pmStyles.btn, background: "var(--havn-error, #c0392b)", color: "#fff", borderColor: "var(--havn-error, #c0392b)", borderRadius: "var(--havn-radius-lg)"}}>
          {"\u25A0"} Cancel
        </button>
      ) : (
        <>
          <button onClick={defaultRun} disabled={running} aria-label="Run pipeline" style={pmStyles.btn}>
            {"\u25B6"} Run
          </button>
          <button
            onClick={() => setOpen(!open)}
            disabled={running}
            style={pmStyles.chevron}
            aria-label="Open run menu"
            aria-expanded={open}
            aria-haspopup="menu"
          >
            {"\u25BE"}
          </button>
        </>
      )}
      {open && (
        <div style={pmStyles.menu} role="menu" aria-label="Pipeline actions">
          <div style={pmStyles.groupLabel}>Steps</div>
          <div style={pmStyles.pillRow}>
            {["ingest", "transform", "export"].map((step) => {
              const active = selectedSteps.includes(step);
              return (
                <button
                  key={step}
                  onClick={() => toggleStep(step)}
                  style={active ? pmStyles.pillActive : pmStyles.pillInactive}
                  aria-pressed={active}
                >
                  {step.charAt(0).toUpperCase() + step.slice(1)}
                </button>
              );
            })}
          </div>

          <label style={pmStyles.checkboxRow}>
            <input
              type="checkbox"
              checked={onlyChanged}
              onChange={(e) => setOnlyChanged(e.target.checked)}
              style={{ margin: 0 }}
            />
            <span style={{ fontSize: "12px", color: "var(--havn-text)" }}>Only changed</span>
          </label>
          <div style={pmStyles.hintText}>
            Skip models that haven't changed since last build
          </div>

          <button
            style={pmStyles.runSelectedBtn}
            disabled={running || selectedSteps.length === 0}
            onClick={() => {
              onRunPipeline(selectedSteps, !onlyChanged);
              setOpen(false);
            }}
          >
            {"\u25B6"} Run{selectedSteps.length < 3 ? " Selected" : ""}
          </button>

          <div style={pmStyles.divider} />
          <div style={pmStyles.groupLabel}>Validate</div>
          <div style={{ padding: "4px 12px", display: "flex", alignItems: "center", gap: "8px" }}>
            <button style={{ ...pmStyles.item, flex: 1, textAlign: "center", background: "var(--havn-btn-bg)", borderRadius: "var(--havn-radius)", padding: "5px 0" }} disabled={running} onClick={() => { onLint(autoFix); setOpen(false); }}
              onMouseEnter={(e) => e.currentTarget.style.filter = "brightness(1.15)"}
              onMouseLeave={(e) => e.currentTarget.style.filter = ""}
            >Lint</button>
            <button style={{ ...pmStyles.item, flex: 1, textAlign: "center", background: "var(--havn-btn-bg)", borderRadius: "var(--havn-radius)", padding: "5px 0" }} disabled={running} onClick={() => { onContracts(); setOpen(false); }}
              onMouseEnter={(e) => e.currentTarget.style.filter = "brightness(1.15)"}
              onMouseLeave={(e) => e.currentTarget.style.filter = ""}
            >Contracts</button>
          </div>
          <label style={{ ...pmStyles.checkboxRow, paddingTop: "2px" }}>
            <input
              type="checkbox"
              checked={autoFix}
              onChange={(e) => setAutoFix(e.target.checked)}
              style={{ margin: 0 }}
            />
            <span style={{ fontSize: "11px", color: "var(--havn-text-secondary)" }}>Auto-fix lint issues</span>
          </label>
        </div>
      )}
    </div>
  );
}

const pmStyles = {
  wrapper: { position: "relative", display: "inline-flex" },
  btn: {
    padding: "5px 14px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)",
    borderRadius: "var(--havn-radius-lg) 0 0 var(--havn-radius-lg)", color: "#fff", cursor: "pointer",
    fontSize: "12px", fontWeight: 600, letterSpacing: "0.3px",
  },
  chevron: {
    padding: "5px 7px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)",
    borderRadius: "0 var(--havn-radius-lg) var(--havn-radius-lg) 0", color: "#fff", cursor: "pointer",
    fontSize: "10px", borderLeft: "1px solid rgba(255,255,255,0.2)", marginLeft: "-1px",
  },
  menu: {
    position: "absolute", top: "100%", right: 0, marginTop: "4px",
    background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)",
    borderRadius: "var(--havn-radius)", zIndex: 10000, minWidth: "220px",
    boxShadow: "0 4px 16px rgba(0,0,0,0.3)", padding: "4px 0",
  },
  groupLabel: {
    padding: "6px 12px 2px", fontSize: "10px", fontWeight: 600,
    color: "var(--havn-text-dim)", textTransform: "uppercase", letterSpacing: "0.5px",
  },
  item: {
    display: "block", width: "100%", padding: "6px 12px", background: "none",
    border: "none", color: "var(--havn-text)", cursor: "pointer", fontSize: "12px",
    textAlign: "left", whiteSpace: "nowrap",
  },
  divider: { height: "1px", background: "var(--havn-border)", margin: "4px 0" },
  pillRow: {
    display: "flex", gap: "6px", padding: "6px 12px",
  },
  pillActive: {
    padding: "4px 12px", fontSize: "11px", fontWeight: 600, cursor: "pointer",
    background: "var(--havn-accent)", color: "var(--havn-bg)", border: "1px solid var(--havn-accent)",
    borderRadius: "var(--havn-radius-lg)",
  },
  pillInactive: {
    padding: "4px 12px", fontSize: "11px", fontWeight: 600, cursor: "pointer",
    background: "transparent", color: "var(--havn-text-secondary)", border: "1px solid var(--havn-border-light)",
    borderRadius: "var(--havn-radius-lg)",
  },
  checkboxRow: {
    display: "flex", alignItems: "center", gap: "6px", padding: "6px 12px 0",
    cursor: "pointer",
  },
  hintText: {
    padding: "2px 12px 6px", fontSize: "10px", color: "var(--havn-text-dim)",
    lineHeight: 1.3,
  },
  runSelectedBtn: {
    display: "block", width: "calc(100% - 24px)", margin: "4px 12px 8px",
    padding: "6px 0", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)",
    borderRadius: "var(--havn-radius)", color: "#fff", cursor: "pointer",
    fontSize: "12px", fontWeight: 600, textAlign: "center",
  },
};

/* ------------------------------------------------------------------ */
/* Schema tree (sidebar)                                               */
/* ------------------------------------------------------------------ */

function groupBySchema(tables) {
  const schemas = {};
  for (const t of tables) {
    if (!schemas[t.schema]) schemas[t.schema] = [];
    schemas[t.schema].push(t);
  }
  return schemas;
}

function SchemaTree({ tables, selectedTable, onSelectTable, filter }) {
  const allSchemas = groupBySchema(tables);
  // Apply filter to table names
  const schemas = {};
  for (const [schema, tbls] of Object.entries(allSchemas)) {
    const filtered = filter ? tbls.filter(t => t.name.toLowerCase().includes(filter.toLowerCase())) : tbls;
    if (filtered.length > 0) schemas[schema] = filtered;
  }
  const schemaNames = Object.keys(schemas).sort(schemaCompare);
  const [expanded, setExpanded] = useState(() => {
    const m = {};
    for (const s of schemaNames) m[s] = true;
    return m;
  });
  const activeTableRef = useRef(null);

  useEffect(() => {
    setExpanded((prev) => {
      const next = { ...prev };
      for (const s of schemaNames) {
        if (!(s in next)) next[s] = true;
      }
      return next;
    });
  }, [tables]);

  // Auto-scroll to selected table
  useEffect(() => {
    if (selectedTable && activeTableRef.current) {
      activeTableRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [selectedTable]);

  if (tables.length === 0) {
    return <div style={stStyles.empty}>No tables yet</div>;
  }

  return (
    <div>
      {schemaNames.map((schema) => (
        <div key={schema}>
          <div
            style={stStyles.schemaRow}
            onClick={() => setExpanded((prev) => ({ ...prev, [schema]: !prev[schema] }))}
          >
            <span style={{ ...stStyles.arrow, transform: expanded[schema] ? "rotate(0deg)" : "rotate(-90deg)" }}>
              {"\u25BE"}
            </span>
            <span style={stStyles.schemaName}>{schema}</span>
            <span style={stStyles.schemaCount}>{schemas[schema].length}</span>
          </div>
          {expanded[schema] && schemas[schema].map((t) => {
            const key = `${t.schema}.${t.name}`;
            const isActive = selectedTable === key;
            return (
              <div
                data-havn-file=""
                key={key}
                ref={isActive ? activeTableRef : undefined}
                style={{
                  ...stStyles.tableRow,
                  background: isActive ? "var(--havn-bg-secondary)" : "transparent",
                  borderLeft: isActive ? "2px solid var(--havn-accent)" : "2px solid transparent",
                }}
                onClick={() => onSelectTable(t.schema, t.name)}
              >
                <span style={{
                  ...stStyles.typeIcon,
                  color: t.type === "VIEW" ? "var(--havn-purple)" : "var(--havn-accent)",
                }}>{t.type === "VIEW" ? "V" : "T"}</span>
                <span style={isActive ? stStyles.tableNameActive : stStyles.tableName}>{t.name}</span>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

const stStyles = {
  empty: { padding: "12px", color: "var(--havn-text-dim)", fontSize: "12px", textAlign: "center" },
  schemaRow: { display: "flex", alignItems: "center", gap: "6px", padding: "4px 8px", cursor: "pointer", margin: "0 4px", borderRadius: "3px" },
  arrow: { fontSize: "10px", color: "var(--havn-text-secondary)", width: "10px", display: "inline-block", transition: "transform 0.12s ease" },
  schemaName: { fontSize: "13px", fontWeight: 500, color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)" },
  schemaCount: { fontSize: "10px", color: "var(--havn-text-dim)", marginLeft: "auto" },
  tableRow: { display: "flex", alignItems: "center", gap: "6px", padding: "3px 8px 3px 30px", cursor: "pointer", fontSize: "12px", fontFamily: "var(--havn-font-mono)", margin: "0 4px", borderRadius: "3px" },
  typeIcon: { fontSize: "9px", fontWeight: 700, flexShrink: 0 },
  tableName: { color: "var(--havn-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  tableNameActive: { color: "var(--havn-accent)", fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
};

/* ------------------------------------------------------------------ */
/* Main app content                                                    */
/* ------------------------------------------------------------------ */

function DashboardsSection({ showConfirm, embedMode }) {
  const { dashboard, loadDashboard, closeDashboard, setEditMode } = useDashboard();
  const [editingWidget, setEditingWidget] = useState(null);

  // In embed mode, auto-load the dashboard from the hash and force view mode
  useEffect(() => {
    if (!embedMode) return;
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    const dashId = params.get("dashboard");
    if (dashId && !dashboard) {
      loadDashboard(dashId).then(() => setEditMode(false));
    }
  }, [embedMode]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!dashboard) {
    if (embedMode) return <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font)" }}>Loading dashboard...</div>;
    return <DashboardListPanel onOpenDashboard={(id) => loadDashboard(id)} showConfirm={showConfirm} />;
  }

  return (
    <>
      <DashboardFilterBar />
      <DashboardCanvas
        onBack={closeDashboard}
        onEditWidget={embedMode ? undefined : setEditingWidget}
        showConfirm={showConfirm}
        embedMode={embedMode}
      />
      {editingWidget && !embedMode && (
        <WidgetEditor
          widget={editingWidget}
          onClose={() => setEditingWidget(null)}
          onSave={() => setEditingWidget(null)}
        />
      )}
    </>
  );
}

function AppContent() {
  const { currentUser, handleLogout } = useAuth();
  const { tables, files, streams, loadFiles, refreshAll } = useWarehouse();
  const { running, output, runSummary, progress, addOutput, clearOutput, setRunSummary, runTransformAll, runStream, cancelPipeline, runLint, runCurrentScript, runSingleModel, runContracts, runPipeline } = usePipeline();

  // Editor state
  const [activeFile, setActiveFile] = useState(null);
  const [sidebarFilter, setSidebarFilter] = useState("");
  const activeFileRef = useRef(null);
  const [fileContent, setFileContent] = useState("");
  const [fileLang, setFileLang] = useState("sql");
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const [preview, setPreview] = useState(null);
  const [previewError, setPreviewError] = useState(null);
  const [previewRunning, setPreviewRunning] = useState(false);
  const [previewHeight, onPreviewResize, onPreviewResizeStart] = useResizable("havn_editor_preview_height", 200, 80, 600);

  // Tab/UI state
  const [activeTab, setActiveTab] = useState("Overview");
  const [selectedTable, setSelectedTable] = useState(null);

  // Derive active section from active tab
  const activeSection = TAB_TO_SECTION[activeTab] || "Overview";
  const currentSectionDef = SECTIONS.find(s => s.id === activeSection);
  const subTabs = currentSectionDef?.tabs || [];

  // Resizable panels
  const [sidebarWidth, onSidebarResize, onSidebarResizeStart] = useResizable("havn_sidebar_width", 240, 150, 500);
  const [outputHeight, onOutputResize, onOutputResizeStart] = useResizable("havn_output_height", 80, 40, 500);
  const [outputCollapsed, setOutputCollapsed] = useState(() => localStorage.getItem("havn_output_collapsed") === "true");
  const toggleOutputCollapsed = useCallback(() => {
    setOutputCollapsed(prev => {
      const next = !prev;
      localStorage.setItem("havn_output_collapsed", String(next));
      return next;
    });
  }, []);
  const [agentWidth, onAgentResize, onAgentResizeStart] = useResizable("havn_agent_width", 340, 240, 600);

  // Editor navigation
  const editorRef = useRef(null);
  const [goToLine, setGoToLine] = useState(null);

  // Onboarding state — per-project, keyed by project name
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [isSampleProject, setIsSampleProject] = useState(false);
  const onboardingProjectRef = useRef(null);
  useEffect(() => {
    api.getOverview().then((overview) => {
      const name = overview.project_name || "__default__";
      onboardingProjectRef.current = name;
      setIsSampleProject(!!overview.is_sample);
      if (!localStorage.getItem(`havn_onboarding_completed:${name}`)) {
        setOnboardingOpen(true);
      }
    }).catch(() => {});
  }, []);

  // Hint system triggers
  const setHintTrigger = useHintTriggerFn();
  const tabSwitchCountRef = useRef(0);

  useEffect(() => {
    setHintTrigger("warehouseHasTables", tables.length > 0);
  }, [tables, setHintTrigger]);

  const [notebookPath, setNotebookPath] = useState(null);
  const [modelNotebookName, setModelNotebookName] = useState(null);
  const [showNewDialog, setShowNewDialog] = useState(false);

  // Agent sidebar state
  const [agentSidebarOpen, setAgentSidebarOpen] = useState(false);

  // Run status indicator (header)
  const [recentStatus, setRecentStatus] = useState(null); // "success" | "failed" | null
  const prevRunningRef = useRef(false);
  useEffect(() => {
    if (prevRunningRef.current && !running) {
      // Running just went from true to false — determine status from runSummary
      // Only show status if we actually have a summary; skip if null (avoids
      // showing "Failed" when the summary hasn't been set yet)
      if (runSummary) {
        setRecentStatus(runSummary.status === "success" ? "success" : "failed");
        const timer = setTimeout(() => setRecentStatus(null), 10000);
        prevRunningRef.current = running;
        return () => clearTimeout(timer);
      }
    }
    prevRunningRef.current = running;
  }, [running, runSummary]);

  // Command palette state
  const [paletteOpen, setPaletteOpen] = useState(false);

  // Delete confirmation dialog state
  const [deleteConfirm, setDeleteConfirm] = useState(null);
  const deleteResolveRef = useRef(null);

  // Agent file conflict dialog state
  const [agentConflict, setAgentConflict] = useState(null);
  const agentConflictResolveRef = useRef(null);

  // Generic confirm dialog state (replaces browser confirm())
  const [confirmDialog, setConfirmDialog] = useState(null);
  const confirmResolveRef = useRef(null);
  function showConfirm(title, body, confirmLabel = "Confirm", danger = false) {
    return new Promise((resolve) => {
      confirmResolveRef.current = resolve;
      setConfirmDialog({ title, body, confirmLabel, danger });
    });
  }
  function resolveConfirm(result) {
    setConfirmDialog(null);
    if (confirmResolveRef.current) {
      confirmResolveRef.current(result);
      confirmResolveRef.current = null;
    }
  }

  function handleOnboardingComplete() {
    setOnboardingOpen(false);
    const name = onboardingProjectRef.current || "__default__";
    localStorage.setItem(`havn_onboarding_completed:${name}`, "true");
  }

  async function handleClearSample() {
    try {
      await api.clearSampleProject();
      window.location.reload();
    } catch (e) {
      addOutput("error", `Failed to clear sample project: ${e.message || e}`);
    }
  }

  function showGuide() {
    setOnboardingOpen(true);
  }

  const pendingSubTabRef = useRef(null);

  function navigateToTab(tab) {
    // Support "Quality:Contracts:filterValue" format to navigate to a sub-tab with filter
    if (tab.includes(":")) {
      const parts = tab.split(":");
      const mainTab = parts[0];
      const subTab = parts[1];
      const filter = parts[2] || null;
      pendingSubTabRef.current = { tab: mainTab, subTab, filter };
      setActiveTab(mainTab);
      // Dispatch after React render so the target panel is mounted
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent("havn-subtab", { detail: { tab: mainTab, subTab, filter } }));
        pendingSubTabRef.current = null;
      }, 100);
    } else {
      setActiveTab(tab);
    }
    tabSwitchCountRef.current += 1;
    setHintTrigger("tabSwitchCount", tabSwitchCountRef.current);
  }

  // Keyboard shortcuts: Alt+1..5 for sections, Ctrl/Cmd+K for command palette
  useEffect(() => {
    function handleKeyDown(e) {
      // Ctrl+K / Cmd+K — command palette
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if (!e.altKey) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey) return;
      const num = parseInt(e.key);
      if (num >= 1 && num <= SECTIONS.length) {
        e.preventDefault();
        const section = SECTIONS[num - 1];
        navigateToTab(SECTION_DEFAULT[section.id]);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  async function openFile(path, opts = {}) {
    path = path.replace(/\\/g, "/");
    if (path.endsWith(".dpnb")) {
      setNotebookPath(path);
      setActiveFile(path);
      setActiveTab("Notebooks");
      return;
    }
    if (path.endsWith(".sql") && path.startsWith("transform/") && opts.notebookView) {
      const parts = path.replace("transform/", "").replace(".sql", "").split("/");
      if (parts.length >= 2) {
        setModelNotebookName(`${parts[0]}.${parts[1]}`);
        return;
      }
    }
    if (dirty && activeFile) {
      const ok = await showConfirm("Unsaved changes", "Discard unsaved changes and open another file?", "Discard", true);
      if (!ok) return;
    }
    try {
      const data = await api.readFile(path);
      const content = data.content || "";

      // Binary file check: look for null bytes in first 1000 chars
      const sample = content.substring(0, 1000);
      if (sample.indexOf("\0") !== -1) {
        addOutput("warn", "This appears to be a binary file and cannot be opened in the editor.");
        return;
      }

      const sizeMB = (content.length / 1_000_000).toFixed(1);

      // Hard limit: refuse files > 10MB
      if (content.length > 10_000_000) {
        addOutput("error", `This file is too large to edit (${sizeMB}MB). Use an external editor or the query panel to inspect the data.`);
        return;
      }

      // Soft limit: warn for files > 1MB
      if (content.length > 1_000_000) {
        const ok = await showConfirm(
          "Large file",
          `This file is large (${sizeMB}MB). Large files may slow down the editor. Open anyway?`,
          "Open anyway",
          false
        );
        if (!ok) return;
      }

      setActiveFile(path);
      setFileContent(content);
      setFileLang(data.language);
      setDirty(false);
      setPreview(null);
      setPreviewError(null);
      setActiveTab("Editor");
    } catch (e) {
      addOutput("error", `Failed to open: ${e.message}`);
    }
  }

  function resolveFilePath(ref) {
    const normalized = ref.replace(/\\/g, "/");
    const hasExtension = /\.(sql|py|yml|yaml|json|csv|md|txt|dpnb)$/i.test(normalized);

    if (!hasExtension && !normalized.includes("/") && /^\w+\.\w+$/.test(normalized)) {
      const [schema, model] = normalized.split(".");
      return `transform/${schema}/${model}.sql`;
    }

    if (!normalized.includes("/")) {
      const allPaths = [];
      const collect = (nodes) => {
        for (const n of nodes) {
          if (n.type === "file") allPaths.push(n.path);
          if (n.children) collect(n.children);
        }
      };
      collect(files);
      const match = allPaths.find((f) => f.endsWith("/" + normalized) || f === normalized);
      if (match) return match;
    }

    return normalized;
  }

  async function openFileAtLine(ref, line, col) {
    const path = resolveFilePath(ref);
    if (activeFile === path) {
      setGoToLine({ line, col: col || 1 });
      setActiveTab("Editor");
    } else {
      try {
        const data = await api.readFile(path);
        setActiveFile(path);
        setFileContent(data.content);
        setFileLang(data.language);
        setDirty(false);
        setActiveTab("Editor");
        setTimeout(() => setGoToLine({ line, col: col || 1 }), 50);
      } catch (e) {
        addOutput("error", `Failed to open: ${e.message}`);
      }
    }
  }

  // Keep refs in sync for use in callbacks with stale closures
  activeFileRef.current = activeFile;
  dirtyRef.current = dirty;

  // Reload the currently open file from disk (used when agent edits it)
  async function reloadActiveFile() {
    const file = activeFileRef.current;
    if (!file) return;
    if (dirtyRef.current) {
      // User has unsaved changes — show custom dialog
      const choice = await new Promise((resolve) => {
        agentConflictResolveRef.current = resolve;
        setAgentConflict({ path: file });
      });
      if (choice !== "load") return;
    }
    try {
      const data = await api.readFile(file);
      setFileContent(data.content);
      setDirty(false);
    } catch {
      // file may have been deleted — ignore
    }
  }

  function resolveAgentConflict(choice) {
    setAgentConflict(null);
    if (agentConflictResolveRef.current) {
      agentConflictResolveRef.current(choice);
      agentConflictResolveRef.current = null;
    }
  }

  async function saveFile() {
    if (!activeFile) return;
    try {
      await api.saveFile(activeFile, fileContent);
      setDirty(false);
      addOutput("info", `Saved ${activeFile}`);
      setHintTrigger("firstFileEdited", true);
    } catch (e) {
      addOutput("error", `Failed to save: ${e.message}`);
    }
  }

  async function createFile(path) {
    path = path.replace(/\\/g, "/");
    if (!path.trim()) return;
    const defaultContent = path.endsWith(".py")
      ? '# A DuckDB connection is available as `db`\n\n'
      : path.endsWith(".sql")
      ? `-- config: materialized=table\n\nSELECT 1\n`
      : "";
    try {
      await api.saveFile(path, defaultContent);
      addOutput("info", `Created ${path}`);
      await loadFiles();
      openFile(path);
    } catch (e) {
      addOutput("error", `Failed to create: ${e.message}`);
    }
  }

  async function deleteFile(path) {
    path = path.replace(/\\/g, "/");

    const isTransform = path.endsWith(".sql") && path.startsWith("transform/");
    const isSeed = path.endsWith(".csv") && path.startsWith("seeds/");
    let dropObject = false;

    if (isTransform || isSeed) {
      const parts = path.split("/");
      const fileName = parts[parts.length - 1];
      const name = fileName.replace(/\.(sql|csv)$/, "");
      const schema = isSeed ? "seeds" : (parts.length >= 3 ? parts[1] : "bronze");
      const choice = await new Promise((resolve) => {
        deleteResolveRef.current = resolve;
        setDeleteConfirm({ path, schema, name, hasObject: true });
      });
      if (choice === "cancel") return;
      dropObject = choice === "drop";
    } else {
      const choice = await new Promise((resolve) => {
        deleteResolveRef.current = resolve;
        setDeleteConfirm({ path, hasObject: false });
      });
      if (choice === "cancel") return;
    }

    try {
      const result = await api.deleteFile(path, dropObject);
      addOutput("info", `Deleted ${path}`);
      if (result.dropped) {
        addOutput("info", `Dropped ${result.dropped} from warehouse`);
      }
      if (activeFile === path) {
        setActiveFile(null);
        setFileContent("");
        setDirty(false);
      }
      await loadFiles();
    } catch (e) {
      addOutput("error", `Failed to delete: ${e.message}`);
    }
  }

  async function moveFile(source, destination) {
    try {
      await api.moveFile(source, destination);
      addOutput("info", `Moved ${source} → ${destination}`);
      if (activeFile === source) {
        setActiveFile(destination);
      }
      await loadFiles();
    } catch (e) {
      addOutput("error", `Failed to move: ${e.message}`);
    }
  }

  function resolveDeleteConfirm(choice) {
    setDeleteConfirm(null);
    if (deleteResolveRef.current) {
      deleteResolveRef.current(choice);
      deleteResolveRef.current = null;
    }
  }

  async function runCurrentFile() {
    if (!activeFile) return;
    if (dirty) await saveFile();
    if (activeFile.endsWith(".sql")) {
      await runTransformAll(false);
    } else if (activeFile.endsWith(".py")) {
      await runCurrentScript(activeFile);
    } else if (activeFile.endsWith(".yml") && activeFile.startsWith("contracts/")) {
      await runContracts();
    }
  }

  async function handleRunSingleModel() {
    if (!activeFile || !activeFile.includes("transform/") || !activeFile.endsWith(".sql")) return;
    if (dirty) await saveFile();
    const modelName = activeFile.replace(/^transform\//, "").replace(/\.sql$/, "").replace(/\//g, ".");
    await runSingleModel(modelName);
  }

  async function formatCurrentFile() {
    if (!activeFile || !activeFile.endsWith(".sql")) return;
    addOutput("info", `Formatting ${activeFile}...`);
    try {
      const data = await api.lintFile(activeFile, true, fileContent);
      for (const v of data.violations || []) {
        addOutput("warn", `${activeFile}:${v.line}:${v.col} [${v.code}] ${v.description} (unfixable)`);
      }
      const fixed = data.fixed ?? 0;
      if (fixed > 0) addOutput("info", `${fixed} issue(s) fixed.`);
      else if (data.count === 0) addOutput("info", "No violations found.");
      if (data.content != null) setFileContent(data.content);
    } catch (e) {
      addOutput("error", e.message);
    }
  }

  async function previewCurrentFile() {
    if (!activeFile || !activeFile.endsWith(".sql")) return;
    const lines = fileContent.split("\n");
    let start = 0;
    for (const line of lines) {
      const s = line.trim();
      if (s.startsWith("-- config:") || s.startsWith("-- depends_on:") || s === "") { start++; } else break;
    }
    const sql = lines.slice(start).join("\n").trim();
    if (!sql) return;
    setPreviewRunning(true);
    setPreviewError(null);
    try {
      const data = await api.runQuery(sql);
      setPreview(data);
    } catch (e) {
      setPreviewError(e.message);
      setPreview(null);
    } finally {
      setPreviewRunning(false);
    }
  }

  function handleSelectTable(schema, name) {
    setSelectedTable(`${schema}.${name}`);
    setActiveTab("Tables");
  }

  function queryTable(schema, table) {
    navigateToTab("Query");
    window.__havn_prefill_query = { sql: `SELECT * FROM ${schema}.${table}`, run: true };
  }

  async function handleRunLintWithReload(fix = false) {
    await runLint(fix);
    if (fix && activeFile) {
      const d = await api.readFile(activeFile);
      setFileContent(d.content);
    }
  }

  const isTransformFile = activeFile && activeFile.includes("transform/") && activeFile.endsWith(".sql");

  return (
    <div style={styles.container}>
      {/* Header: logo + section nav + actions + user */}
      <header style={styles.header} role="banner">
        <button
          onClick={() => navigateToTab("Overview")}
          style={styles.logo}
          title="Home"
          aria-label="havn home"
        >
          <img src="/logo.svg" alt="havn" width="22" height="22" style={{ marginRight: "6px", verticalAlign: "middle" }} />
          havn
        </button>

        {/* Section navigation */}
        <nav style={styles.sectionNav} data-havn-guide="tabs" aria-label="Main navigation">
          {SECTIONS.map((section, i) => {
            const isActive = activeSection === section.id;
            return (
              <button
                key={section.id}
                data-havn-tab=""
                data-havn-active={isActive ? "true" : "false"}
                onClick={() => navigateToTab(SECTION_DEFAULT[section.id])}
                style={isActive ? styles.sectionActive : styles.section}
                title={`${section.label} (Alt+${i + 1})`}
                aria-current={isActive ? "true" : undefined}
              >
                {section.label}
              </button>
            );
          })}
        </nav>

        {/* Run status indicator */}
        {running && (
          <div style={{ display: "flex", alignItems: "center", gap: "6px", marginLeft: "auto", marginRight: "12px" }}>
            <div style={{
              width: 8, height: 8, borderRadius: "50%",
              background: "var(--havn-accent)",
              animation: "havn-pulse 1.5s ease-in-out infinite",
            }} />
            <span style={{ fontSize: "11px", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font-mono)" }}>
              Running...
            </span>
          </div>
        )}
        {!running && recentStatus && (
          <div style={{ display: "flex", alignItems: "center", gap: "6px", marginLeft: "auto", marginRight: "12px" }}>
            <span style={{ fontSize: "12px" }}>{recentStatus === "success" ? "\u2713" : "\u2717"}</span>
            <span style={{
              fontSize: "11px",
              fontFamily: "var(--havn-font-mono)",
              color: recentStatus === "success" ? "var(--havn-green)" : "var(--havn-red)",
            }}>
              {recentStatus === "success" ? "Done" : "Failed"}
            </span>
          </div>
        )}

        {/* Right side: run menu + new + env + user */}
        <div style={styles.headerRight} data-havn-guide="actions">
          <PipelineMenu
            running={running}
            onRunPipeline={runPipeline}
            onLint={handleRunLintWithReload}
            onContracts={runContracts}
            onCancel={cancelPipeline}
          />
          <button
            onClick={() => setAgentSidebarOpen((v) => !v)}
            style={agentSidebarOpen ? styles.btnPrimary : styles.btn}
            title="Toggle agent sidebar"
            aria-label="Toggle agent sidebar"
            aria-expanded={agentSidebarOpen}
          >
            Agent
          </button>
          <EnvironmentSwitcher showConfirm={showConfirm} />
          {currentUser && (
            <div style={styles.userInfo}>
              <span style={styles.userName}>{currentUser.display_name || currentUser.username}</span>
              <span style={styles.userRole}>{currentUser.role}</span>
              {currentUser.username !== "local" && (
                <button onClick={handleLogout} style={styles.logoutBtn} aria-label="Logout">Logout</button>
              )}
            </div>
          )}
        </div>
      </header>

      <div style={styles.main}>
        {/* Sidebar */}
        <aside style={{ ...styles.sidebar, width: sidebarWidth }} data-havn-guide="sidebar" role="navigation" aria-label="File browser">
          <div style={{ padding: "4px 8px", borderBottom: "1px solid var(--havn-border)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="var(--havn-text-dim)" strokeWidth="1.5" style={{ flexShrink: 0 }}>
                <circle cx="6.5" cy="6.5" r="5"/><path d="M10.5 10.5L14.5 14.5"/>
              </svg>
              <input
                value={sidebarFilter}
                onChange={(e) => setSidebarFilter(e.target.value)}
                placeholder="Filter files &amp; tables..."
                style={{ flex: 1, padding: "3px 6px", background: "var(--havn-bg)", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text)", fontSize: "11px", fontFamily: "var(--havn-font-mono)", outline: "none", minWidth: 0 }}
                aria-label="Filter files and tables"
              />
              {sidebarFilter && (
                <button onClick={() => setSidebarFilter("")} style={{ background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", padding: "0 2px", lineHeight: 1 }} aria-label="Clear filter">&times;</button>
              )}
              <button onClick={refreshAll} style={styles.sidebarRefreshBtn} title="Refresh files &amp; tables" aria-label="Refresh files and tables">&#x21BB;</button>
            </div>
          </div>
          <div style={styles.sidebarPane} data-havn-guide="files-pane">
            <div style={styles.sidebarSectionHeader}>
              <span>FILES</span>
              <button onClick={() => setShowNewDialog(true)} style={styles.sidebarNewBtn} title="Create new file" aria-label="Create new file">+</button>
            </div>
            <div style={styles.sidebarPaneContent}>
              <FileTree files={files} onSelect={openFile} activeFile={activeFile} onNewFile={createFile} onDeleteFile={deleteFile} onMoveFile={moveFile} filter={sidebarFilter} />
            </div>
          </div>
          <div style={styles.sidebarDivider} />
          <div style={styles.sidebarPane} data-havn-guide="tables-pane">
            <div style={styles.sidebarSectionHeader}>TABLES</div>
            <div style={styles.sidebarPaneContent}>
              <SchemaTree
                tables={tables}
                selectedTable={selectedTable}
                onSelectTable={handleSelectTable}
                filter={sidebarFilter}
              />
            </div>
          </div>
        </aside>

        <ResizeHandle
          direction="horizontal"
          onResize={onSidebarResize}
          onResizeStart={onSidebarResizeStart}
        />

        {/* Content */}
        <div style={styles.content} role="main">
          {/* Sub-tab bar (shown when section has multiple tabs) */}
          {subTabs.length > 0 && (
            <div style={styles.subTabBar} data-havn-hint="tab-bar" data-havn-guide="sub-tab-bar">
              {subTabs.map((tab) => (
                <button
                  key={tab}
                  data-havn-tab=""
                  data-havn-active={activeTab === tab ? "true" : "false"}
                  onClick={() => navigateToTab(tab)}
                  style={activeTab === tab ? styles.subTabActive : styles.subTab}
                >
                  {tab === "Editor" && dirty ? tab + " *" : tab}
                </button>
              ))}
              {/* Editor file actions inline */}
              {activeFile && activeTab === "Editor" && (
                <div style={styles.fileActions} data-havn-hint="editor-toolbar">
                  <span style={styles.fileName}>
                    {activeFile}
                    {dirty && <span style={styles.modifiedDot}> *</span>}
                  </span>
                  <button onClick={saveFile} disabled={!dirty} style={styles.btn}>
                    Save
                  </button>
                  {isTransformFile && (
                    <button onClick={handleRunSingleModel} disabled={running} style={styles.btn} title="Run just this model">
                      Run Model
                    </button>
                  )}
                  <button onClick={runCurrentFile} disabled={running} style={styles.btnPrimary}>
                    Run
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Panel */}
          <div style={styles.panel} data-havn-guide="main-panel">
            {activeTab === "Overview" && (
              <ErrorBoundary name="Overview">
                <OverviewPanel
                  onNavigate={navigateToTab}
                  onSelectTable={handleSelectTable}
                  onOpenFile={openFile}
                  onRunStream={(name, force) => {
                    if (name) runStream(name, force);
                    else {
                      const names = Object.keys(streams);
                      if (names.length > 0) runStream(names[0], force);
                      else addOutput("warn", "No streams defined in project.yml");
                    }
                  }}
                  streams={streams}
                  showConfirm={showConfirm}
                  onClearSample={handleClearSample}
                  refreshKey={tables}
                />
              </ErrorBoundary>
            )}
            {activeTab === "Dashboards" && (
              <ErrorBoundary name="Dashboards">
                <DashboardProvider>
                  <DashboardsSection showConfirm={showConfirm} />
                </DashboardProvider>
              </ErrorBoundary>
            )}
            {activeTab === "Editor" && (
              <ErrorBoundary name="Editor">
                <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
                  <div style={{ flex: 1, overflow: "hidden", minHeight: 0 }}>
                    <Editor
                      content={fileContent}
                      language={fileLang}
                      onChange={(val) => {
                        setFileContent(val);
                        setDirty(true);
                      }}
                      activeFile={activeFile}
                      onMount={(editor) => { editorRef.current = editor; }}
                      goToLine={goToLine}
                      onFormat={activeFile?.endsWith(".sql") ? formatCurrentFile : undefined}
                      onPreview={activeFile?.endsWith(".sql") ? previewCurrentFile : undefined}
                    />
                  </div>
                  {(preview || previewError || previewRunning) && (
                    <>
                      <ResizeHandle direction="vertical" onResize={(d) => onPreviewResize(-d)} onResizeStart={onPreviewResizeStart} />
                      <div style={{ height: previewHeight, flexShrink: 0, borderTop: "1px solid var(--havn-border)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
                        <div style={{ padding: "4px 12px", fontSize: "11px", color: "var(--havn-text-secondary)", borderBottom: "1px solid var(--havn-border)", display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0 }}>
                          <span>
                            {previewRunning ? "Running\u2026" : previewError ? "Error" : `${preview.rows.length} row${preview.rows.length !== 1 ? "s" : ""}, ${preview.columns.length} col${preview.columns.length !== 1 ? "s" : ""}`}
                          </span>
                          <button onClick={() => { setPreview(null); setPreviewError(null); }} style={{ background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", lineHeight: 1 }} aria-label="Close preview">{"\u00D7"}</button>
                        </div>
                        <div style={{ flex: 1, overflow: "auto" }}>
                          {previewError
                            ? <div style={{ padding: "8px 12px", color: "var(--havn-red)", fontFamily: "var(--havn-font-mono)", fontSize: "12px", whiteSpace: "pre-wrap" }}>{previewError}</div>
                            : preview && <SortableTable columns={preview.columns} rows={preview.rows} />
                          }
                        </div>
                      </div>
                    </>
                  )}
                </div>
              </ErrorBoundary>
            )}
            {activeTab === "Query" && <ErrorBoundary name="Query"><QueryPanel addOutput={addOutput} onOpenModel={(key) => { const [s, t] = key.split("."); openFile(`transform/${s}/${t}.sql`); }} /></ErrorBoundary>}
            {activeTab === "Tables" && <ErrorBoundary name="Tables"><TablesPanel selectedTable={selectedTable} onQueryTable={queryTable} /></ErrorBoundary>}
            {activeTab === "Data Sources" && <ErrorBoundary name="Data Sources"><DataSourcesPanel addOutput={addOutput} showConfirm={showConfirm} onDataChanged={refreshAll} /></ErrorBoundary>}
            {activeTab === "Notebooks" && <ErrorBoundary name="Notebooks"><NotebookPanel openPath={notebookPath} /></ErrorBoundary>}
            {activeTab === "DAG" && <ErrorBoundary name="DAG"><DAGPanel onOpenFile={openFile} showConfirm={showConfirm} /></ErrorBoundary>}
            {activeTab === "Git" && <ErrorBoundary name="Git"><GitPanel /></ErrorBoundary>}
            {activeTab === "Sentinel" && <ErrorBoundary name="Sentinel"><SentinelPanel /></ErrorBoundary>}
            {activeTab === "Diff" && <ErrorBoundary name="Diff"><DiffPanel api={api} addOutput={addOutput} /></ErrorBoundary>}
            {activeTab === "Docs" && <ErrorBoundary name="Docs"><DocsPanel /></ErrorBoundary>}
            {activeTab === "Quality" && <ErrorBoundary name="Quality"><QualityPanel addOutput={addOutput} /></ErrorBoundary>}
            {activeTab === "Masking" && <ErrorBoundary name="Masking"><MaskingPanel /></ErrorBoundary>}
            {activeTab === "Wiki" && <ErrorBoundary name="Wiki"><WikiPanel /></ErrorBoundary>}
            {activeTab === "Runs" && <ErrorBoundary name="Runs"><HistoryPanel onOpenFile={openFile} /></ErrorBoundary>}
            {activeTab === "Settings" && <ErrorBoundary name="Settings"><SettingsPanel onShowGuide={showGuide} showConfirm={showConfirm} /></ErrorBoundary>}
          </div>

          {/* Run summary */}
          {runSummary && (
            <div data-havn-hint="run-summary">
              <RunSummary
                summary={runSummary}
                onNavigate={navigateToTab}
                onDismiss={() => setRunSummary(null)}
              />
            </div>
          )}

          {/* Output */}
          {!outputCollapsed && <ResizeHandle
            direction="vertical"
            onResize={(delta) => onOutputResize(-delta)}
            onResizeStart={onOutputResizeStart}
          />}
          <div data-havn-guide="output" style={{ flexShrink: 0 }}>
            {running && progress > 0 && (
              <div style={{ height: 3, background: "#1a1a2e", width: "100%" }}>
                <div style={{ height: 3, background: "#2dd4bf", width: `${Math.round(progress * 100)}%`, transition: "width 0.3s ease" }} />
              </div>
            )}
            <OutputPanel output={output} onClear={clearOutput} height={outputCollapsed ? 28 : outputHeight} onOpenFile={openFileAtLine} running={running} progress={progress} collapsed={outputCollapsed} onToggleCollapse={toggleOutputCollapsed} />
          </div>
        </div>

        {/* Agent sidebar */}
        {agentSidebarOpen && (
          <>
            <ResizeHandle
              direction="horizontal"
              onResize={(d) => onAgentResize(-d)}
              onResizeStart={onAgentResizeStart}
            />
            <div style={{ ...styles.agentPanel, width: agentWidth }} aria-label="Agent">
              <AgentSidebar
                isOpen={agentSidebarOpen}
                onToggle={() => setAgentSidebarOpen(false)}
                onFileChanged={() => { reloadActiveFile(); refreshAll(); }}
                onOpenFile={openFile}
                onSelectTable={handleSelectTable}
              />
            </div>
          </>
        )}
      </div>

      {!onboardingOpen && <Hint onNavigate={navigateToTab} />}
      <Onboarding onComplete={handleOnboardingComplete} isOpen={onboardingOpen} onNavigate={navigateToTab} tables={tables} onSelectTable={handleSelectTable} files={files} onOpenFile={openFile} isSample={isSampleProject} onClearSample={handleClearSample} />

      {/* Dialog priority system: only one dialog visible at a time */}
      {/* Priority: delete > agent > confirm (highest to lowest) */}
      {deleteConfirm && !agentConflict ? (
        <FocusTrap labelledBy="havn-delete-dialog-title" style={dcStyles.overlay} onClick={() => resolveDeleteConfirm("cancel")}>
          <div style={dcStyles.dialog} onClick={(e) => e.stopPropagation()}>
            <div id="havn-delete-dialog-title" style={dcStyles.title}>Delete {deleteConfirm.path}?</div>
            {deleteConfirm.hasObject ? (
              <>
                <div style={dcStyles.body}>
                  Also drop <strong>{deleteConfirm.schema}.{deleteConfirm.name}</strong> table/view from the warehouse?
                </div>
                <div style={dcStyles.footer}>
                  <button onClick={() => resolveDeleteConfirm("cancel")} style={dcStyles.btnCancel}>Cancel</button>
                  <button onClick={() => resolveDeleteConfirm("keep")} style={dcStyles.btnSecondary}>Delete File Only</button>
                  <button onClick={() => resolveDeleteConfirm("drop")} style={dcStyles.btnDanger}>Delete & Drop</button>
                </div>
              </>
            ) : (
              <>
                <div style={dcStyles.body}>This file will be permanently deleted.</div>
                <div style={dcStyles.footer}>
                  <button onClick={() => resolveDeleteConfirm("cancel")} style={dcStyles.btnCancel}>Cancel</button>
                  <button onClick={() => resolveDeleteConfirm("keep")} style={dcStyles.btnDanger}>Delete</button>
                </div>
              </>
            )}
          </div>
        </FocusTrap>
      ) : agentConflict ? (
        <FocusTrap labelledBy="havn-agent-conflict-dialog-title" style={dcStyles.overlay} onClick={() => resolveAgentConflict("keep")}>
          <div style={dcStyles.dialog} onClick={(e) => e.stopPropagation()}>
            <div id="havn-agent-conflict-dialog-title" style={dcStyles.title}>Agent edited {agentConflict.path}</div>
            <div style={dcStyles.body}>
              You have unsaved changes to this file. Load the agent's version? Your unsaved changes will be lost.
            </div>
            <div style={dcStyles.footer}>
              <button onClick={() => resolveAgentConflict("keep")} style={dcStyles.btnCancel}>Keep my changes</button>
              <button onClick={() => resolveAgentConflict("load")} style={dcStyles.btnDanger}>Load agent version</button>
            </div>
          </div>
        </FocusTrap>
      ) : confirmDialog ? (
        <FocusTrap labelledBy="havn-confirm-dialog-title" style={dcStyles.overlay} onClick={() => resolveConfirm(false)}>
          <div style={dcStyles.dialog} onClick={(e) => e.stopPropagation()}>
            <div id="havn-confirm-dialog-title" style={dcStyles.title}>{confirmDialog.title}</div>
            <div style={dcStyles.body}>{confirmDialog.body}</div>
            <div style={dcStyles.footer}>
              <button onClick={() => resolveConfirm(false)} style={dcStyles.btnCancel}>Cancel</button>
              <button onClick={() => resolveConfirm(true)} style={confirmDialog.danger ? dcStyles.btnDanger : dcStyles.btnSecondary}>{confirmDialog.confirmLabel}</button>
            </div>
          </div>
        </FocusTrap>
      ) : null}

      {/* Model notebook view overlay */}
      {modelNotebookName && (
        <div style={{ position: "fixed", inset: 0, background: "var(--havn-bg)", zIndex: 900, overflow: "auto", padding: "16px" }}>
          <ModelNotebookView
            modelName={modelNotebookName}
            onClose={() => setModelNotebookName(null)}
            onSaved={() => { loadFiles(); }}
          />
        </div>
      )}

      {/* New model/notebook/ingest dialog */}
      {showNewDialog && (
        <NewModelDialog
          onClose={() => setShowNewDialog(false)}
          onCreated={(result) => {
            loadFiles();
            if (result.path) {
              addOutput("info", `Created ${result.path}`);
            }
          }}
        />
      )}

      {/* Command palette (Ctrl+K / Cmd+K) */}
      <CommandPalette
        isOpen={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        files={files}
        tables={tables}
        streams={streams}
        onOpenFile={(path) => { openFile(path); }}
        onNavigate={(tab) => { navigateToTab(tab); }}
        onRunStream={(name) => { runStream(name); }}
      />
    </div>
  );
}

/** Bridge component that provides PipelineProvider with access to WarehouseContext */
function WarehouseConsumerBridge({ children, onPipelineComplete }) {
  const { loadTables, refreshAll } = useWarehouse();
  const handleComplete = useCallback(() => {
    refreshAll();
    onPipelineComplete?.();
  }, [refreshAll, onPipelineComplete]);
  return (
    <PipelineProvider onTablesChanged={loadTables} onPipelineComplete={handleComplete}>
      {children}
    </PipelineProvider>
  );
}

/** Detect embed mode from URL hash */
function isEmbedMode() {
  const hash = window.location.hash.replace(/^#/, "");
  return new URLSearchParams(hash).get("embed") === "true";
}

/** Root App: wraps inner content with Warehouse + Pipeline providers */
export default function App() {
  const { authChecked, authRequired, needsSetup, handleLogin, isAuthenticated } = useAuth();

  const setHintTrigger = useHintTriggerFn();
  const onPipelineComplete = useCallback(() => {
    setHintTrigger("pipelineJustCompleted", true);
    setHintTrigger("pipelineRanThisSession", true);
  }, [setHintTrigger]);

  if (!authChecked) {
    return <div style={styles.loading}>Loading...</div>;
  }

  if (authRequired) {
    return <LoginPage onLogin={handleLogin} needsSetup={needsSetup} />;
  }

  // Embed mode: render only the dashboard with no chrome
  if (isEmbedMode()) {
    return (
      <WarehouseProvider enabled={isAuthenticated}>
        <div style={{ height: "100vh", background: "var(--havn-bg)", color: "var(--havn-text)", fontFamily: "var(--havn-font)" }}>
          <DashboardProvider>
            <DashboardsSection embedMode={true} />
          </DashboardProvider>
        </div>
      </WarehouseProvider>
    );
  }

  return (
    <WarehouseProvider enabled={isAuthenticated}>
      <WarehouseConsumerBridge onPipelineComplete={onPipelineComplete}>
        <AppContent />
      </WarehouseConsumerBridge>
    </WarehouseProvider>
  );
}

/* ------------------------------------------------------------------ */
/* Styles                                                              */
/* ------------------------------------------------------------------ */

const styles = {
  container: { display: "flex", flexDirection: "column", height: "100vh", background: "var(--havn-bg)", color: "var(--havn-text)", fontFamily: "var(--havn-font)" },
  loading: { display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", background: "var(--havn-bg)", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font)", fontSize: "14px" },

  // Header
  header: { display: "flex", alignItems: "center", padding: "0 16px", borderBottom: "1px solid var(--havn-border)", background: "var(--havn-bg-secondary)", minHeight: "44px", gap: "16px" },
  logo: { display: "inline-flex", alignItems: "center", fontSize: "18px", fontWeight: 700, fontFamily: "var(--havn-font)", color: "#3ECFB4", letterSpacing: "-0.5px", background: "none", border: "none", cursor: "pointer", padding: "8px 4px", flexShrink: 0 },

  // Section navigation (in header)
  sectionNav: { display: "flex", alignItems: "center", gap: "2px", flex: 1 },
  section: {
    padding: "10px 16px", background: "none", border: "none", borderBottom: "2px solid transparent",
    color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "13px", whiteSpace: "nowrap",
    fontWeight: 500, transition: "color 0.15s",
  },
  sectionActive: {
    padding: "10px 16px", background: "none", border: "none", borderBottom: "2px solid var(--havn-accent)",
    color: "var(--havn-text)", cursor: "pointer", fontSize: "13px", whiteSpace: "nowrap",
    fontWeight: 500, transition: "color 0.15s",
  },

  // Header right side
  headerRight: { display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 },
  userInfo: { display: "flex", alignItems: "center", gap: "8px", marginLeft: "4px" },
  userName: { fontSize: "12px", color: "var(--havn-text)", fontWeight: 500 },
  userRole: { fontSize: "10px", color: "var(--havn-text-secondary)", background: "var(--havn-btn-bg)", padding: "2px 8px", borderRadius: "10px", fontWeight: 500, textTransform: "capitalize" },
  logoutBtn: { padding: "3px 8px", background: "none", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "11px" },

  // Layout
  main: { display: "flex", flex: 1, overflow: "hidden" },
  sidebar: { borderRight: "1px solid var(--havn-border)", overflow: "hidden", background: "var(--havn-bg-tertiary)", padding: "0", flexShrink: 0, display: "flex", flexDirection: "column" },
  sidebarPane: { flex: 1, minHeight: 0, display: "flex", flexDirection: "column" },
  sidebarPaneContent: { flex: 1, overflow: "auto", minHeight: 0, padding: "0 0 8px" },
  sidebarDivider: { height: "1px", background: "var(--havn-border)", margin: "0 12px", flexShrink: 0 },
  sidebarSectionHeader: { display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 12px 6px", fontSize: "10px", fontWeight: "600", color: "var(--havn-text-dim)", letterSpacing: "1px", textTransform: "uppercase", flexShrink: 0 },
  sidebarNewBtn: { background: "none", border: "1px solid var(--havn-border)", borderRadius: "var(--havn-radius)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "14px", lineHeight: 1, padding: "0 5px", fontWeight: 600 },
  sidebarRefreshBtn: { background: "none", border: "none", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "13px", padding: "0 2px", lineHeight: 1 },
  content: { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  agentPanel: { flexShrink: 0, overflow: "hidden", height: "100%", display: "flex", flexDirection: "column" },

  // Sub-tab bar
  subTabBar: {
    display: "flex", alignItems: "center", borderBottom: "1px solid var(--havn-border)",
    padding: "0 12px", background: "var(--havn-bg-tertiary)", minHeight: "32px",
  },
  subTab: {
    padding: "6px 14px", background: "none", border: "none", borderBottom: "2px solid transparent",
    color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "12px", whiteSpace: "nowrap", fontWeight: 500,
  },
  subTabActive: {
    padding: "6px 14px", background: "none", border: "none", borderBottom: "2px solid var(--havn-accent)",
    color: "var(--havn-text)", cursor: "pointer", fontSize: "12px", whiteSpace: "nowrap", fontWeight: 600,
  },

  // File actions (inline in sub-tab bar)
  fileActions: { marginLeft: "auto", display: "flex", alignItems: "center", gap: "8px", paddingLeft: "16px" },
  fileName: { fontSize: "12px", color: "var(--havn-text-secondary)", fontFamily: "var(--havn-font-mono)" },
  modifiedDot: { color: "var(--havn-accent)", fontWeight: 700 },

  // Panel
  panel: { flex: 1, overflow: "hidden", minHeight: 0 },

  // Buttons
  btn: { padding: "5px 12px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontSize: "12px", fontWeight: 500 },
  btnPrimary: { padding: "5px 12px", background: "var(--havn-green)", border: "1px solid var(--havn-green-border)", borderRadius: "var(--havn-radius-lg)", color: "#fff", cursor: "pointer", fontSize: "12px", fontWeight: 500 },
};

const dcStyles = {
  overlay: { position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 },
  dialog: { background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "8px", padding: "20px", width: "420px", maxWidth: "90vw" },
  title: { fontSize: "14px", fontWeight: 600, color: "var(--havn-text)", marginBottom: "8px" },
  body: { fontSize: "13px", color: "var(--havn-text-secondary)", marginBottom: "16px", lineHeight: 1.5 },
  footer: { display: "flex", justifyContent: "flex-end", gap: "8px" },
  btnCancel: { padding: "6px 14px", background: "none", border: "1px solid var(--havn-border-light)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text-secondary)", cursor: "pointer", fontSize: "12px", fontWeight: 500 },
  btnSecondary: { padding: "6px 14px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-btn-border)", borderRadius: "var(--havn-radius-lg)", color: "var(--havn-text)", cursor: "pointer", fontSize: "12px", fontWeight: 500 },
  btnDanger: { padding: "6px 14px", background: "var(--havn-red, #c53030)", border: "1px solid var(--havn-red, #c53030)", borderRadius: "var(--havn-radius-lg)", color: "#fff", cursor: "pointer", fontSize: "12px", fontWeight: 500 },
};

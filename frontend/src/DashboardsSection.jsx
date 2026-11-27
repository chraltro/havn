import React, { useState, useEffect } from "react";
import DashboardListPanel from "./DashboardListPanel";
import DashboardCanvas from "./DashboardCanvas";
import WidgetEditor from "./WidgetEditor";
import DashboardFilterBar from "./DashboardFilterBar";
import { DashboardProvider, useDashboard } from "./DashboardContext";

/*
 * The Dashboards page (and the chrome-less embed view, #embed=true). A module
 * of its own so the dashboard code is fetched only when it is opened.
 */

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
    // A column, so the canvas gets the height left under the filter bar. As
    // siblings in a plain box the canvas took the full height *below* the
    // bar, and its last filter-bar's-worth of widgets was clipped off.
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
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
    </div>
  );
}

export default function DashboardsPage({ showConfirm, embedMode }) {
  return (
    <DashboardProvider>
      <DashboardsSection showConfirm={showConfirm} embedMode={embedMode} />
    </DashboardProvider>
  );
}

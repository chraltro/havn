import React, { useState } from "react";
import GitPanel from "./GitPanel";
import ReviewsPanel from "./ReviewsPanel";

/**
 * Develop -> Git tab wrapper that exposes two sub-tabs:
 *   - Status: the existing GitPanel (branch, changes, commits, branches, stash)
 *   - Reviews: the pull request system (ReviewsPanel)
 *
 * Matches the Observe -> Quality sub-tab pattern so users have one consistent
 * place for everything git/PR-related.
 */
export default function GitReviewsPanel({ showConfirm }) {
  const [sub, setSub] = useState("status");
  return (
    <div style={styles.container}>
      <div style={styles.tabBar}>
        {[
          { id: "status", label: "Status" },
          { id: "reviews", label: "Reviews" },
        ].map((t) => (
          <button
            key={t.id}
            type="button"
            style={{ ...styles.tab, ...(sub === t.id ? styles.tabActive : {}) }}
            onClick={() => setSub(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div style={styles.content}>
        {sub === "status" && <GitPanel />}
        {sub === "reviews" && <ReviewsPanel showConfirm={showConfirm} />}
      </div>
    </div>
  );
}

const styles = {
  container: {
    height: "100%",
    display: "flex",
    flexDirection: "column",
    background: "var(--havn-bg)",
    overflow: "hidden",
  },
  tabBar: {
    display: "flex",
    gap: 0,
    padding: "0 12px",
    borderBottom: "1px solid var(--havn-border)",
    flexShrink: 0,
    background: "var(--havn-bg)",
  },
  tab: {
    padding: "10px 20px",
    background: "none",
    border: "none",
    borderBottom: "2px solid transparent",
    color: "var(--havn-text-secondary)",
    cursor: "pointer",
    fontSize: 13,
    userSelect: "none",
  },
  tabActive: {
    color: "var(--havn-text)",
    borderBottom: "2px solid var(--havn-accent)",
  },
  content: {
    flex: 1,
    overflow: "hidden",
    display: "flex",
    flexDirection: "column",
  },
};

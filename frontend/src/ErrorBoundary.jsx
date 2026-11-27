import React from "react";
import { isChunkLoadError } from "./lazyLoad";
import { safeGetItem, safeSetItem } from "./safeStorage";

// When the last automatic reload for a missing chunk happened. Kept in
// localStorage, not sessionStorage: the app clears session state when the
// server restarts, which is exactly when chunks go missing.
const RELOAD_KEY = "havn_chunk_reload_at";
// A second missing chunk within this window means reloading did not help (the
// server really lacks the file), so the boundary asks instead of looping.
const RELOAD_WINDOW_MS = 60_000;

/**
 * Reload the page once for a chunk that could not be fetched. Returns false,
 * without reloading, when a reload for the same reason happened within the
 * last minute.
 */
export function reloadOnceForChunkError(reload = () => window.location.reload(), now = Date.now()) {
  const last = Number(safeGetItem(RELOAD_KEY)) || 0;
  if (now - last < RELOAD_WINDOW_MS) return false;
  safeSetItem(RELOAD_KEY, String(now));
  reload();
  return true;
}

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    if (isChunkLoadError(error)) {
      // A tab opened before a deploy asks for a hashed file the server no
      // longer has. A reload fetches the new index.html and its chunks; an
      // unsaved file still gets the browser's leave-page prompt first.
      console.warn(`[ErrorBoundary] ${this.props.name || "Unknown"}: could not load part of the app, reloading`, error);
      reloadOnceForChunkError(this.props.reload);
      return;
    }
    console.error(`[ErrorBoundary] ${this.props.name || "Unknown"}:`, error, info);
  }

  render() {
    if (this.state.hasError && isChunkLoadError(this.state.error)) {
      const reload = this.props.reload || (() => window.location.reload());
      return (
        <div style={this.props.fullScreen ? styles.fullScreen : styles.container} role="alert">
          <div style={styles.title}>havn has been updated</div>
          <div style={styles.hint}>
            This part of the app could not be loaded, most likely because a newer version is now
            running. Reload the page to continue.
          </div>
          <button style={styles.button} onClick={() => reload()}>
            Reload
          </button>
        </div>
      );
    }
    if (this.state.hasError) {
      return (
        <div style={this.props.fullScreen ? styles.fullScreen : styles.container}>
          <div style={styles.icon}>!</div>
          <div style={styles.title}>
            {this.props.name ? `${this.props.name} crashed` : "Something went wrong"}
          </div>
          <div style={styles.message}>{this.state.error?.message || "An unexpected error occurred."}</div>
          <button
            style={styles.button}
            onClick={() => this.setState({ hasError: false, error: null })}
          >
            Try Again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const styles = {
  container: {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    height: "100%",
    padding: "24px",
    color: "var(--havn-text-secondary)",
    fontFamily: "var(--havn-font)",
    textAlign: "center",
    gap: "12px",
  },
  icon: {
    width: "36px",
    height: "36px",
    borderRadius: "50%",
    background: "var(--havn-red, #e53e3e)",
    color: "#fff",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: "18px",
    fontWeight: "bold",
  },
  title: {
    fontSize: "14px",
    fontWeight: 600,
    color: "var(--havn-text)",
  },
  hint: {
    fontSize: "12px",
    color: "var(--havn-text-secondary)",
    maxWidth: "400px",
    lineHeight: 1.5,
  },
  message: {
    fontSize: "12px",
    color: "var(--havn-text-dim)",
    maxWidth: "400px",
    fontFamily: "var(--havn-font-mono)",
    wordBreak: "break-word",
  },
  button: {
    marginTop: "8px",
    padding: "6px 16px",
    background: "var(--havn-btn-bg)",
    border: "1px solid var(--havn-btn-border)",
    borderRadius: "var(--havn-radius-lg)",
    color: "var(--havn-text)",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 500,
  },
};

// At the root (main.jsx) there is no sized panel around the boundary.
styles.fullScreen = { ...styles.container, height: "100vh", background: "var(--havn-bg)" };

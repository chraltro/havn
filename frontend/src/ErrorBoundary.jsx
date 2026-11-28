import React from "react";
import { isChunkLoadError, isNewDeploy } from "./lazyLoad";
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
 * last minute, or when the guard could not be stored: with storage blocked
 * (a cross-site embed, some private windows) nothing would remember the
 * reload, and the page could reload forever.
 */
export function reloadOnceForChunkError(reload = () => window.location.reload(), now = Date.now()) {
  const last = Number(safeGetItem(RELOAD_KEY)) || 0;
  if (now - last < RELOAD_WINDOW_MS) return false;
  if (!safeSetItem(RELOAD_KEY, String(now)) || safeGetItem(RELOAD_KEY) !== String(now)) return false;
  reload();
  return true;
}

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    // chunk: null, or what a chunk failure turned out to be: "checking",
    // "updated" (a new deploy; reload) or "unreachable" (retry in place).
    this.state = { hasError: false, error: null, chunk: null };
    this.retry = this.retry.bind(this);
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error, chunk: isChunkLoadError(error) ? "checking" : null };
  }

  componentDidMount() {
    this.mounted = true;
  }

  componentWillUnmount() {
    this.mounted = false;
  }

  componentDidCatch(error, info) {
    if (isChunkLoadError(error)) {
      // The import was already retried (lazyWithPreload). Reload only for a
      // real new deploy, where a tab opened earlier asks for hashed files the
      // server no longer has; a server that is restarting or a network blip
      // gets a Retry instead, which keeps everything on screen.
      console.warn(`[ErrorBoundary] ${this.props.name || "Unknown"}: could not load part of the app`, error);
      const check = this.props.checkDeploy || (() => isNewDeploy());
      Promise.resolve()
        .then(check)
        .catch(() => false)
        .then((updated) => {
          if (!this.mounted || this.state.error !== error) return;
          if (updated) reloadOnceForChunkError(this.props.reload);
          this.setState({ chunk: updated ? "updated" : "unreachable" });
        });
      return;
    }
    console.error(`[ErrorBoundary] ${this.props.name || "Unknown"}:`, error, info);
  }

  /** Try the failed part again without reloading the page. */
  retry() {
    this.setState({ hasError: false, error: null, chunk: null });
  }

  render() {
    if (this.state.hasError && this.state.chunk) {
      const box = this.props.fullScreen ? styles.fullScreen : styles.container;
      if (this.state.chunk === "checking") {
        return <div style={box} role="status" aria-busy="true" />;
      }
      if (this.state.chunk === "updated") {
        const reload = this.props.reload || (() => window.location.reload());
        return (
          <div style={box} role="alert">
            <div style={styles.title}>havn has been updated</div>
            <div style={styles.hint}>
              A newer version of havn is running and this part of the app belongs to it. Reload the
              page to continue.
            </div>
            <button style={styles.button} onClick={() => reload()}>
              Reload
            </button>
          </div>
        );
      }
      return (
        <div style={box} role="alert">
          <div style={styles.title}>Could not load this part of havn</div>
          <div style={styles.hint}>
            The server did not answer. It may be restarting, or the network dropped out. Nothing
            on screen has been lost.
          </div>
          <button style={styles.button} onClick={this.retry}>
            Retry
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

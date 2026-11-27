import React, { lazy, useEffect, useState } from "react";

/*
 * Code splitting helpers. Each page of the app is its own chunk, fetched the
 * first time it is shown. App.jsx preloads a page's chunk when the pointer or
 * focus reaches its nav item, and the default page of every section when the
 * browser goes idle after the first paint, so switching pages on a normal
 * connection does not show the loading state at all.
 */

/**
 * React.lazy with a `preload()` that starts the download without rendering.
 *
 * The import promise is shared between preload() and the render, so a page
 * preloaded on hover is not fetched twice. A failed preload is forgotten, so
 * the render tries again; a failed render goes to the nearest ErrorBoundary,
 * which treats a chunk that cannot be fetched (a new deploy removed it) as a
 * reason to reload the page (see isChunkLoadError).
 */
export function lazyWithPreload(factory) {
  let promise = null;
  const load = () => {
    if (!promise) {
      promise = factory().catch((err) => {
        promise = null;
        throw err;
      });
    }
    return promise;
  };
  const Component = lazy(load);
  Component.preload = () => load().catch(() => {});
  return Component;
}

/**
 * Start downloading every given lazy component; errors are left to the
 * render. The promise settles (never rejects) once all have.
 */
export function preloadAll(components) {
  return Promise.all((components || []).map((c) => c?.preload?.()));
}

/**
 * Run `fn` once the browser is idle after the first paint, or after a short
 * timeout where requestIdleCallback is missing (Safari). Returns a cancel.
 */
export function whenIdle(fn, timeout = 2000) {
  if (typeof window === "undefined") return () => {};
  if (typeof window.requestIdleCallback === "function") {
    const id = window.requestIdleCallback(fn, { timeout });
    return () => window.cancelIdleCallback?.(id);
  }
  const id = window.setTimeout(fn, timeout);
  return () => window.clearTimeout(id);
}

// Messages browsers (and Vite's preload helper) use when a dynamic import
// cannot fetch its module: Chromium, Firefox, Safari, Vite in that order.
const CHUNK_ERROR_PATTERNS = [
  /Failed to fetch dynamically imported module/i,
  /error loading dynamically imported module/i,
  /Importing a module script failed/i,
  /Unable to preload CSS/i,
  /Loading (CSS )?chunk [\w-]+ failed/i,
];

/** True when `error` says a code-split chunk could not be fetched. */
export function isChunkLoadError(error) {
  if (!error) return false;
  if (error.name === "ChunkLoadError") return true;
  const message = String(error.message || error);
  return CHUNK_ERROR_PATTERNS.some((re) => re.test(message));
}

/**
 * Loading state for a page whose chunk is still downloading. It fills the
 * panel the page will fill, on the theme's background, so nothing around it
 * moves; the text appears only after a short delay, so a fast load shows
 * nothing at all rather than a flash of "Loading...".
 */
export function PanelFallback({ label = "Loading…", delay = 200, fullScreen = false }) {
  const [visible, setVisible] = useState(delay <= 0);
  useEffect(() => {
    if (delay <= 0) return undefined;
    const id = setTimeout(() => setVisible(true), delay);
    return () => clearTimeout(id);
  }, [delay]);
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      data-havn-loading=""
      style={fullScreen ? { ...fallbackStyle, height: "100vh" } : fallbackStyle}
    >
      {visible ? label : null}
    </div>
  );
}

const fallbackStyle = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  height: "100%",
  background: "var(--havn-bg)",
  color: "var(--havn-text-secondary)",
  fontFamily: "var(--havn-font)",
  fontSize: "13px",
};

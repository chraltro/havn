import React, { lazy, useEffect, useState } from "react";

/*
 * Code splitting helpers. Each page of the app is its own chunk, fetched the
 * first time it is shown. App.jsx preloads a page's chunk when the pointer or
 * focus reaches its nav item, and the default page of every section when the
 * browser goes idle after the first paint, so switching pages on a normal
 * connection does not show the loading state at all.
 */

// Pauses before the second and third try of a chunk that could not be fetched.
const DEFAULT_RETRY_DELAYS = [500, 1500];

/**
 * React.lazy with a `preload()` that starts the download without rendering.
 *
 * The import promise is shared between preload() and the render, so a page
 * preloaded on hover is not fetched twice. A chunk that cannot be fetched is
 * tried again after a short pause (`retryDelays`), since a server restarting
 * or a network blip is far more common than a deploy. A load that still
 * fails is forgotten, so the next preload or mount tries again; a failed
 * render goes to the nearest ErrorBoundary (see isChunkLoadError).
 */
export function lazyWithPreload(factory, { retryDelays = DEFAULT_RETRY_DELAYS } = {}) {
  let promise = null;
  let loaded = null;
  const attempt = async () => {
    for (let i = 0; ; i++) {
      try {
        return await factory();
      } catch (err) {
        if (i >= retryDelays.length || !isChunkLoadError(err)) throw err;
        await new Promise((r) => setTimeout(r, retryDelays[i]));
      }
    }
  };
  const load = () => {
    if (!promise) {
      promise = attempt().then(
        (mod) => {
          loaded = mod;
          return mod;
        },
        (err) => {
          promise = null;
          // React.lazy keeps a rejected load for good, so a later mount (the
          // ErrorBoundary's Retry) needs a fresh one to fetch again.
          Lazy = lazy(load);
          throw err;
        },
      );
    }
    return promise;
  };
  let Lazy = lazy(load);
  // A chunk that has already arrived is rendered directly. Through
  // React.lazy it would still suspend once (React has to await the promise
  // itself), and React holds a fallback it has shown for at least 300 ms, so
  // a preloaded page would flash the loading state anyway. The choice is made
  // once per mount, so a mounted page never changes component type.
  function LazyPage(props) {
    const [Impl] = useState(() => (loaded ? loaded.default : Lazy));
    return <Impl {...props} />;
  }
  LazyPage.preload = () => load().catch(() => {});
  return LazyPage;
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

const MODULE_SCRIPT_RE = /<script\b[^>]*\btype=["']module["'][^>]*>/gi;
const SRC_RE = /\bsrc=["']([^"']+)["']/i;

/** Path of the entry module script an index.html loads, or null. */
export function entryScriptFromHtml(html, base = "http://localhost/") {
  for (const tag of String(html || "").match(MODULE_SCRIPT_RE) || []) {
    const src = tag.match(SRC_RE);
    if (src) {
      try {
        return new URL(src[1], base).pathname;
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** Path of the entry module script the running page was started from, or null. */
export function runningEntryScript(doc = typeof document !== "undefined" ? document : null) {
  const el = doc?.querySelector?.('script[type="module"][src]');
  if (!el) return null;
  try {
    return new URL(el.getAttribute("src"), doc.baseURI).pathname;
  } catch {
    return null;
  }
}

/**
 * Whether a chunk failed because a new version of havn is being served.
 *
 * Fetches index.html bypassing the cache. Only when the server answers and
 * that page starts from a different entry script than the one running is it
 * a new deploy, where a reload helps. A server that is down or restarting, or
 * a network that dropped out, answers false: reloading then lands on the
 * browser's own error page and throws away whatever was on screen.
 */
export async function isNewDeploy({
  fetchImpl = typeof fetch === "function" ? fetch : null,
  running = runningEntryScript(),
} = {}) {
  if (!fetchImpl || !running) return false;
  try {
    const res = await fetchImpl("/", { cache: "no-store", headers: { Accept: "text/html" } });
    if (!res?.ok) return false;
    const fresh = entryScriptFromHtml(await res.text());
    return Boolean(fresh) && fresh !== running;
  } catch {
    return false;
  }
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

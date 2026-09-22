import { useEffect, useRef } from "react";

/**
 * Files on disk changed behind the editor's back.
 *
 * A column rename is the one edit the UI does not make itself: the API
 * rewrites every model that reads the column, so the file tree and anything
 * derived from the model list are stale the moment it returns. Whoever wrote
 * the files says so with this event; the app listens and refreshes.
 */
export const FILES_CHANGED_EVENT = "havn-files-changed";

/** Announce that `paths` were written by something other than the editor. */
export function notifyFilesChanged(paths) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(FILES_CHANGED_EVENT, { detail: { paths: paths || [] } }));
}

/**
 * Run `handler(paths)` whenever files change on disk.
 *
 * The handler is read from a ref, so a caller can pass an inline function
 * without re-registering the listener on every render.
 */
export function useFilesChanged(handler) {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  useEffect(() => {
    const listener = (event) => {
      const fn = handlerRef.current;
      if (fn) fn((event && event.detail && event.detail.paths) || []);
    };
    window.addEventListener(FILES_CHANGED_EVENT, listener);
    return () => window.removeEventListener(FILES_CHANGED_EVENT, listener);
  }, []);
}

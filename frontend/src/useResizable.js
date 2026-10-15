import { useState, useRef, useCallback } from "react";

export default function useResizable(storageKey, defaultSize, minSize, maxSize) {
  const [size, setSize] = useState(() => {
    let stored = null;
    try { stored = localStorage.getItem(storageKey); } catch { /* storage unavailable */ }
    if (stored) {
      const parsed = parseInt(stored, 10);
      if (!isNaN(parsed)) return Math.max(minSize, Math.min(maxSize, parsed));
    }
    return defaultSize;
  });

  const startSizeRef = useRef(size);

  // `visibleSize` lets a caller that clamps the size to its container start
  // the drag from what is on screen. Starting from a larger stored size made
  // the first part of a drag do nothing visible.
  const onResizeStart = useCallback((visibleSize) => {
    startSizeRef.current = typeof visibleSize === "number" ? visibleSize : size;
  }, [size]);

  const onResize = useCallback((delta) => {
    const newSize = Math.max(minSize, Math.min(maxSize, startSizeRef.current + delta));
    setSize(newSize);
    try { localStorage.setItem(storageKey, String(Math.round(newSize))); } catch { /* storage unavailable */ }
  }, [storageKey, minSize, maxSize]);

  // Back to the default, e.g. on a double-click of the handle.
  const reset = useCallback(() => {
    setSize(defaultSize);
    try { localStorage.removeItem(storageKey); } catch { /* storage unavailable */ }
  }, [storageKey, defaultSize]);

  return [size, onResize, onResizeStart, reset];
}

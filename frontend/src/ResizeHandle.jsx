import React, { useState, useCallback, useRef } from "react";

// Keyboard step for the arrow keys; Shift moves further.
const KEY_STEP = 20;
const KEY_STEP_LARGE = 80;

export default function ResizeHandle({ direction, onResize, onResizeStart, onDoubleClick, label }) {
  const [dragging, setDragging] = useState(false);
  const [hovered, setHovered] = useState(false);
  const startPos = useRef(0);
  const overlayRef = useRef(null);

  const isHorizontal = direction === "horizontal";

  // Arrow keys resize, so splits are not mouse-only. Each press is a fresh
  // resize from the current size: start, then one delta.
  const handleKeyDown = useCallback((e) => {
    const grow = isHorizontal ? "ArrowRight" : "ArrowDown";
    const shrink = isHorizontal ? "ArrowLeft" : "ArrowUp";
    if (e.key !== grow && e.key !== shrink) return;
    e.preventDefault();
    const step = e.shiftKey ? KEY_STEP_LARGE : KEY_STEP;
    if (onResizeStart) onResizeStart();
    onResize(e.key === grow ? step : -step);
  }, [isHorizontal, onResize, onResizeStart]);

  const handleMouseDown = useCallback((e) => {
    e.preventDefault();
    startPos.current = isHorizontal ? e.clientX : e.clientY;
    setDragging(true);
    if (onResizeStart) onResizeStart();

    // Prevent Monaco and iframes from swallowing events
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;z-index:9998;cursor:" +
      (isHorizontal ? "col-resize" : "row-resize");
    document.body.appendChild(overlay);
    overlayRef.current = overlay;

    document.body.style.cursor = isHorizontal ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";

    function handleMouseMove(e) {
      const current = isHorizontal ? e.clientX : e.clientY;
      const delta = current - startPos.current;
      onResize(delta);
    }

    function handleMouseUp() {
      setDragging(false);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (overlayRef.current) {
        overlayRef.current.remove();
        overlayRef.current = null;
      }
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    }

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
  }, [isHorizontal, onResize, onResizeStart]);

  const active = dragging || hovered;

  const containerStyle = isHorizontal ? {
    width: "6px",
    cursor: "col-resize",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    position: "relative",
  } : {
    height: "6px",
    cursor: "row-resize",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    flexShrink: 0,
    position: "relative",
  };

  const lineStyle = isHorizontal ? {
    width: "1px",
    height: "24px",
    borderRadius: "1px",
    background: active ? "var(--havn-accent)" : "var(--havn-border-light)",
    opacity: dragging ? 1 : (hovered ? 0.6 : 0.4),
    transition: "background 0.15s ease, opacity 0.15s ease",
  } : {
    height: "1px",
    width: "24px",
    borderRadius: "1px",
    background: active ? "var(--havn-accent)" : "var(--havn-border-light)",
    opacity: dragging ? 1 : (hovered ? 0.6 : 0.4),
    transition: "background 0.15s ease, opacity 0.15s ease",
  };

  return (
    <div
      data-havn-resize-handle=""
      role="separator"
      // A handle that resizes horizontally is a vertical bar, and vice versa.
      aria-orientation={isHorizontal ? "vertical" : "horizontal"}
      aria-label={label || (isHorizontal ? "Resize panel width" : "Resize panel height")}
      tabIndex={0}
      title={onDoubleClick ? "Drag to resize, double-click to reset" : "Drag to resize"}
      style={containerStyle}
      onMouseDown={handleMouseDown}
      onKeyDown={handleKeyDown}
      onDoubleClick={onDoubleClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div style={lineStyle} />
    </div>
  );
}

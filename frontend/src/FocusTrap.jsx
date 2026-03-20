import React, { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Traps keyboard focus within a container (for modal dialogs).
 *
 * - On mount: focuses the first focusable element inside.
 * - Tab / Shift+Tab cycle within the container.
 * - On unmount: restores focus to the previously focused element.
 * - Adds role="dialog" and aria-modal="true" to the container.
 *
 * Props:
 *   children   — dialog content
 *   labelledBy — id of the title element (for aria-labelledby)
 *   style      — passed through to the wrapper div
 *   className  — passed through to the wrapper div
 *   onClick    — passed through to the wrapper div
 */
export default function FocusTrap({ children, labelledBy, style, className, onClick }) {
  const containerRef = useRef(null);
  const previousFocusRef = useRef(null);

  useEffect(() => {
    // Remember what was focused before the trap opened
    previousFocusRef.current = document.activeElement;

    // Focus the first focusable element inside the trap
    const container = containerRef.current;
    if (container) {
      const first = container.querySelector(FOCUSABLE_SELECTOR);
      if (first) {
        // Small delay to ensure the DOM is fully rendered
        requestAnimationFrame(() => first.focus());
      }
    }

    return () => {
      // Restore focus on unmount
      if (previousFocusRef.current && typeof previousFocusRef.current.focus === "function") {
        previousFocusRef.current.focus();
      }
    };
  }, []);

  function handleKeyDown(e) {
    if (e.key !== "Tab") return;

    const container = containerRef.current;
    if (!container) return;

    const focusable = Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR));
    if (focusable.length === 0) {
      e.preventDefault();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (e.shiftKey) {
      // Shift+Tab: if on first element, wrap to last
      if (document.activeElement === first) {
        e.preventDefault();
        last.focus();
      }
    } else {
      // Tab: if on last element, wrap to first
      if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
      onKeyDown={handleKeyDown}
      style={style}
      className={className}
      onClick={onClick}
    >
      {children}
    </div>
  );
}

// Shared keyboard-access helpers.
//
// Before this module the whole app had exactly one tabIndex (a local copy of
// `clickable` in DataSourcesPanel), so every clickable row, card, tab and
// expander that was not a real <button> could only be used with a mouse.
import { useEffect } from "react";

// Make a non-button element (a row, a card, a div tab) behave like a button
// for keyboard and assistive-tech users. Spread it alongside onClick:
//
//   <tr onClick={open} {...clickable(open, `Open ${name}`)}>
//
// Enter or Space activates it. Keys that bubble up from a control inside the
// element (a nested button, input or checkbox) are left alone, so typing a
// space in a row's input does not also open the row.
export function clickable(onActivate, label) {
  return {
    role: "button",
    tabIndex: 0,
    "aria-label": label,
    onKeyDown: (e) => {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onActivate(e);
      }
    },
  };
}

// Close a popover, menu or dropdown on Escape and on a mousedown outside it.
// `refs` are the elements that count as "inside" (the popover and, usually,
// the button that opened it, so clicking the trigger toggles instead of
// closing and immediately reopening). Pass `returnFocusRef` to put focus back
// on the trigger after an Escape, so a keyboard user is not dropped on <body>.
export function useDismiss(open, onClose, refs = [], returnFocusRef = null) {
  useEffect(() => {
    if (!open) return undefined;
    function onKey(e) {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
      returnFocusRef?.current?.focus?.();
    }
    function onPointer(e) {
      const inside = refs.some((r) => r?.current && r.current.contains(e.target));
      if (!inside) onClose();
    }
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("mousedown", onPointer);
    };
    // refs are stable ref objects; onClose identity changes are harmless here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
}

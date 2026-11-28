/*
 * Unsaved notebooks, by name. The panel unmounts on every tab switch and
 * whenever another file is opened, and the edits lived only in its state:
 * a trip to the Query tab and back lost them. A draft kept here outlives the
 * panel (not a page reload, which App warns about instead).
 *
 * A module of its own so App's beforeunload check can ask about drafts
 * without loading NotebookPanel, which is fetched on demand.
 */
export const drafts = new Map();

export function hasUnsavedNotebooks() {
  return drafts.size > 0;
}

/*
 * Small editor helpers the app shell needs before the editor itself has
 * loaded. Kept out of Editor.jsx so App.jsx can use them without pulling the
 * editor (and its Monaco wiring) into the first download. Editor.jsx
 * re-exports both, so existing imports keep working.
 */

/** Legacy SQL-comment directive forms, kept in step with the backend's _META_PREFIXES. */
const LEGACY_DIRECTIVE_PREFIXES = [
  "-- config:", "-- depends_on:", "-- description:", "-- col:", "-- assert:",
];

/**
 * Blank out every directive line in a model, keeping the line count.
 *
 * Directives can sit anywhere in the file, not just at the top: an @assert
 * after the SELECT is normal and DuckDB would choke on it. Blanking rather
 * than deleting keeps the remaining line numbers matching the file, so an
 * error from a preview still points at the right line in the editor.
 */
export function stripModelDirectives(text) {
  return (text || "")
    .split("\n")
    .map((line) => {
      const s = line.trim();
      const isDirective = s.startsWith("@") || LEGACY_DIRECTIVE_PREFIXES.some((p) => s.startsWith(p));
      return isDirective ? "" : line;
    })
    .join("\n");
}

/** Short label for the editor toolbar: "binding...", "3 errors", "ok". */
export function bindStatusLabel(state) {
  if (!state) return null;
  if (state.running) return "binding…";
  if (state.errorCount > 0) return `${state.errorCount} error${state.errorCount === 1 ? "" : "s"}`;
  if (state.warningCount > 0) return `${state.warningCount} warning${state.warningCount === 1 ? "" : "s"}`;
  return "ok";
}

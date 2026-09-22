import React, { useEffect, useRef, useState } from "react";
import MonacoEditor, { loader } from "@monaco-editor/react";
import { useTheme } from "./ThemeProvider";
import { COLOR_THEMES } from "./themes";
import { api, getMacros } from "./api";
import { packageOfPath } from "./FileTree";
import { notifyFilesChanged } from "./filesChanged";

// ---------------------------------------------------------------------------
// Custom Monaco themes derived from havn COLOR_THEMES
// ---------------------------------------------------------------------------

/** Normalize a CSS hex color to 6-digit uppercase hex (strips shorthand). */
function normHex(hex) {
  let h = hex.replace("#", "");
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  return "#" + h.toUpperCase();
}

/** Append an alpha byte (0-255) to a 6-digit hex color → #RRGGBBAA. */
function hexAlpha(hex, alpha) {
  const a = Math.round(alpha * 255).toString(16).padStart(2, "0").toUpperCase();
  return normHex(hex) + a;
}

/** Build a Monaco IStandaloneThemeData from a havn color theme entry. */
function buildMonacoTheme(themeEntry) {
  const v = themeEntry.vars;
  const dark = themeEntry.dark;
  return {
    base: dark ? "vs-dark" : "vs",
    inherit: true,
    rules: [
      // Keywords (SELECT, FROM, CREATE, etc.)
      { token: "keyword", foreground: normHex(v["--havn-accent"]).slice(1), fontStyle: "bold" },
      { token: "keyword.sql", foreground: normHex(v["--havn-accent"]).slice(1), fontStyle: "bold" },
      // Operators
      { token: "operator", foreground: normHex(v["--havn-text-secondary"]).slice(1) },
      { token: "operator.sql", foreground: normHex(v["--havn-text-secondary"]).slice(1) },
      // Strings
      { token: "string", foreground: normHex(v["--havn-green"]).slice(1) },
      { token: "string.sql", foreground: normHex(v["--havn-green"]).slice(1) },
      // Numbers
      { token: "number", foreground: normHex(v["--havn-purple"]).slice(1) },
      { token: "number.sql", foreground: normHex(v["--havn-purple"]).slice(1) },
      // Comments
      { token: "comment", foreground: normHex(v["--havn-text-dim"]).slice(1), fontStyle: "italic" },
      { token: "comment.sql", foreground: normHex(v["--havn-text-dim"]).slice(1), fontStyle: "italic" },
      // Identifiers / default
      { token: "identifier", foreground: normHex(v["--havn-text"]).slice(1) },
      { token: "identifier.sql", foreground: normHex(v["--havn-text"]).slice(1) },
      // Types
      { token: "type", foreground: normHex(v["--havn-yellow"]).slice(1) },
      { token: "predefined.sql", foreground: normHex(v["--havn-yellow"]).slice(1) },
      // Functions
      { token: "predefined", foreground: normHex(v["--havn-yellow"]).slice(1) },
      // Delimiters (parentheses, commas, semicolons)
      { token: "delimiter", foreground: normHex(v["--havn-text-secondary"]).slice(1) },
      { token: "delimiter.parenthesis", foreground: normHex(v["--havn-text-secondary"]).slice(1) },
      // Python-specific tokens
      { token: "keyword.python", foreground: normHex(v["--havn-accent"]).slice(1), fontStyle: "bold" },
      { token: "string.python", foreground: normHex(v["--havn-green"]).slice(1) },
      { token: "comment.python", foreground: normHex(v["--havn-text-dim"]).slice(1), fontStyle: "italic" },
      { token: "number.python", foreground: normHex(v["--havn-purple"]).slice(1) },
      { token: "identifier.python", foreground: normHex(v["--havn-text"]).slice(1) },
      { token: "delimiter.python", foreground: normHex(v["--havn-text-secondary"]).slice(1) },
      { token: "type.identifier.python", foreground: normHex(v["--havn-yellow"]).slice(1) },
      // YAML tokens
      { token: "type.yaml", foreground: normHex(v["--havn-accent"]).slice(1) },
      { token: "string.yaml", foreground: normHex(v["--havn-green"]).slice(1) },
      { token: "number.yaml", foreground: normHex(v["--havn-purple"]).slice(1) },
      { token: "comment.yaml", foreground: normHex(v["--havn-text-dim"]).slice(1), fontStyle: "italic" },
    ],
    colors: {
      "editor.background": normHex(v["--havn-bg-tertiary"]),
      "editor.foreground": normHex(v["--havn-text"]),
      "editorLineNumber.foreground": normHex(v["--havn-text-dim"]),
      "editorLineNumber.activeForeground": normHex(v["--havn-text-secondary"]),
      "editorCursor.foreground": normHex(v["--havn-accent"]),
      "editor.selectionBackground": hexAlpha(v["--havn-accent"], 0.2),
      "editor.inactiveSelectionBackground": hexAlpha(v["--havn-accent"], 0.1),
      "editor.lineHighlightBackground": normHex(v["--havn-bg-secondary"]),
      "editor.lineHighlightBorder": "#00000000",
      "editorIndentGuide.background": normHex(v["--havn-border"]),
      "editorIndentGuide.activeBackground": normHex(v["--havn-border-light"]),
      "editorBracketMatch.background": hexAlpha(v["--havn-accent"], 0.15),
      "editorBracketMatch.border": hexAlpha(v["--havn-accent"], 0.4),
      "editorWidget.background": normHex(v["--havn-bg-secondary"]),
      "editorWidget.border": normHex(v["--havn-border"]),
      "editorSuggestWidget.background": normHex(v["--havn-bg-secondary"]),
      "editorSuggestWidget.border": normHex(v["--havn-border"]),
      "editorSuggestWidget.selectedBackground": normHex(v["--havn-btn-bg"]),
      "editorSuggestWidget.highlightForeground": normHex(v["--havn-accent"]),
      "editorHoverWidget.background": normHex(v["--havn-bg-secondary"]),
      "editorHoverWidget.border": normHex(v["--havn-border"]),
      "scrollbarSlider.background": hexAlpha(v["--havn-text-dim"], 0.2),
      "scrollbarSlider.hoverBackground": hexAlpha(v["--havn-text-dim"], 0.35),
      "scrollbarSlider.activeBackground": hexAlpha(v["--havn-text-dim"], 0.5),
    },
  };
}

/** Register all havn themes with a Monaco instance. */
function defineHavnThemes(monaco) {
  for (const [id, entry] of Object.entries(COLOR_THEMES)) {
    monaco.editor.defineTheme(`havn-${id}`, buildMonacoTheme(entry));
  }
}

// ---------------------------------------------------------------------------
// Editor context shared with the language providers
//
// Completion, hover, definition and code lens providers are registered once
// against the `sql` language when Monaco loads, so they cannot close over React
// state. This module-level object is the bridge: the Editor component keeps it
// in sync with the active file, and the providers read it.
// ---------------------------------------------------------------------------

export const editorContext = {
  /** Project-relative path of the file in the editor, e.g. "transform/silver/customers.sql". */
  activeFile: null,
  /**
   * Whether the buffer on screen has unsaved edits.
   *
   * The rename plan is computed against the file on disk, so its offsets mean
   * nothing once the buffer has moved. The providers refuse rather than splice
   * a disk offset into text that is not on disk.
   */
  dirty: false,
  /**
   * Put a file's content back into the buffer, unmodified: (path, text) => void.
   * Set by the component, used after a rename the API already wrote.
   */
  reloadFile: null,
  /** Latest bind result, keyed by project-relative path. Feeds hover types. */
  bindResults: new Map(),
  /** App navigation hook: (path, line, col) => void. Set by the component. */
  openModel: null,
  /** App preview hook: (sql, label) => void. Set by the component. */
  previewSql: null,
  /**
   * Ask the user whether to go ahead despite blockers: (blocked) => Promise<bool>.
   * Left null the rename refuses, which is the safe default: a blocker means
   * the index could not see the whole picture.
   */
  confirmBlockers: null,
  /**
   * Ask the user to confirm a rename that reaches outside the model on
   * screen: ({ model, column, files }) => Promise<bool>. Left null the rename
   * refuses, for the same reason as `confirmBlockers`.
   */
  confirmTarget: null,
  /** Show a list of column reference sites: (result) => void. */
  showReferences: null,
};

/** Monaco model URI for a project file, so every open file gets its own model. */
export function modelUriFor(path) {
  if (!path) return undefined;
  return `file:///${String(path).replace(/^\/+/, "")}`;
}

/** Inverse of `modelUriFor`: project-relative path from a Monaco model URI. */
export function pathFromUri(uri) {
  if (!uri) return null;
  const p = String(uri.path || "");
  return p.startsWith("/") ? p.slice(1) : p;
}

/**
 * The project file a URI names, or null when it names something else.
 *
 * Monaco asks the opener about every URI it wants on screen, including its
 * own `inmemory://model/17` buffers behind a diff or a peek widget. Those are
 * Monaco's to open; claiming them navigated the app to a file that does not
 * exist. Only a `file:` URI pointing inside the project is the app's.
 */
export function projectPathFromUri(uri) {
  if (!uri || String(uri.scheme || "") !== "file") return null;
  const path = pathFromUri(uri);
  if (!path || path.startsWith("/") || path.split("/").includes("..")) return null;
  return path;
}

/**
 * How many file models stay alive at once.
 *
 * @monaco-editor/react creates a model per `path` and only disposes it when
 * the component unmounts, so a session that opens fifty files keeps fifty
 * buffers in memory. Keeping the recent ones means switching back and forth
 * between a handful of files never pays for a reload.
 */
export const MAX_OPEN_MODELS = 8;

/**
 * Dispose the models of files that fell out of the recent list.
 *
 * `recent` is most-recently-opened first; everything past `keep` goes. The
 * file on screen is the head of the list, so it is never a candidate, and
 * each model's markers are cleared before it goes to keep the marker owners
 * from outliving it.
 */
export function pruneOpenModels(monaco, recent, keep = MAX_OPEN_MODELS) {
  if (!monaco) return [];
  const disposed = [];
  for (const path of (recent || []).slice(keep)) {
    let model = null;
    try {
      model = monaco.editor.getModel(monaco.Uri.parse(modelUriFor(path)));
    } catch {
      model = null;
    }
    if (!model || (model.isDisposed && model.isDisposed())) continue;
    try {
      monaco.editor.setModelMarkers(model, BIND_MARKER_OWNER, []);
      monaco.editor.setModelMarkers(model, LINT_MARKER_OWNER, []);
      model.dispose();
      disposed.push(path);
    } catch {
      // A model Monaco already let go of is nothing to worry about.
    }
  }
  return disposed;
}

/** True for files the SQL model features (bind, lint, definition) apply to. */
export function isTransformSql(path) {
  return !!path && path.endsWith(".sql") && path.replace(/\\/g, "/").startsWith("transform/");
}

// Cache for table schema lookups to avoid repeated API calls.
// Entries are { info, time }; `info` is null for a known miss (negative cache).
const COLUMNS_TTL_MS = 60_000;
const schemaCache = new Map();
// Cached table list for completions (populated on first completion request)
let tablesCache = null;
let tablesCacheTime = 0;
// Cached /api/models, for go-to-definition
let modelsCache = null;
let modelsCacheTime = 0;
const MODELS_TTL_MS = 60_000;

/**
 * Drop every cached schema. Called when a transform run completes: a rebuilt
 * model can add, drop or retype columns, and a stale cache would keep offering
 * the old ones for the rest of the session.
 */
export function invalidateSchemaCaches() {
  schemaCache.clear();
  tablesCache = null;
  tablesCacheTime = 0;
  modelsCache = null;
  modelsCacheTime = 0;
}

function readColumns(key) {
  const hit = schemaCache.get(key);
  if (!hit) return undefined;
  if (Date.now() - hit.time > COLUMNS_TTL_MS) {
    schemaCache.delete(key);
    return undefined;
  }
  return hit.info;
}

function writeColumns(key, info) {
  schemaCache.set(key, { info, time: Date.now() });
}

/** Split "schema.name" -- or "db.schema.name" -- at the last dot. */
export function splitRelationKey(key) {
  const text = String(key || "");
  const dot = text.lastIndexOf(".");
  if (dot < 0) return { schema: "", name: text };
  return { schema: text.slice(0, dot), name: text.slice(dot + 1) };
}

/**
 * Seed the column cache from a bind result, without overwriting a better one.
 *
 * The binder usually saw the upstream relations moments ago, but it can also
 * fall back to `_havn.model_columns`, which is only as new as the last build.
 * A live DESCRIBE that knows at least as many columns is therefore kept: it
 * is the one that has the column somebody just added, and replacing it would
 * stop completions offering a column that really exists. Entries that stay
 * keep their original TTL.
 */
export function seedColumnsFromBind(bind) {
  if (!bind || !bind.upstream) return;
  for (const [key, columns] of Object.entries(bind.upstream)) {
    if (!Array.isArray(columns)) continue;
    const cached = readColumns(key);
    // undefined is a miss or an expired entry; null is a known miss, which
    // the binder's answer beats.
    if (cached && Array.isArray(cached.columns) && cached.columns.length >= columns.length) continue;
    const { schema, name } = splitRelationKey(key);
    writeColumns(key, { schema, name, columns });
  }
}

async function getTablesCache() {
  const now = Date.now();
  if (tablesCache && now - tablesCacheTime < 30_000) return tablesCache;
  try {
    tablesCache = await api.listTables();
    tablesCacheTime = now;
  } catch {
    tablesCache = tablesCache || [];
  }
  return tablesCache;
}

async function getModelsCache() {
  const now = Date.now();
  if (modelsCache && now - modelsCacheTime < MODELS_TTL_MS) return modelsCache;
  try {
    modelsCache = await api.listModels();
    modelsCacheTime = now;
  } catch {
    modelsCache = modelsCache || [];
  }
  return modelsCache;
}

async function getColumnsCache(schema, table) {
  const key = `${schema}.${table}`;
  const cached = readColumns(key);
  if (cached !== undefined) return cached;
  let info;
  try {
    info = await api.describeTable(schema, table);
  } catch {
    info = null;
  }
  writeColumns(key, info);
  return info;
}

// A finished transform run invalidates every cached schema. The bridge in
// App.jsx fires this event once the pipeline completes.
if (typeof window !== "undefined") {
  window.addEventListener("havn-data-changed", invalidateSchemaCaches);
}

// ---------------------------------------------------------------------------
// Diagnostics: bind markers and lint markers
// ---------------------------------------------------------------------------

/** Marker owner for binder and validator diagnostics from POST /api/bind. */
export const BIND_MARKER_OWNER = "havn-bind";
/** Marker owner for SQLFluff violations from POST /api/lint/file. */
export const LINT_MARKER_OWNER = "havn-lint";

/** Bind is cheap, so it can follow the keyboard. */
export const BIND_DEBOUNCE_MS = 400;
/** SQLFluff is ~150 ms per call, so it waits for the typing to stop. */
export const LINT_DEBOUNCE_MS = 1500;

/**
 * Drop stale in-flight responses.
 *
 * Every request takes a ticket; only the newest ticket may write to the
 * editor. Without this a slow bind of an older buffer can land after a fast
 * bind of the current one and paint markers for text that is no longer there.
 * Same shape as QueryPanel's `acRequestRef`.
 */
export function createRequestGuard() {
  let current = 0;
  return {
    next: () => ++current,
    isStale: (id) => id !== current,
    current: () => current,
  };
}

/**
 * Map /api/bind diagnostics onto Monaco markers.
 *
 * `severity` carries the monaco.MarkerSeverity values, so this stays a pure
 * function that can be tested without a Monaco instance. An error with no
 * line is a whole-file error: rather than drop it, put it on line 1 spanning
 * that line, so the message is still visible.
 */
export function bindErrorsToMarkers(errors, severity, wholeFileEndColumn = 2) {
  return (errors || []).map((err) => {
    const sev = err.severity === "warning" ? severity.warning : severity.error;
    const base = { severity: sev, message: err.message, source: err.source || "bind" };
    if (err.line == null) {
      return {
        ...base,
        startLineNumber: 1,
        startColumn: 1,
        endLineNumber: 1,
        endColumn: Math.max(2, wholeFileEndColumn),
      };
    }
    const startLineNumber = Math.max(1, err.line);
    const startColumn = Math.max(1, err.col == null ? 1 : err.col);
    const endLineNumber = Math.max(startLineNumber, err.end_line == null ? startLineNumber : err.end_line);
    let endColumn;
    if (err.end_col != null) endColumn = err.end_col;
    else if (endLineNumber > startLineNumber) endColumn = 1;
    else endColumn = startColumn + 1;
    return { ...base, startLineNumber, startColumn, endLineNumber, endColumn };
  });
}

/** Map /api/lint/file violations onto Monaco markers. Lint is advisory, so always a warning. */
export function lintViolationsToMarkers(violations, warningSeverity) {
  return (violations || []).map((v) => {
    const line = Math.max(1, v.line || 1);
    const col = Math.max(1, v.col || 1);
    return {
      severity: warningSeverity,
      message: v.code ? `[${v.code}] ${v.description}` : v.description,
      source: "havn lint",
      startLineNumber: line,
      startColumn: col,
      endLineNumber: line,
      endColumn: col + 1,
    };
  });
}

// ---------------------------------------------------------------------------
// Reference resolution shared by the completion, hover and definition providers
// ---------------------------------------------------------------------------

/** Extract every `schema.table` reference and its alias from SQL text. */
export function extractTableRefs(text) {
  const refs = [];
  const pattern = /\b(?:FROM|JOIN)\s+(\w+)\.(\w+)(?:\s+(?:AS\s+)?(\w+))?/gi;
  let m;
  while ((m = pattern.exec(text)) !== null) {
    refs.push({ schema: m[1], table: m[2], alias: m[3] || null });
  }
  return refs;
}

/**
 * The key the binder uses for an upstream relation.
 *
 * POST /api/bind keys its `upstream` map by the lower-cased model name, while
 * the buffer keeps whatever case the author typed and may quote either half.
 * `FROM Bronze."Customers" c` names the same relation as `bronze.customers`,
 * so both have to arrive at the same key.
 */
export function relationKey(schema, table) {
  const clean = (part) => String(part || "").replace(/["`\[\]]/g, "").toLowerCase();
  return `${clean(schema)}.${clean(table)}`;
}

/**
 * An upstream relation's columns from a bind result, and the key they are
 * filed under. Returns null when the binder reported no schema for it.
 */
export function upstreamSchema(bind, schema, table) {
  const upstream = (bind && bind.upstream) || {};
  const key = relationKey(schema, table);
  if (Array.isArray(upstream[key])) return { key, columns: upstream[key] };
  const match = Object.keys(upstream).find((k) => k.toLowerCase() === key);
  return match ? { key: match, columns: upstream[match] || [] } : null;
}

/**
 * Resolve a hovered token to a column type using a bind result.
 *
 * Two cases resolve: a bare word that is one of the model's own output
 * columns, and `alias.column` where the alias names an upstream relation the
 * binder reported a schema for. Anything else returns null and the hover
 * keeps its existing behaviour.
 */
export function resolveColumnType({ word, qualifier, bind, tableRefs = [] }) {
  if (!word || !bind) return null;
  const lower = word.toLowerCase();
  if (qualifier) {
    const q = qualifier.toLowerCase();
    const ref = tableRefs.find(
      (r) => (r.alias && r.alias.toLowerCase() === q) || (!r.alias && r.table.toLowerCase() === q),
    );
    if (!ref) return null;
    const upstream = upstreamSchema(bind, ref.schema, ref.table);
    if (!upstream) return null;
    const col = upstream.columns.find((c) => c.name.toLowerCase() === lower);
    return col ? { name: col.name, type: col.type, source: upstream.key } : null;
  }
  const col = (bind.columns || []).find((c) => c.name.toLowerCase() === lower);
  return col ? { name: col.name, type: col.type, source: bind.model || null } : null;
}

/**
 * Find the model a `schema.name` reference points at.
 *
 * Always resolves through the model's own `path`. The
 * transform/{schema}/{name}.sql layout is only a default: `@config schema=`
 * can point a model at another schema, and jumping to a file that does not
 * exist is worse than not jumping at all.
 */
export function findModelDefinition(models, schema, name) {
  if (!schema || !name) return null;
  const full = `${schema}.${name}`.toLowerCase();
  const hit = (models || []).find((m) => String(m.full_name || "").toLowerCase() === full);
  return hit && hit.path ? hit : null;
}

/** The `alias` in `alias.word`, or null when the word is unqualified. */
export function qualifierBefore(line, word) {
  if (!word) return null;
  const before = String(line || "").substring(0, word.startColumn - 1);
  const match = before.match(/(\w+)\.\s*$/);
  return match ? match[1] : null;
}

/**
 * Read a `schema.name` reference around the cursor, whichever half it sits on.
 * `line` is the line text, `word` the Monaco word at the position.
 */
export function qualifiedRefAt(line, word) {
  if (!word) return null;
  const before = line.substring(0, word.startColumn - 1);
  const dotBefore = before.match(/(\w+)\.\s*$/);
  if (dotBefore) return { schema: dotBefore[1], name: word.word };
  const after = line.substring(word.endColumn - 1);
  const dotAfter = after.match(/^\s*\.(\w+)/);
  if (dotAfter) return { schema: word.word, name: dotAfter[1] };
  return null;
}

// ---------------------------------------------------------------------------
// Column rename
// ---------------------------------------------------------------------------

/**
 * Every name a `WITH` block defines: the CTEs themselves and the columns
 * aliased inside them.
 *
 * A CTE's `price * qty AS amount` is local to the query. It is not a column
 * of any model, so a rename must not follow it out to an upstream that
 * happens to have a column of the same name.
 */
export function cteLocalNames(sql) {
  const text = String(sql || "");
  const names = new Set();
  const header = /(?:\bWITH\b|,)\s*(?:RECURSIVE\s+)?"?(\w+)"?\s+AS\s*(?:NOT\s+MATERIALIZED\s+|MATERIALIZED\s+)?\(/gi;
  let m;
  while ((m = header.exec(text)) !== null) {
    names.add(m[1].toLowerCase());
    // Walk the CTE body, so only aliases inside it count.
    let depth = 1;
    let i = header.lastIndex;
    for (; i < text.length && depth > 0; i++) {
      if (text[i] === "(") depth++;
      else if (text[i] === ")") depth--;
    }
    const body = text.slice(header.lastIndex, Math.max(header.lastIndex, i - 1));
    const alias = /\bAS\s+"?(\w+)"?/gi;
    let a;
    while ((a = alias.exec(body)) !== null) names.add(a[1].toLowerCase());
  }
  return names;
}

/**
 * Which model's column the cursor is on, if any.
 *
 * Two things are renameable: the current model's own output column, and
 * `alias.column` reaching into an upstream relation the binder reported a
 * schema for. Anything else (a function name, a table name, a column of a
 * relation nobody bound) returns null, and the editor says so rather than
 * renaming something it cannot see the extent of.
 *
 * `cteLocal` are the names the buffer's own `WITH` blocks define; a token
 * that is one of those is nobody's column and never resolves to an upstream.
 */
export function renameTargetAt({ word, qualifier, bind, tableRefs = [], cteLocal = null }) {
  if (!word || !bind) return null;
  const lower = word.toLowerCase();
  if (qualifier) {
    const q = qualifier.toLowerCase();
    const ref = tableRefs.find(
      (r) => (r.alias && r.alias.toLowerCase() === q) || (!r.alias && r.table.toLowerCase() === q),
    );
    if (!ref) return null;
    const upstream = upstreamSchema(bind, ref.schema, ref.table);
    if (!upstream) return null;
    const hit = upstream.columns.find((c) => c.name.toLowerCase() === lower);
    return hit ? { model: upstream.key, column: hit.name } : null;
  }
  const own = (bind.columns || []).find((c) => c.name.toLowerCase() === lower);
  if (own && bind.model) return { model: bind.model, column: own.name };
  // A name the query itself invents lives and dies in this buffer. Following
  // it to an upstream column of the same name would rename a model the
  // cursor was never on.
  if (cteLocal && (cteLocal.has ? cteLocal.has(lower) : [...cteLocal].includes(lower))) return null;
  // Unqualified, and not an output column: it may still be an upstream
  // column, but only when exactly one upstream has it. Two would be a guess.
  const matches = [];
  for (const [key, columns] of Object.entries(bind.upstream || {})) {
    const hit = (columns || []).find((c) => c.name.toLowerCase() === lower);
    if (hit) matches.push({ model: key, column: hit.name });
  }
  return matches.length === 1 ? matches[0] : null;
}

/**
 * True when the buffer for `path` holds unsaved edits.
 *
 * The rename plan's offsets are offsets into the file on disk. A buffer that
 * has moved since the last save is a different string, so those offsets point
 * at the wrong characters and the rename must not touch it.
 */
export function bufferIsDirty(path) {
  if (!editorContext.dirty) return false;
  return !editorContext.activeFile || path === editorContext.activeFile;
}

/** The message shown when a rename is refused because the buffer is dirty. */
export const DIRTY_BUFFER_REJECTION = "Save the file before renaming";

/**
 * The file's content after a rename, as the server computed it.
 *
 * The plan carries the new content of every file it touches, so the open
 * buffer can be replaced wholesale instead of being spliced: a whole-file
 * swap cannot land an offset in the wrong place, and it leaves the buffer
 * equal to what the API just wrote to disk.
 */
export function renamedContentFor(plan, path) {
  const hit = ((plan && plan.files) || []).find((f) => f.path === path);
  return hit && typeof hit.content === "string" ? hit.content : null;
}

/** One line per blocker for the confirmation dialog. */
export function blockerLines(blocked) {
  return (blocked || []).map((b) => `${b.path}: ${b.message}`);
}

/** Short right-hand label for a site row: "where clause in silver.customers". */
export function siteLabel(site) {
  if (!site) return "";
  if (site.kind === "yaml") return "named in YAML";
  const where = site.clause === "select" ? "projection" : `${site.clause} clause`;
  if (site.kind === "definition") return `defined here (${where})`;
  if (site.kind === "alias") return "re-aliased here, the name stops";
  return site.resolved ? where : `${where}, unresolved`;
}

// ---------------------------------------------------------------------------
// Model directives
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// CTE preview
// ---------------------------------------------------------------------------

/** Monaco command the "Preview" code lens invokes. */
export const PREVIEW_CTE_COMMAND = "havn.previewCte";

/** Monaco command the "Find column references" action invokes. */
export const FIND_COLUMN_REFERENCES_COMMAND = "havn.findColumnReferences";

/**
 * Pick the CTE to preview from a /api/sql/ctes response.
 *
 * The server flags the CTE containing the requested line as `active`. With
 * the cursor outside every CTE there is still a sensible answer: the last
 * one, which is what a `WITH` chain builds up to.
 */
export function pickActiveCte(result) {
  const ctes = (result && result.ctes) || [];
  if (ctes.length === 0) return null;
  const active = result.active;
  if (active != null && ctes[active]) return ctes[active];
  return ctes[ctes.length - 1];
}

// Last /api/sql/ctes response, so the code lens provider does not re-ask for
// a buffer it has already parsed.
let cteMemo = { content: null, result: null };

async function getCtes(content, line) {
  if (line == null && cteMemo.content === content) return cteMemo.result;
  const result = await api.listCtes(content, line);
  if (line == null) cteMemo = { content, result };
  return result;
}

/** Short label for the editor toolbar: "binding...", "3 errors", "ok". */
export function bindStatusLabel(state) {
  if (!state) return null;
  if (state.running) return "binding…";
  if (state.errorCount > 0) return `${state.errorCount} error${state.errorCount === 1 ? "" : "s"}`;
  if (state.warningCount > 0) return `${state.warningCount} warning${state.warningCount === 1 ? "" : "s"}`;
  return "ok";
}

// Cache for macro metadata
let macrosCache = null;
let macrosCacheTime = 0;

async function getMacrosCache() {
  const now = Date.now();
  if (macrosCache && now - macrosCacheTime < 5 * 60 * 1000) return macrosCache;
  try {
    macrosCache = await getMacros();
    macrosCacheTime = now;
  } catch {
    macrosCache = macrosCache || [];
  }
  return macrosCache;
}

function buildMacroSignature(macro) {
  const paramStr = (macro.params || []).map((p) => `${p.name}: ${p.type}`).join(", ");
  if (macro.kind === "table") {
    return `${macro.name}(${paramStr}) -> TABLE`;
  }
  const ret = macro.return_type || "VARCHAR";
  return `${macro.name}(${paramStr}) -> ${ret}`;
}

// Register SQL hover + completion providers once when Monaco loads
let providersRegistered = false;
loader.init().then((monaco) => {
  if (providersRegistered) return;
  providersRegistered = true;

  // --- Completion provider ---

  // SQL keywords after which a table reference (schema.table) is expected
  const TABLE_CONTEXTS = /\b(?:FROM|JOIN|INNER\s+JOIN|LEFT\s+(?:OUTER\s+)?JOIN|RIGHT\s+(?:OUTER\s+)?JOIN|FULL\s+(?:OUTER\s+)?JOIN|CROSS\s+JOIN|INTO|UPDATE|TABLE)\s+(\w*)$/i;

  // SQL contexts where column names make sense
  const COLUMN_CONTEXTS = /\b(?:SELECT|WHERE|AND|OR|ON|USING|GROUP\s+BY|ORDER\s+BY|HAVING|SET|WHEN|THEN|ELSE|CASE|BETWEEN|AS|DISTINCT|NOT|IN|IS|LIKE|ILIKE|LIMIT|OFFSET)\s+(\w*)$/i;

  // Also columns after a comma (continuing a SELECT list, GROUP BY list, etc.)
  const AFTER_COMMA = /,\s*(\w*)$/;

  // Alias.column — "a." where "a" is an alias for a table
  const ALIAS_DOT = /\b(\w+)\.\s*(\w*)$/;

  monaco.languages.registerCompletionItemProvider("sql", {
    triggerCharacters: [".", " ", ","],
    provideCompletionItems: async (model, position) => {
      const fullText = model.getValue();
      // All text up to the cursor position
      const offset = model.getOffsetAt(position);
      const textBefore = fullText.substring(0, offset);

      const tables = await getTablesCache();

      // 1) After "schema." — always suggest tables in that schema
      const dotMatch = textBefore.match(ALIAS_DOT);
      if (dotMatch) {
        const prefix = dotMatch[1].toLowerCase();
        const partial = (dotMatch[2] || "").toLowerCase();

        // Check if prefix is a known schema — suggest schema.table completions
        const schemaMatch = tables.some((t) => t.schema.toLowerCase() === prefix);
        if (schemaMatch) {
          const suggestions = [];
          for (const t of tables) {
            if (t.schema.toLowerCase() === prefix && t.name.toLowerCase().startsWith(partial)) {
              suggestions.push({
                label: `${t.schema}.${t.name}`,
                kind: monaco.languages.CompletionItemKind.Struct,
                insertText: t.name,
                detail: t.type || "table",
                range: {
                  startLineNumber: position.lineNumber,
                  startColumn: position.column - partial.length,
                  endLineNumber: position.lineNumber,
                  endColumn: position.column,
                },
              });
            }
          }
          return { suggestions };
        }

        // Check if prefix is a table alias — suggest columns
        const tableRefs = extractTableRefs(fullText);
        const aliasRef = tableRefs.find(
          (r) => (r.alias && r.alias.toLowerCase() === prefix) || (!r.alias && r.table.toLowerCase() === prefix)
        );
        if (aliasRef) {
          const info = await getColumnsCache(aliasRef.schema, aliasRef.table);
          if (info && info.columns) {
            return {
              suggestions: info.columns
                .filter((c) => c.name.toLowerCase().startsWith(partial))
                .map((c) => ({
                  label: c.name,
                  kind: monaco.languages.CompletionItemKind.Field,
                  insertText: c.name,
                  detail: `${aliasRef.schema}.${aliasRef.table} — ${c.type}`,
                  range: {
                    startLineNumber: position.lineNumber,
                    startColumn: position.column - partial.length,
                    endLineNumber: position.lineNumber,
                    endColumn: position.column,
                  },
                })),
            };
          }
        }

        return { suggestions: [] };
      }

      // 2) Table context — FROM, JOIN, INTO, etc.
      const tableCtx = textBefore.match(TABLE_CONTEXTS);
      if (tableCtx) {
        const partial = (tableCtx[1] || "").toLowerCase();
        const suggestions = [];
        for (const t of tables) {
          const full = `${t.schema}.${t.name}`;
          if (
            t.schema.toLowerCase().startsWith(partial) ||
            t.name.toLowerCase().startsWith(partial) ||
            full.toLowerCase().startsWith(partial)
          ) {
            suggestions.push({
              label: full,
              kind: monaco.languages.CompletionItemKind.Struct,
              insertText: full,
              detail: t.type || "table",
              range: {
                startLineNumber: position.lineNumber,
                startColumn: position.column - partial.length,
                endLineNumber: position.lineNumber,
                endColumn: position.column,
              },
            });
          }
        }
        return { suggestions };
      }

      // 3) Column context — SELECT, WHERE, ON, GROUP BY, etc. or after comma
      const colCtx = textBefore.match(COLUMN_CONTEXTS) || textBefore.match(AFTER_COMMA);
      if (colCtx) {
        const partial = (colCtx[1] || "").toLowerCase();
        const tableRefs = extractTableRefs(fullText);
        if (tableRefs.length === 0) return { suggestions: [] };

        // Fetch columns for all referenced tables
        const allCols = [];
        for (const ref of tableRefs) {
          const info = await getColumnsCache(ref.schema, ref.table);
          if (info && info.columns) {
            for (const col of info.columns) {
              allCols.push({ ...col, source: `${ref.schema}.${ref.table}` });
            }
          }
        }

        // Deduplicate by name (if same column in multiple tables, show source)
        const seen = new Map();
        for (const col of allCols) {
          const key = col.name.toLowerCase();
          if (seen.has(key)) { seen.get(key).ambiguous = true; }
          else { seen.set(key, { ...col, ambiguous: false }); }
        }

        const suggestions = [];
        for (const [, col] of seen) {
          if (col.name.toLowerCase().startsWith(partial)) {
            suggestions.push({
              label: col.name,
              kind: monaco.languages.CompletionItemKind.Field,
              insertText: col.name,
              detail: `${col.type} — ${col.source}`,
              range: {
                startLineNumber: position.lineNumber,
                startColumn: position.column - partial.length,
                endLineNumber: position.lineNumber,
                endColumn: position.column,
              },
            });
          }
        }
        return { suggestions };
      }

      // 4) Macro completions — always offered when no other context matched
      const macros = await getMacrosCache();
      if (macros.length > 0) {
        const wordMatch = textBefore.match(/(\w+)$/);
        const partial = wordMatch ? wordMatch[1].toLowerCase() : "";
        const suggestions = [];
        for (const macro of macros) {
          if (!macro.name.toLowerCase().startsWith(partial)) continue;
          const sig = buildMacroSignature(macro);
          const isTable = macro.kind === "table";
          const kindLabel = isTable ? "[T] " : "[S] ";
          suggestions.push({
            label: macro.name,
            kind: isTable
              ? monaco.languages.CompletionItemKind.Method
              : monaco.languages.CompletionItemKind.Function,
            detail: kindLabel + sig,
            documentation: { value: macro.docstring || sig },
            insertText: `${macro.name}($0)`,
            insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
            range: {
              startLineNumber: position.lineNumber,
              startColumn: position.column - partial.length,
              endLineNumber: position.lineNumber,
              endColumn: position.column,
            },
          });
        }
        if (suggestions.length > 0) return { suggestions };
      }

      return { suggestions: [] };
    },
  });

  // --- Hover provider ---
  monaco.languages.registerHoverProvider("sql", {
    provideHover: async (model, position) => {
      const line = model.getLineContent(position.lineNumber);
      const word = model.getWordAtPosition(position);
      if (!word) return null;

      const hoverRange = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);

      // Inferred column type from the last bind of this buffer. Shown above
      // whatever else the hover has to say.
      const before = line.substring(0, word.startColumn - 1);
      const dotBefore = before.match(/(\w+)\.\s*$/);
      const bind = editorContext.bindResults.get(pathFromUri(model.uri));
      const typed = resolveColumnType({
        word: word.word,
        qualifier: dotBefore ? dotBefore[1] : null,
        bind,
        tableRefs: bind ? extractTableRefs(model.getValue()) : [],
      });
      const typePrefix = typed ? [`\`${typed.name}\`: **${typed.type}**${typed.source ? ` (${typed.source})` : ""}`, ""] : [];

      // Check if the hovered word is a known macro
      const macros = await getMacrosCache();
      const macroMatch = macros.find((m) => m.name === word.word);
      if (macroMatch) {
        const sig = buildMacroSignature(macroMatch);
        const kindLabel = macroMatch.kind === "table" ? "table macro" : macroMatch.kind === "sql" ? "SQL macro" : "scalar macro";
        const lines = [...typePrefix, `**${sig}** *(${kindLabel})*`];
        if (macroMatch.docstring) {
          lines.push("", macroMatch.docstring);
        }
        return { range: hoverRange, contents: [{ value: lines.join("\n") }] };
      }

      // Detect schema.table pattern around cursor
      let schema = null;
      let table = null;

      // Case 1: cursor is on the table part (after the dot)
      if (dotBefore) {
        schema = dotBefore[1];
        table = word.word;
      }

      // Case 2: cursor is on the schema part (before the dot)
      if (!schema) {
        const after = line.substring(word.endColumn - 1);
        const dotAfter = after.match(/^\s*\.(\w+)/);
        if (dotAfter) {
          schema = word.word;
          table = dotAfter[1];
        }
      }

      // Nothing but the inferred type resolved: still worth showing.
      if (!schema || !table) {
        if (typePrefix.length === 0) return null;
        return { range: hoverRange, contents: [{ value: typePrefix.join("\n").trimEnd() }] };
      }

      const info = await getColumnsCache(schema, table);

      if (!info || !info.columns || info.columns.length === 0) {
        return {
          range: hoverRange,
          contents: [{ value: [...typePrefix, `*${schema}.${table}* — table not found in warehouse`].join("\n") }],
        };
      }

      const lines = [...typePrefix, `**${schema}.${table}** — ${info.columns.length} columns`, ""];
      for (const col of info.columns) {
        lines.push(`- \`${col.name}\` *${col.type}*`);
      }

      return { range: hoverRange, contents: [{ value: lines.join("\n") }] };
    },
  });

  // --- Definition provider ---
  // Ctrl/Cmd+click and F12 on a `schema.name` reference jump to the file that
  // declares that model.
  monaco.languages.registerDefinitionProvider("sql", {
    provideDefinition: async (model, position) => {
      const word = model.getWordAtPosition(position);
      if (!word) return null;
      const ref = qualifiedRefAt(model.getLineContent(position.lineNumber), word);
      if (!ref) return null;
      const hit = findModelDefinition(await getModelsCache(), ref.schema, ref.name);
      if (!hit) return null;
      return {
        uri: monaco.Uri.parse(modelUriFor(hit.path)),
        range: new monaco.Range(1, 1, 1, 1),
      };
    },
  });

  // --- Rename provider ---
  // F2 on a column renames it in every model that reads it. The plan comes
  // from the server, which is the only thing that can see the downstream
  // models; the editor's job is to show what the plan refuses to do before
  // anything is written.
  monaco.languages.registerRenameProvider("sql", {
    resolveRenameLocation: async (model, position) => {
      const path = pathFromUri(model.uri);
      const word = model.getWordAtPosition(position);
      if (!word) return { rejectReason: "Nothing to rename here" };
      if (!isTransformSql(path)) {
        return { rejectReason: "Only columns in transform models can be renamed" };
      }
      if (bufferIsDirty(path)) return { rejectReason: DIRTY_BUFFER_REJECTION };
      const target = renameTargetAt({
        word: word.word,
        qualifier: qualifierBefore(model.getLineContent(position.lineNumber), word),
        bind: editorContext.bindResults.get(path),
        tableRefs: extractTableRefs(model.getValue()),
        cteLocal: cteLocalNames(model.getValue()),
      });
      if (!target) {
        return { rejectReason: `${word.word} is not a column this editor can resolve` };
      }
      return {
        range: new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn),
        text: word.word,
      };
    },

    provideRenameEdits: async (model, position, newName) => {
      const path = pathFromUri(model.uri);
      const word = model.getWordAtPosition(position);
      if (!word) return { edits: [], rejectReason: "Nothing to rename here" };
      if (bufferIsDirty(path)) return { edits: [], rejectReason: DIRTY_BUFFER_REJECTION };
      const target = renameTargetAt({
        word: word.word,
        qualifier: qualifierBefore(model.getLineContent(position.lineNumber), word),
        bind: editorContext.bindResults.get(path),
        tableRefs: extractTableRefs(model.getValue()),
        cteLocal: cteLocalNames(model.getValue()),
      });
      if (!target) {
        return { edits: [], rejectReason: `${word.word} is not a column this editor can resolve` };
      }

      let plan;
      try {
        plan = await api.planColumnRename(target.model, target.column, newName);
      } catch (e) {
        return { edits: [], rejectReason: e.message };
      }

      let force = false;
      if ((plan.blocked || []).length > 0) {
        const confirm = editorContext.confirmBlockers;
        const ok = confirm ? await confirm(plan.blocked, plan) : false;
        if (!ok) {
          return {
            edits: [],
            rejectReason: `${plan.blocked.length} place(s) the rename cannot see through`,
          };
        }
        force = true;
        try {
          plan = await api.planColumnRename(target.model, target.column, newName, true);
        } catch (e) {
          return { edits: [], rejectReason: e.message };
        }
      }
      if (plan.error) return { edits: [], rejectReason: plan.error };
      if (!(plan.edits || []).length) {
        return { edits: [], rejectReason: `Nothing references ${target.model}.${target.column}` };
      }

      // Renaming the model on screen is what F2 looks like it does. Anything
      // else -- an upstream model's column, reached through an alias -- edits
      // files the cursor was never in, so it is said out loud first.
      const bind = editorContext.bindResults.get(path);
      if (!bind || !bind.model || target.model !== bind.model) {
        const confirmTarget = editorContext.confirmTarget;
        const files = (plan.files || []).length;
        const ok = confirmTarget ? await confirmTarget({ ...target, files }) : false;
        if (!ok) {
          return { edits: [], rejectReason: `Renaming ${target.model}.${target.column} was not confirmed` };
        }
      }

      const hashes = {};
      for (const file of plan.files || []) hashes[file.path] = file.file_hash;
      try {
        await api.applyColumnRename(target.model, target.column, newName, hashes, force);
      } catch (e) {
        return { edits: [], rejectReason: e.message };
      }
      // The API wrote every file, this one included. Reload the buffer from
      // what the server produced rather than replaying the plan's offsets
      // into it: the buffer is then byte for byte what is on disk, and the
      // editor has nothing left to save.
      let renamed = renamedContentFor(plan, path);
      if (renamed == null) {
        try {
          renamed = (await api.readFile(path)).content;
        } catch {
          renamed = null;
        }
      }
      if (renamed != null && editorContext.reloadFile) editorContext.reloadFile(path, renamed);
      notifyFilesChanged((plan.files || []).map((f) => f.path));
      // Nothing for Monaco to splice: every file, including this buffer, is
      // already at its new content.
      return { edits: [] };
    },
  });

  // --- Find column references ---
  monaco.editor.registerCommand(FIND_COLUMN_REFERENCES_COMMAND, async (_accessor, payload) => {
    if (editorContext.showReferences) editorContext.showReferences(payload);
  });

  // --- CTE preview ---
  // A "Preview" lens above each CTE runs just that CTE, so a long WITH chain
  // can be checked a step at a time instead of only end to end.
  monaco.editor.registerCommand(PREVIEW_CTE_COMMAND, (_accessor, sql, name) => {
    if (editorContext.previewSql && sql) editorContext.previewSql(sql, name);
  });

  monaco.languages.registerCodeLensProvider("sql", {
    provideCodeLenses: async (model) => {
      if (!isTransformSql(pathFromUri(model.uri))) return { lenses: [], dispose: () => {} };
      let result;
      try {
        result = await getCtes(model.getValue(), null);
      } catch {
        return { lenses: [], dispose: () => {} };
      }
      const lenses = (result.ctes || []).map((cte, i) => ({
        range: new monaco.Range(Math.max(1, cte.start_line), 1, Math.max(1, cte.start_line), 1),
        id: `havn-cte-${i}`,
        command: {
          id: PREVIEW_CTE_COMMAND,
          title: "Preview",
          arguments: [cte.preview_sql, cte.name],
        },
      }));
      return { lenses, dispose: () => {} };
    },
  });

  // The editor holds one file at a time and the surrounding app owns which
  // file that is, so opening another model is the app's job, not Monaco's.
  // Handlers registered here run before Monaco's own, which would otherwise
  // swap the model out from under App.jsx's editor state.
  monaco.editor.registerEditorOpener({
    openCodeEditor: (_source, resource, selectionOrPosition) => {
      const path = projectPathFromUri(resource);
      if (!path || !editorContext.openModel) return false;
      // Same file: let Monaco reveal the position in place.
      if (path === editorContext.activeFile) return false;
      const line = selectionOrPosition
        ? selectionOrPosition.lineNumber || selectionOrPosition.startLineNumber || 1
        : 1;
      editorContext.openModel(path, line);
      return true;
    },
  });
});

export default function Editor({ content, language, onChange, activeFile, dirty, onReloadFile, onMount, goToLine, onFormat, onPreview, onOpenModel, onPreviewCte, onStatus }) {
  const { themeId } = useTheme();
  const monacoTheme = `havn-${themeId}`;
  const editorRef = useRef(null);
  const onFormatRef = useRef(onFormat);
  onFormatRef.current = onFormat;
  const onPreviewRef = useRef(onPreview);
  onPreviewRef.current = onPreview;

  const monacoRef = useRef(null);
  const [monacoReady, setMonacoReady] = useState(false);
  const bindGuardRef = useRef(createRequestGuard());
  const lintGuardRef = useRef(createRequestGuard());
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;
  const contentRef = useRef(content);
  contentRef.current = content;
  const activeFileRef = useRef(activeFile);
  activeFileRef.current = activeFile;

  const onPreviewCteRef = useRef(onPreviewCte);
  onPreviewCteRef.current = onPreviewCte;

  // Column references panel and the blocker confirmation it shares with the
  // rename provider.
  const [columnRefs, setColumnRefs] = useState(null);
  const [blockers, setBlockers] = useState(null);
  const blockerResolveRef = useRef(null);
  const [renameTarget, setRenameTarget] = useState(null);
  const targetResolveRef = useRef(null);

  function answerBlockers(proceed) {
    const resolve = blockerResolveRef.current;
    blockerResolveRef.current = null;
    setBlockers(null);
    if (resolve) resolve(proceed);
  }

  function answerTarget(proceed) {
    const resolve = targetResolveRef.current;
    targetResolveRef.current = null;
    setRenameTarget(null);
    if (resolve) resolve(proceed);
  }

  /**
   * Answer "no" for a dialog nobody can answer any more.
   *
   * A rename waits on these promises. If the file changes under the dialog,
   * or the editor unmounts while it is up, nothing will ever click a button
   * and the rename would wait forever, holding Monaco's rename session open.
   */
  function settlePendingDialogs() {
    if (blockerResolveRef.current) answerBlockers(false);
    if (targetResolveRef.current) answerTarget(false);
  }

  // Keep the module-level provider context pointed at the file on screen.
  editorContext.activeFile = activeFile || null;
  editorContext.dirty = !!dirty;
  editorContext.reloadFile = onReloadFile || null;
  editorContext.openModel = onOpenModel || null;
  editorContext.previewSql = onPreviewCte || null;
  editorContext.showReferences = setColumnRefs;
  editorContext.confirmBlockers = (blocked) =>
    new Promise((resolve) => {
      blockerResolveRef.current = resolve;
      setBlockers(blocked);
    });
  editorContext.confirmTarget = (target) =>
    new Promise((resolve) => {
      targetResolveRef.current = resolve;
      setRenameTarget(target);
    });

  // Warm the model list so go-to-definition resolves on the first try.
  useEffect(() => {
    if (isTransformSql(activeFile)) getModelsCache();
  }, [activeFile]);

  // The dialogs belong to the rename that opened them, and that rename is
  // about the file on screen.
  useEffect(() => settlePendingDialogs, [activeFile]);

  // --- Keep the number of live text models bounded ---
  // Every file opened gets its own model and @monaco-editor/react keeps it
  // until unmount. The recent ones stay, so switching back is instant; the
  // rest are disposed.
  const recentPathsRef = useRef([]);
  useEffect(() => {
    if (!activeFile) return;
    const recent = [activeFile, ...recentPathsRef.current.filter((p) => p !== activeFile)];
    pruneOpenModels(monacoRef.current, recent);
    recentPathsRef.current = recent.slice(0, MAX_OPEN_MODELS);
  }, [activeFile, monacoReady]);

  function reportStatus(state) {
    if (onStatusRef.current) onStatusRef.current(state);
  }

  /** The Monaco model backing `path`, or null if it is not open. */
  function modelFor(path) {
    const monaco = monacoRef.current;
    if (!monaco || !path) return null;
    try {
      return monaco.editor.getModel(monaco.Uri.parse(modelUriFor(path)));
    } catch {
      return null;
    }
  }

  // --- Toolbar status belongs to the file on screen ---
  // A new file has no counts until its own bind lands, so the toolbar is
  // cleared on every switch. Leaving the previous file's number up made the
  // toolbar claim an error in a model that does not have one.
  useEffect(() => {
    reportStatus(null);
  }, [activeFile]);

  // --- Bind diagnostics: debounced, follows the keyboard ---
  useEffect(() => {
    if (!monacoReady || !isTransformSql(activeFile)) return undefined;
    const timer = setTimeout(async () => {
      const monaco = monacoRef.current;
      const guard = bindGuardRef.current;
      const reqId = guard.next();
      const path = activeFile;
      reportStatus({ running: true, errorCount: 0, warningCount: 0 });
      let result;
      try {
        result = await api.bindSql(path, contentRef.current);
      } catch {
        if (guard.isStale(reqId)) return;
        reportStatus(null);
        return;
      }
      if (guard.isStale(reqId)) return;
      const model = modelFor(path);
      if (!model) return;
      editorContext.bindResults.set(path, result);
      seedColumnsFromBind(result);
      const errors = result.errors || [];
      monaco.editor.setModelMarkers(
        model,
        BIND_MARKER_OWNER,
        bindErrorsToMarkers(
          errors,
          { error: monaco.MarkerSeverity.Error, warning: monaco.MarkerSeverity.Warning },
          model.getLineMaxColumn(1),
        ),
      );
      reportStatus({
        running: false,
        errorCount: errors.filter((e) => e.severity !== "warning").length,
        warningCount: errors.filter((e) => e.severity === "warning").length,
      });
    }, BIND_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [content, activeFile, monacoReady]);

  // --- Lint diagnostics: on idle and on save, never per keystroke ---
  const runLintMarkersRef = useRef(null);
  runLintMarkersRef.current = async function runLintMarkers() {
    const monaco = monacoRef.current;
    const path = activeFileRef.current;
    if (!monaco || !isTransformSql(path)) return;
    const guard = lintGuardRef.current;
    const reqId = guard.next();
    let data;
    try {
      data = await api.lintFile(path, false, contentRef.current);
    } catch {
      return;
    }
    if (guard.isStale(reqId)) return;
    const model = modelFor(path);
    if (!model) return;
    monaco.editor.setModelMarkers(
      model,
      LINT_MARKER_OWNER,
      lintViolationsToMarkers(data.violations, monaco.MarkerSeverity.Warning),
    );
  };

  useEffect(() => {
    if (!monacoReady || !isTransformSql(activeFile)) return undefined;
    const timer = setTimeout(() => { runLintMarkersRef.current(); }, LINT_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [content, activeFile, monacoReady]);

  useEffect(() => {
    const onSaved = () => { runLintMarkersRef.current(); };
    window.addEventListener("havn-file-saved", onSaved);
    return () => window.removeEventListener("havn-file-saved", onSaved);
  }, []);

  // --- Clear both marker sets when the file leaves the editor ---
  useEffect(() => {
    const path = activeFile;
    return () => {
      if (!path) return;
      const monaco = monacoRef.current;
      const model = modelFor(path);
      if (monaco && model) {
        monaco.editor.setModelMarkers(model, BIND_MARKER_OWNER, []);
        monaco.editor.setModelMarkers(model, LINT_MARKER_OWNER, []);
      }
      editorContext.bindResults.delete(path);
      // Anything still in flight belongs to a file that is no longer open.
      bindGuardRef.current.next();
      lintGuardRef.current.next();
    };
  }, [activeFile]);

  function handleBeforeMount(monaco) {
    defineHavnThemes(monaco);
  }

  function handleEditorMount(editor, monaco) {
    editorRef.current = editor;
    monacoRef.current = monaco;
    setMonacoReady(true);
    if (onMount) onMount(editor);

    editor.addAction({
      id: "havn-format-sql",
      label: "Format SQL (havn lint --fix)",
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.KeyF],
      precondition: null,
      keybindingContext: null,
      run: () => { if (onFormatRef.current) onFormatRef.current(); },
    });

    editor.addAction({
      id: "havn-preview-sql",
      label: "Preview SQL results",
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter],
      precondition: null,
      keybindingContext: null,
      run: () => { if (onPreviewRef.current) onPreviewRef.current(); },
    });

    editor.addAction({
      id: "havn-preview-cte",
      label: "Preview CTE at cursor",
      keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyMod.Shift | monaco.KeyCode.Enter],
      precondition: null,
      keybindingContext: null,
      run: (ed) => { previewCteAtCursor(ed); },
    });

    editor.addAction({
      id: "havn-find-column-references",
      label: "Find column references",
      precondition: null,
      keybindingContext: null,
      contextMenuGroupId: "navigation",
      contextMenuOrder: 1.6,
      run: (ed) => { findColumnReferencesAtCursor(ed); },
    });
  }

  /**
   * List every place the column under the cursor is written.
   *
   * The same resolution the rename uses, without the edit: a model that only
   * filters on the column shows up here, which is the part a text search over
   * the project cannot tell you.
   */
  async function findColumnReferencesAtCursor(ed) {
    const path = activeFileRef.current;
    if (!isTransformSql(path)) return;
    const position = ed.getPosition();
    const model = ed.getModel();
    if (!position || !model) return;
    const word = model.getWordAtPosition(position);
    if (!word) return;
    const target = renameTargetAt({
      word: word.word,
      qualifier: qualifierBefore(model.getLineContent(position.lineNumber), word),
      bind: editorContext.bindResults.get(path),
      tableRefs: extractTableRefs(model.getValue()),
      cteLocal: cteLocalNames(model.getValue()),
    });
    if (!target) {
      setColumnRefs({ model: "", column: word.word, sites: [], blocked: [], error: `${word.word} is not a column this editor can resolve` });
      return;
    }
    setColumnRefs({ model: target.model, column: target.column, sites: [], blocked: [], loading: true });
    try {
      const result = await api.columnReferences(target.model, target.column);
      setColumnRefs(result);
    } catch (e) {
      setColumnRefs({ model: target.model, column: target.column, sites: [], blocked: [], error: e.message });
    }
  }

  /** Preview the CTE the cursor sits in, falling back to the last one. */
  async function previewCteAtCursor(ed) {
    const path = activeFileRef.current;
    if (!isTransformSql(path) || !onPreviewCteRef.current) return;
    const position = ed.getPosition();
    let result;
    try {
      result = await getCtes(contentRef.current, position ? position.lineNumber : null);
    } catch {
      return;
    }
    const cte = pickActiveCte(result);
    if (cte) onPreviewCteRef.current(cte.preview_sql, cte.name);
  }

  useEffect(() => {
    if (!goToLine || !editorRef.current) return;
    const { line, col } = goToLine;
    editorRef.current.revealLineInCenter(line);
    editorRef.current.setPosition({ lineNumber: line, column: col || 1 });
    editorRef.current.focus();
  }, [goToLine]);

  if (!activeFile) {
    return (
      <div style={styles.empty}>
        <div style={styles.emptyIcon}>
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--havn-text-dim)" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" />
            <polyline points="14,2 14,8 20,8" />
            <line x1="16" y1="13" x2="8" y2="13" />
            <line x1="16" y1="17" x2="8" y2="17" />
            <polyline points="10,9 9,9 8,9" />
          </svg>
        </div>
        <p style={styles.emptyText}>Select a file to edit</p>
        <p style={styles.emptyHint}>
          SQL files in <code style={styles.code}>transform/</code> are transformation models.
          <br />
          Python files in <code style={styles.code}>ingest/</code> and <code style={styles.code}>export/</code> are data scripts.
        </p>
      </div>
    );
  }

  // Adjust Monaco options based on file size for performance
  const contentLen = (content || "").length;
  const disableMinimap = contentLen > 500_000;    // > 500KB
  const disableFolding = contentLen > 1_000_000;  // > 1MB

  // Installed package files are still editable -- sometimes you need a quick
  // local patch to find out whether a fix works -- but the next
  // `havn packages install` replaces the whole checkout, so say so up front
  // rather than letting the edit quietly disappear.
  const activePackage = packageOfPath(activeFile);

  const editorElement = (
    <MonacoEditor
      height="100%"
      language={language}
      path={modelUriFor(activeFile)}
      value={content}
      onChange={(val) => onChange(val || "")}
      theme={monacoTheme}
      beforeMount={handleBeforeMount}
      onMount={(editor, monaco) => handleEditorMount(editor, monaco)}
      wrapperProps={{ "aria-label": "Code editor" }}
      options={{
        minimap: { enabled: !disableMinimap },
        hover: { above: false },
        fontSize: 13,
        lineNumbers: "on",
        renderLineHighlight: "all",
        scrollBeyondLastLine: false,
        wordWrap: "on",
        padding: { top: 12, bottom: 12 },
        tabSize: 4,
        insertSpaces: true,
        smoothScrolling: true,
        cursorBlinking: "smooth",
        cursorSmoothCaretAnimation: "on",
        fontFamily: "var(--havn-font-mono)",
        quickSuggestions: true,
        suggestOnTriggerCharacters: true,
        folding: !disableFolding,
      }}
    />
  );

  return (
    <div style={styles.shell}>
      {activePackage !== null && (
        <div style={styles.packageBanner} role="status">
          From installed package
          {activePackage ? <strong>{` ${activePackage}`}</strong> : null}
          {". The next "}
          <code style={styles.code}>havn packages install</code>
          {" overwrites your edits."}
        </div>
      )}
      <div style={styles.editorArea}>{editorElement}</div>
      {columnRefs && (
        <ColumnReferencesPanel
          result={columnRefs}
          onClose={() => setColumnRefs(null)}
          onJump={(site) => {
            if (editorContext.openModel) editorContext.openModel(site.path, site.line || 1, site.col || 1);
          }}
        />
      )}
      {blockers && <BlockerDialog blocked={blockers} onAnswer={answerBlockers} />}
      {renameTarget && <TargetDialog target={renameTarget} onAnswer={answerTarget} />}
    </div>
  );
}

/**
 * The sites a column reference search found, one row each.
 *
 * Rows are grouped by nothing on purpose: the order the server returns is
 * path then position, which reads like a file listing and keeps the
 * definition next to the model that owns it.
 */
function ColumnReferencesPanel({ result, onClose, onJump }) {
  const sites = result.sites || [];
  const blocked = result.blocked || [];
  return (
    <div style={styles.panel} aria-label="Column references">
      <div style={styles.panelHeader}>
        <span>
          {result.column
            ? `${result.model ? `${result.model}.` : ""}${result.column}`
            : "Column references"}
          {result.loading ? ": searching…" : `: ${sites.length} site${sites.length === 1 ? "" : "s"}`}
        </span>
        <button onClick={onClose} style={styles.panelClose} aria-label="Close column references">
          {"×"}
        </button>
      </div>
      <div style={styles.panelBody}>
        {result.error && <div style={styles.panelError}>{result.error}</div>}
        {sites.map((site, i) => (
          <button
            key={`${site.path}:${site.start}:${i}`}
            style={styles.panelRow}
            onClick={() => onJump(site)}
            title={`${site.path}:${site.line}`}
          >
            <span style={styles.panelPath}>{site.path}</span>
            <span style={styles.panelLine}>:{site.line}</span>
            <span style={styles.panelKind}>{siteLabel(site)}</span>
          </button>
        ))}
        {!result.loading && !result.error && sites.length === 0 && (
          <div style={styles.panelEmpty}>No references found.</div>
        )}
        {blocked.map((b, i) => (
          <div key={`blocked-${i}`} style={styles.panelBlocked}>
            {b.path}: {b.message}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * What the rename could not see through, shown before anything is written.
 *
 * Renaming past a blocker is allowed but never the default: the index has
 * already said it cannot account for these places, so the person has to say
 * they know.
 */
function BlockerDialog({ blocked, onAnswer }) {
  return (
    <div style={styles.dialogBackdrop} role="dialog" aria-label="Rename blockers">
      <div style={styles.dialog}>
        <div style={styles.dialogTitle}>
          {blocked.length} place{blocked.length === 1 ? "" : "s"} this rename cannot see through
        </div>
        <div style={styles.dialogBody}>
          {blockerLines(blocked).map((line, i) => (
            <div key={i} style={styles.dialogLine}>{line}</div>
          ))}
        </div>
        <div style={styles.dialogHint}>
          Renaming anyway leaves these untouched. They may need editing by hand.
        </div>
        <div style={styles.dialogButtons}>
          <button style={styles.dialogCancel} onClick={() => onAnswer(false)}>Cancel</button>
          <button style={styles.dialogConfirm} onClick={() => onAnswer(true)}>Rename anyway</button>
        </div>
      </div>
    </div>
  );
}

/** The sentence the target confirmation asks: model, column and file count. */
export function targetPrompt(target) {
  const files = (target && target.files) || 0;
  return `Rename ${target.model}.${target.column} in ${files} file${files === 1 ? "" : "s"}?`;
}

/**
 * Confirm a rename that leaves the model on screen.
 *
 * `alias.column` and an unqualified column of a single upstream both resolve
 * to another model's column, and renaming it edits files the cursor was never
 * in. The rename still happens, but never without being named first.
 */
function TargetDialog({ target, onAnswer }) {
  return (
    <div style={styles.dialogBackdrop} role="dialog" aria-label="Confirm rename target">
      <div style={styles.dialog}>
        <div style={styles.dialogTitle}>{targetPrompt(target)}</div>
        <div style={styles.dialogHint}>
          This column belongs to another model. Every model that reads it is rewritten too.
        </div>
        <div style={styles.dialogButtons}>
          <button style={styles.dialogCancel} onClick={() => onAnswer(false)}>Cancel</button>
          <button style={styles.dialogConfirm} onClick={() => onAnswer(true)}>Rename</button>
        </div>
      </div>
    </div>
  );
}

const styles = {
  shell: { display: "flex", flexDirection: "column", height: "100%", minHeight: 0, position: "relative" },
  editorArea: { flex: 1, minHeight: 0 },
  panel: { height: "180px", flexShrink: 0, borderTop: "1px solid var(--havn-border)", display: "flex", flexDirection: "column", overflow: "hidden", background: "var(--havn-bg-secondary)" },
  panelHeader: { padding: "4px 12px", fontSize: "11px", color: "var(--havn-text-secondary)", borderBottom: "1px solid var(--havn-border)", display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0 },
  panelClose: { background: "none", border: "none", color: "var(--havn-text-dim)", cursor: "pointer", fontSize: "14px", lineHeight: 1 },
  panelBody: { flex: 1, overflow: "auto", padding: "4px 0" },
  panelRow: { display: "flex", gap: "8px", alignItems: "baseline", width: "100%", textAlign: "left", background: "none", border: "none", cursor: "pointer", padding: "2px 12px", color: "var(--havn-text)", fontFamily: "var(--havn-font-mono)", fontSize: "12px" },
  panelPath: { color: "var(--havn-text)" },
  panelLine: { color: "var(--havn-text-dim)" },
  panelKind: { color: "var(--havn-text-secondary)", marginLeft: "auto" },
  panelEmpty: { padding: "6px 12px", fontSize: "12px", color: "var(--havn-text-dim)" },
  panelError: { padding: "6px 12px", fontSize: "12px", color: "var(--havn-red)" },
  panelBlocked: { padding: "2px 12px", fontSize: "12px", color: "var(--havn-yellow)", fontFamily: "var(--havn-font-mono)" },
  dialogBackdrop: { position: "absolute", inset: 0, background: "rgba(0,0,0,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 20 },
  dialog: { background: "var(--havn-bg-secondary)", border: "1px solid var(--havn-border)", borderRadius: "6px", padding: "16px", maxWidth: "520px", width: "90%", display: "flex", flexDirection: "column", gap: "10px" },
  dialogTitle: { fontSize: "13px", fontWeight: 600, color: "var(--havn-text)" },
  dialogBody: { maxHeight: "200px", overflow: "auto", display: "flex", flexDirection: "column", gap: "4px" },
  dialogLine: { fontSize: "12px", fontFamily: "var(--havn-font-mono)", color: "var(--havn-text-secondary)" },
  dialogHint: { fontSize: "12px", color: "var(--havn-text-dim)" },
  dialogButtons: { display: "flex", gap: "8px", justifyContent: "flex-end" },
  dialogCancel: { padding: "5px 12px", background: "var(--havn-btn-bg)", border: "1px solid var(--havn-border)", borderRadius: "4px", color: "var(--havn-text)", cursor: "pointer", fontSize: "12px" },
  dialogConfirm: { padding: "5px 12px", background: "var(--havn-accent)", border: "1px solid var(--havn-accent)", borderRadius: "4px", color: "var(--havn-bg)", cursor: "pointer", fontSize: "12px" },
  empty: { display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--havn-text-secondary)", gap: "6px" },
  emptyIcon: { marginBottom: "8px", opacity: 0.5 },
  emptyText: { margin: 0, fontSize: "15px", fontWeight: 500, letterSpacing: "-0.01em" },
  emptyHint: { margin: 0, fontSize: "13px", textAlign: "center", lineHeight: "1.8", color: "var(--havn-text-dim)", maxWidth: "400px" },
  code: { background: "var(--havn-btn-bg)", padding: "2px 6px", borderRadius: "3px", fontSize: "12px", fontFamily: "var(--havn-font-mono)" },
  packageWrap: { display: "flex", flexDirection: "column", height: "100%", minHeight: 0 },
  packageBanner: { flexShrink: 0, padding: "6px 12px", fontSize: "11.5px", color: "var(--havn-text-secondary)", background: "var(--havn-bg-secondary)", borderBottom: "1px solid var(--havn-border-light)" },
  packageEditor: { flex: 1, minHeight: 0 },
};

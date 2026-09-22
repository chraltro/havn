import { describe, it, expect, vi, beforeEach } from "vitest";

// Monaco never loads in jsdom, so stand in for it with a recorder. The
// providers register against this instance exactly as they would in a browser.
const h = vi.hoisted(() => {
  const registered = {
    completion: null,
    hover: null,
    definition: null,
    codeLens: null,
    rename: null,
    command: null,
    opener: null,
  };
  class FakeRange {
    constructor(startLineNumber, startColumn, endLineNumber, endColumn) {
      Object.assign(this, { startLineNumber, startColumn, endLineNumber, endColumn });
    }
  }
  const monaco = {
    Range: FakeRange,
    Uri: { parse: (s) => ({ scheme: "file", path: s.replace(/^file:\/\//, ""), toString: () => s }) },
    MarkerSeverity: { Hint: 1, Info: 2, Warning: 4, Error: 8 },
    languages: {
      CompletionItemKind: { Struct: 0, Field: 1, Method: 2, Function: 3 },
      CompletionItemInsertTextRule: { InsertAsSnippet: 4 },
      registerCompletionItemProvider: (_lang, p) => { registered.completion = p; },
      registerHoverProvider: (_lang, p) => { registered.hover = p; },
      registerDefinitionProvider: (_lang, p) => { registered.definition = p; },
      registerCodeLensProvider: (_lang, p) => { registered.codeLens = p; },
      registerRenameProvider: (_lang, p) => { registered.rename = p; },
    },
    editor: {
      defineTheme: () => {},
      setModelMarkers: vi.fn(),
      getModel: () => null,
      registerCommand: (id, handler) => { registered.command = { id, handler }; },
      registerEditorOpener: (opener) => { registered.opener = opener; },
    },
  };
  return { registered, monaco };
});

vi.mock("@monaco-editor/react", () => ({
  default: () => null,
  loader: { init: () => Promise.resolve(h.monaco) },
}));

vi.mock("./api", () => ({
  getMacros: vi.fn().mockResolvedValue([]),
  api: {
    listTables: vi.fn().mockResolvedValue([]),
    describeTable: vi.fn().mockResolvedValue(null),
    listModels: vi.fn().mockResolvedValue([]),
    bindSql: vi.fn(),
    listCtes: vi.fn(),
    lintFile: vi.fn(),
  },
}));

import { api } from "./api";
import {
  bindErrorsToMarkers,
  lintViolationsToMarkers,
  bindStatusLabel,
  createRequestGuard,
  findModelDefinition,
  qualifiedRefAt,
  pickActiveCte,
  stripModelDirectives,
  resolveColumnType,
  modelUriFor,
  pathFromUri,
  isTransformSql,
  editorContext,
  PREVIEW_CTE_COMMAND,
} from "./Editor";

const SEV = { error: 8, warning: 4 };

/** Wait for the provider registrations that happen once Monaco resolves. */
async function providersReady() {
  await vi.waitFor(() => {
    expect(h.registered.definition).toBeTruthy();
    expect(h.registered.codeLens).toBeTruthy();
  });
}

describe("bind markers", () => {
  it("maps a positioned error onto its exact range", () => {
    const markers = bindErrorsToMarkers(
      [{ severity: "error", message: 'Referenced column "custmer_id" not found', line: 7, col: 5, end_line: 7, end_col: 15, source: "bind" }],
      SEV,
    );
    expect(markers).toHaveLength(1);
    expect(markers[0]).toMatchObject({
      severity: 8,
      message: 'Referenced column "custmer_id" not found',
      source: "bind",
      startLineNumber: 7,
      startColumn: 5,
      endLineNumber: 7,
      endColumn: 15,
    });
  });

  it("maps severity warning onto the Warning severity", () => {
    const [marker] = bindErrorsToMarkers(
      [{ severity: "warning", message: "column is never used", line: 2, col: 1, end_line: null, end_col: null, source: "validate" }],
      SEV,
    );
    expect(marker.severity).toBe(4);
    expect(marker.source).toBe("validate");
  });

  it("puts a whole-file error (line null) on line 1 rather than dropping it", () => {
    const [marker] = bindErrorsToMarkers(
      [{ severity: "error", message: "syntax error at end of input", line: null, col: null, end_line: null, end_col: null, source: "bind" }],
      SEV,
      42,
    );
    expect(marker.message).toBe("syntax error at end of input");
    expect(marker.startLineNumber).toBe(1);
    expect(marker.startColumn).toBe(1);
    expect(marker.endLineNumber).toBe(1);
    expect(marker.endColumn).toBe(42);
  });

  it("spans one character when the end column is missing", () => {
    const [marker] = bindErrorsToMarkers(
      [{ severity: "error", message: "boom", line: 3, col: 9, end_line: null, end_col: null, source: "bind" }],
      SEV,
    );
    expect(marker.endLineNumber).toBe(3);
    expect(marker.endColumn).toBe(10);
  });

  it("returns no markers for an empty or missing error list", () => {
    expect(bindErrorsToMarkers([], SEV)).toEqual([]);
    expect(bindErrorsToMarkers(undefined, SEV)).toEqual([]);
  });

  it("maps lint violations to warnings with the rule code in the message", () => {
    const [marker] = lintViolationsToMarkers(
      [{ line: 4, col: 3, code: "LT02", description: "Expected indent of 4 spaces" }],
      4,
    );
    expect(marker).toMatchObject({
      severity: 4,
      message: "[LT02] Expected indent of 4 spaces",
      startLineNumber: 4,
      startColumn: 3,
      endColumn: 4,
    });
  });

  it("summarises status for the toolbar", () => {
    expect(bindStatusLabel(null)).toBeNull();
    expect(bindStatusLabel({ running: true })).toBe("binding…");
    expect(bindStatusLabel({ running: false, errorCount: 1, warningCount: 0 })).toBe("1 error");
    expect(bindStatusLabel({ running: false, errorCount: 3, warningCount: 0 })).toBe("3 errors");
    expect(bindStatusLabel({ running: false, errorCount: 0, warningCount: 2 })).toBe("2 warnings");
    expect(bindStatusLabel({ running: false, errorCount: 0, warningCount: 0 })).toBe("ok");
  });
});

describe("stale response guard", () => {
  it("only accepts the newest ticket", () => {
    const guard = createRequestGuard();
    const first = guard.next();
    const second = guard.next();
    expect(guard.isStale(first)).toBe(true);
    expect(guard.isStale(second)).toBe(false);
  });

  it("drops a slow response that lands after a fast newer one", async () => {
    const guard = createRequestGuard();
    const painted = [];

    const bind = async (label, delayMs) => {
      const id = guard.next();
      await new Promise((r) => setTimeout(r, delayMs));
      if (guard.isStale(id)) return;
      painted.push(label);
    };

    const slow = bind("stale buffer", 30);
    // A later keystroke fires a second bind that answers first.
    await new Promise((r) => setTimeout(r, 1));
    await bind("current buffer", 0);
    await slow;

    expect(painted).toEqual(["current buffer"]);
  });

  it("invalidates in-flight requests when the guard is bumped on file close", () => {
    const guard = createRequestGuard();
    const inFlight = guard.next();
    guard.next(); // file closed
    expect(guard.isStale(inFlight)).toBe(true);
  });
});

describe("go to definition", () => {
  const MODELS = [
    // schema comes from @config, so the file does not live under transform/silver/
    { name: "customers", schema: "silver", full_name: "silver.customers", path: "transform/core/customers.sql" },
    { name: "orders", schema: "bronze", full_name: "bronze.orders", path: "transform/bronze/orders.sql" },
    { name: "pathless", schema: "gold", full_name: "gold.pathless" },
  ];

  it("reads a qualified reference from either half of the token", () => {
    const line = "FROM bronze.orders o";
    expect(qualifiedRefAt(line, { word: "orders", startColumn: 13, endColumn: 19 })).toEqual({ schema: "bronze", name: "orders" });
    expect(qualifiedRefAt(line, { word: "bronze", startColumn: 6, endColumn: 12 })).toEqual({ schema: "bronze", name: "orders" });
    expect(qualifiedRefAt("SELECT total", { word: "total", startColumn: 8, endColumn: 13 })).toBeNull();
  });

  it("resolves through the model's own path, not the folder convention", () => {
    const hit = findModelDefinition(MODELS, "silver", "customers");
    expect(hit.path).toBe("transform/core/customers.sql");
  });

  it("is case insensitive and misses unknown models", () => {
    expect(findModelDefinition(MODELS, "BRONZE", "Orders").path).toBe("transform/bronze/orders.sql");
    expect(findModelDefinition(MODELS, "silver", "nope")).toBeNull();
    expect(findModelDefinition([], "silver", "customers")).toBeNull();
    expect(findModelDefinition(MODELS, null, "customers")).toBeNull();
  });

  it("refuses a model with no path rather than guessing one", () => {
    expect(findModelDefinition(MODELS, "gold", "pathless")).toBeNull();
  });

  it("returns a location at line 1 of the declaring file", async () => {
    api.listModels.mockResolvedValue(MODELS);
    await providersReady();
    const model = {
      uri: { path: "/transform/gold/report.sql" },
      getLineContent: () => "FROM silver.customers c",
      getWordAtPosition: () => ({ word: "customers", startColumn: 13, endColumn: 22 }),
    };
    const location = await h.registered.definition.provideDefinition(model, { lineNumber: 1, column: 14 });
    expect(location.uri.toString()).toBe("file:///transform/core/customers.sql");
    expect(location.range.startLineNumber).toBe(1);
  });

  it("hands an open request for another file to the app", async () => {
    await providersReady();
    const opened = [];
    editorContext.activeFile = "transform/gold/report.sql";
    editorContext.openModel = (path, line) => opened.push([path, line]);

    const handled = h.registered.opener.openCodeEditor(
      null,
      { scheme: "file", path: "/transform/core/customers.sql" },
      { lineNumber: 1, column: 1 },
    );
    expect(handled).toBe(true);
    expect(opened).toEqual([["transform/core/customers.sql", 1]]);
  });

  it("leaves a jump within the open file to Monaco", async () => {
    await providersReady();
    editorContext.activeFile = "transform/gold/report.sql";
    editorContext.openModel = () => { throw new Error("should not be called"); };
    expect(h.registered.opener.openCodeEditor(null, { path: "/transform/gold/report.sql" })).toBe(false);
  });
});

describe("CTE preview", () => {
  const CTES = {
    ctes: [
      { name: "base", start_line: 3, end_line: 6, preview_sql: "WITH base AS (SELECT 1) SELECT * FROM base" },
      { name: "joined", start_line: 7, end_line: 12, preview_sql: "WITH base AS (SELECT 1), joined AS (SELECT * FROM base) SELECT * FROM joined" },
    ],
    active: 1,
  };

  beforeEach(() => {
    api.listCtes.mockReset();
  });

  it("picks the CTE the server flagged as active", () => {
    expect(pickActiveCte(CTES).name).toBe("joined");
  });

  it("falls back to the last CTE when the cursor is outside every one", () => {
    expect(pickActiveCte({ ...CTES, active: null }).name).toBe("joined");
  });

  it("returns nothing when the buffer has no CTEs", () => {
    expect(pickActiveCte({ ctes: [], active: null })).toBeNull();
    expect(pickActiveCte(null)).toBeNull();
  });

  it("asks the server for the whole buffer and lenses each CTE", async () => {
    await providersReady();
    api.listCtes.mockResolvedValue(CTES);
    const sql = "@config materialized=table\n\nWITH base AS (\n  SELECT 1\n)\nSELECT * FROM base";
    const model = { uri: { path: "/transform/silver/customers.sql" }, getValue: () => sql };

    const { lenses } = await h.registered.codeLens.provideCodeLenses(model);

    expect(api.listCtes).toHaveBeenCalledWith(sql, null);
    expect(lenses).toHaveLength(2);
    expect(lenses[0].range.startLineNumber).toBe(3);
    expect(lenses[0].command.title).toBe("Preview");
    expect(lenses[0].command.id).toBe(PREVIEW_CTE_COMMAND);
    expect(lenses[0].command.arguments).toEqual([CTES.ctes[0].preview_sql, "base"]);
  });

  it("offers no lenses outside transform/ and does not call the server", async () => {
    await providersReady();
    api.listCtes.mockResolvedValue(CTES);
    const model = { uri: { path: "/ingest/scratch.sql" }, getValue: () => "SELECT 1" };
    const { lenses } = await h.registered.codeLens.provideCodeLenses(model);
    expect(lenses).toEqual([]);
    expect(api.listCtes).not.toHaveBeenCalled();
  });

  it("survives a failing /api/sql/ctes without breaking the editor", async () => {
    await providersReady();
    api.listCtes.mockRejectedValue(new Error("bad request"));
    const model = { uri: { path: "/transform/silver/late.sql" }, getValue: () => "SELECT 2" };
    const { lenses } = await h.registered.codeLens.provideCodeLenses(model);
    expect(lenses).toEqual([]);
  });

  it("forwards the lens command to the app's preview pane", async () => {
    await providersReady();
    const runs = [];
    editorContext.previewSql = (sql, name) => runs.push([sql, name]);
    expect(h.registered.command.id).toBe(PREVIEW_CTE_COMMAND);
    h.registered.command.handler(null, CTES.ctes[1].preview_sql, "joined");
    expect(runs).toEqual([[CTES.ctes[1].preview_sql, "joined"]]);
  });
});

describe("model directives", () => {
  it("blanks a mid-file @assert instead of sending it to DuckDB", () => {
    const sql = [
      "@config materialized=table, schema=silver",
      "",
      "SELECT customer_id",
      "@assert customer_id IS NOT NULL",
      "FROM bronze.customers",
    ].join("\n");
    const stripped = stripModelDirectives(sql);
    expect(stripped).not.toMatch(/@/);
    expect(stripped.split("\n")).toHaveLength(5);
    expect(stripped.split("\n")[3]).toBe("");
    expect(stripped).toContain("FROM bronze.customers");
  });

  it("blanks the legacy comment forms too, and leaves ordinary comments alone", () => {
    const stripped = stripModelDirectives("-- config: materialized=view\n-- a real note\nSELECT 1");
    expect(stripped.split("\n")).toEqual(["", "-- a real note", "SELECT 1"]);
  });

  it("keeps line numbers stable so preview errors still point at the file", () => {
    const sql = "@config materialized=table\n@description hi\nSELECT 1";
    expect(stripModelDirectives(sql).split("\n")).toHaveLength(3);
  });
});

describe("bind result lookups", () => {
  const BIND = {
    model: "silver.customers",
    ok: true,
    errors: [],
    columns: [{ name: "customer_id", type: "BIGINT" }, { name: "email", type: "VARCHAR" }],
    upstream: { "bronze.customers": [{ name: "raw_email", type: "VARCHAR" }] },
    duration_ms: 12,
  };

  it("types an output column of the model", () => {
    expect(resolveColumnType({ word: "email", qualifier: null, bind: BIND })).toEqual({
      name: "email", type: "VARCHAR", source: "silver.customers",
    });
  });

  it("types alias.column through the upstream schema", () => {
    const tableRefs = [{ schema: "bronze", table: "customers", alias: "c" }];
    expect(resolveColumnType({ word: "raw_email", qualifier: "c", bind: BIND, tableRefs })).toEqual({
      name: "raw_email", type: "VARCHAR", source: "bronze.customers",
    });
  });

  it("resolves nothing for an unknown alias or column", () => {
    expect(resolveColumnType({ word: "raw_email", qualifier: "x", bind: BIND, tableRefs: [] })).toBeNull();
    expect(resolveColumnType({ word: "nope", qualifier: null, bind: BIND })).toBeNull();
    expect(resolveColumnType({ word: "email", qualifier: null, bind: null })).toBeNull();
  });
});

describe("model URIs", () => {
  it("round-trips a project path", () => {
    expect(modelUriFor("transform/silver/customers.sql")).toBe("file:///transform/silver/customers.sql");
    expect(pathFromUri({ path: "/transform/silver/customers.sql" })).toBe("transform/silver/customers.sql");
    expect(modelUriFor(null)).toBeUndefined();
    expect(pathFromUri(null)).toBeNull();
  });

  it("recognises the files the model features apply to", () => {
    expect(isTransformSql("transform/silver/customers.sql")).toBe(true);
    expect(isTransformSql("ingest/load.py")).toBe(false);
    expect(isTransformSql("macros/utils.sql")).toBe(false);
    expect(isTransformSql(null)).toBe(false);
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const registered = { hover: null };
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
      CompletionItemKind: {}, CompletionItemInsertTextRule: {},
      registerCompletionItemProvider: () => {},
      registerHoverProvider: (_lang, p) => { registered.hover = p; },
      registerDefinitionProvider: () => {},
      registerCodeLensProvider: () => {},
      registerRenameProvider: () => {},
    },
    editor: {
      defineTheme: () => {},
      setModelMarkers: vi.fn(),
      getModel: () => null,
      registerCommand: () => {},
      registerEditorOpener: () => {},
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
    describeTable: vi.fn(),
    listModels: vi.fn().mockResolvedValue([]),
    bindSql: vi.fn(),
    listCtes: vi.fn(),
    lintFile: vi.fn(),
    columnReferences: vi.fn(),
    planColumnRename: vi.fn(),
    applyColumnRename: vi.fn(),
  },
}));

import { api } from "./api";
import {
  seedColumnsFromBind,
  splitRelationKey,
  invalidateSchemaCaches,
  editorContext,
} from "./Editor";

const PATH = "transform/silver/customers.sql";
const SQL = "SELECT c.email\nFROM bronze.customers c\n";

function hoverModel(word, line) {
  return {
    uri: { path: `/${PATH}`, toString: () => `file:///${PATH}` },
    getValue: () => SQL,
    getLineContent: () => line,
    getWordAtPosition: () => word,
  };
}

/** Hover the table name, which is the path that fills the column cache. */
function hoverTable() {
  return h.registered.hover.provideHover(
    hoverModel({ word: "customers", startColumn: 13, endColumn: 22 }, "FROM bronze.customers c"),
    { lineNumber: 2, column: 15 },
  );
}

beforeEach(async () => {
  await vi.waitFor(() => expect(h.registered.hover).toBeTruthy());
  invalidateSchemaCaches();
  api.describeTable.mockReset();
  editorContext.bindResults.clear();
});

describe("splitRelationKey", () => {
  it("splits a two-part name", () => {
    expect(splitRelationKey("bronze.customers")).toEqual({ schema: "bronze", name: "customers" });
  });

  it("splits a three-part name at the last dot, not the first", () => {
    expect(splitRelationKey("warehouse.bronze.customers"))
      .toEqual({ schema: "warehouse.bronze", name: "customers" });
  });

  it("tolerates a bare name", () => {
    expect(splitRelationKey("customers")).toEqual({ schema: "", name: "customers" });
  });
});

describe("seeding the column cache from a bind result", () => {
  it("keeps a live DESCRIBE that knows more columns than the binder", async () => {
    api.describeTable.mockResolvedValue({
      schema: "bronze",
      name: "customers",
      columns: [{ name: "a", type: "INT" }, { name: "b_new", type: "INT" }],
    });
    const live = await hoverTable();
    expect(live.contents[0].value).toContain("b_new");

    // A bind that fell back to _havn.model_columns (the last build) does not
    // know about the column added since.
    seedColumnsFromBind({ upstream: { "bronze.customers": [{ name: "a", type: "INT" }] } });

    const after = await hoverTable();
    expect(after.contents[0].value).toContain("b_new");
    expect(api.describeTable).toHaveBeenCalledTimes(1);
  });

  it("fills a cache that has no entry yet", async () => {
    api.describeTable.mockResolvedValue(null);
    seedColumnsFromBind({
      upstream: { "bronze.customers": [{ name: "a", type: "INT" }, { name: "b", type: "INT" }] },
    });
    const hover = await hoverTable();
    expect(hover.contents[0].value).toContain("2 columns");
    // The seed answered it, so no DESCRIBE was needed.
    expect(api.describeTable).not.toHaveBeenCalled();
  });

  it("replaces a known miss", async () => {
    api.describeTable.mockResolvedValue(null);
    const miss = await hoverTable();
    expect(miss.contents[0].value).toContain("table not found");
    seedColumnsFromBind({ upstream: { "bronze.customers": [{ name: "a", type: "INT" }] } });
    const seeded = await hoverTable();
    expect(seeded.contents[0].value).toContain("1 columns");
  });

  it("takes a wider schema over a narrower cached one", async () => {
    api.describeTable.mockResolvedValue({
      schema: "bronze", name: "customers", columns: [{ name: "a", type: "INT" }],
    });
    await hoverTable();
    seedColumnsFromBind({
      upstream: { "bronze.customers": [{ name: "a", type: "INT" }, { name: "b_new", type: "INT" }] },
    });
    const after = await hoverTable();
    expect(after.contents[0].value).toContain("b_new");
  });
});

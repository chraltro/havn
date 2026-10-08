import { describe, it, expect, vi } from "vitest";

// The buffer keeps the case the author typed; the binder keys its upstream
// map in lower case. These tests hold the two together.
const h = vi.hoisted(() => {
  class FakeRange {
    constructor(startLineNumber, startColumn, endLineNumber, endColumn) {
      Object.assign(this, { startLineNumber, startColumn, endLineNumber, endColumn });
    }
  }
  const registered = { hover: null };
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
    describeTable: vi.fn().mockResolvedValue(null),
    listModels: vi.fn().mockResolvedValue([]),
    bindSql: vi.fn(),
    listCtes: vi.fn(),
    lintFile: vi.fn(),
    columnReferences: vi.fn(),
    planColumnRename: vi.fn(),
    applyColumnRename: vi.fn(),
  },
}));

import {
  relationKey,
  upstreamSchema,
  resolveColumnType,
  renameTargetAt,
  editorContext,
} from "./Editor";

const PATH = "transform/silver/customers.sql";
// POST /api/bind lower-cases every upstream key.
const BIND = {
  model: "silver.customers",
  columns: [],
  upstream: { "bronze.customers": [{ name: "email", type: "VARCHAR" }] },
};

describe("relationKey", () => {
  it("lower-cases both halves", () => {
    expect(relationKey("Bronze", "Customers")).toBe("bronze.customers");
  });

  it("strips quoting around either half", () => {
    expect(relationKey('"Bronze"', '"Customers"')).toBe("bronze.customers");
    expect(relationKey("bronze", "`customers`")).toBe("bronze.customers");
  });
});

describe("upstreamSchema", () => {
  it("finds the relation however the buffer spells it", () => {
    expect(upstreamSchema(BIND, "Bronze", "Customers").key).toBe("bronze.customers");
    expect(upstreamSchema(BIND, "bronze", "customers").columns).toHaveLength(1);
  });

  it("matches a key the server did not lower-case either", () => {
    const bind = { upstream: { "Bronze.Customers": [{ name: "email", type: "VARCHAR" }] } };
    expect(upstreamSchema(bind, "bronze", "customers").key).toBe("Bronze.Customers");
  });

  it("returns null for a relation the binder reported nothing for", () => {
    expect(upstreamSchema(BIND, "bronze", "orders")).toBeNull();
  });
});

describe("mixed-case upstream references", () => {
  const refs = [{ schema: "Bronze", table: "Customers", alias: "c" }];

  it("hovers a type on alias.column", () => {
    expect(resolveColumnType({ word: "email", qualifier: "c", bind: BIND, tableRefs: refs }))
      .toEqual({ name: "email", type: "VARCHAR", source: "bronze.customers" });
  });

  it("resolves the rename target on alias.column", () => {
    expect(renameTargetAt({ word: "email", qualifier: "c", bind: BIND, tableRefs: refs }))
      .toEqual({ model: "bronze.customers", column: "email" });
  });

  it("still refuses a qualifier that names no relation in the query", () => {
    expect(renameTargetAt({ word: "email", qualifier: "zz", bind: BIND, tableRefs: refs })).toBeNull();
  });
});

describe("the hover provider on a mixed-case FROM", () => {
  it("shows the column type", async () => {
    await vi.waitFor(() => expect(h.registered.hover).toBeTruthy());
    editorContext.bindResults.set(PATH, BIND);
    const model = {
      uri: { path: `/${PATH}`, toString: () => `file:///${PATH}` },
      getValue: () => "SELECT c.email\nFROM Bronze.Customers c\n",
      getLineContent: () => "SELECT c.email",
      getWordAtPosition: () => ({ word: "email", startColumn: 10, endColumn: 15 }),
    };
    const hover = await h.registered.hover.provideHover(model, { lineNumber: 1, column: 12 });
    expect(hover.contents[0].value).toContain("VARCHAR");
    expect(hover.contents[0].value).toContain("bronze.customers");
  });
});

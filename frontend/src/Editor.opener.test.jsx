import { describe, it, expect, vi, beforeEach } from "vitest";

const h = vi.hoisted(() => {
  const registered = { opener: null };
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
      registerHoverProvider: () => {},
      registerDefinitionProvider: () => {},
      registerCodeLensProvider: () => {},
      registerRenameProvider: () => {},
    },
    editor: {
      defineTheme: () => {},
      setModelMarkers: vi.fn(),
      getModel: () => null,
      registerCommand: () => {},
      registerEditorOpener: (o) => { registered.opener = o; },
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

import { editorContext, projectPathFromUri } from "./Editor";

const PATH = "transform/silver/customers.sql";
const fileUri = (p) => ({ scheme: "file", path: `/${p}`, toString: () => `file:///${p}` });

let opened;

beforeEach(async () => {
  await vi.waitFor(() => expect(h.registered.opener).toBeTruthy());
  opened = [];
  editorContext.activeFile = PATH;
  editorContext.openModel = (p, line) => opened.push([p, line]);
});

describe("projectPathFromUri", () => {
  it("reads the path out of a file URI", () => {
    expect(projectPathFromUri(fileUri(PATH))).toBe(PATH);
  });

  it("refuses a scheme that is not file", () => {
    expect(projectPathFromUri({ scheme: "inmemory", path: "/model/17" })).toBeNull();
    expect(projectPathFromUri({ scheme: "vscode", path: "/settings.json" })).toBeNull();
  });

  it("refuses a URI with no scheme, an empty path or a climb out of the project", () => {
    expect(projectPathFromUri({ path: "/transform/x.sql" })).toBeNull();
    expect(projectPathFromUri({ scheme: "file", path: "" })).toBeNull();
    expect(projectPathFromUri({ scheme: "file", path: "/../../etc/passwd" })).toBeNull();
    expect(projectPathFromUri(null)).toBeNull();
  });
});

describe("the editor opener", () => {
  it("leaves an inmemory URI to Monaco", () => {
    const handled = h.registered.opener.openCodeEditor(
      null,
      { scheme: "inmemory", path: "/model/17", toString: () => "inmemory://model/17" },
      null,
    );
    expect(handled).toBe(false);
    expect(opened).toEqual([]);
  });

  it("opens another project file at the requested line", () => {
    const handled = h.registered.opener.openCodeEditor(
      null,
      fileUri("transform/gold/orders.sql"),
      { lineNumber: 12 },
    );
    expect(handled).toBe(true);
    expect(opened).toEqual([["transform/gold/orders.sql", 12]]);
  });

  it("lets Monaco reveal a position in the file already on screen", () => {
    expect(h.registered.opener.openCodeEditor(null, fileUri(PATH), { lineNumber: 3 })).toBe(false);
    expect(opened).toEqual([]);
  });

  it("does nothing for a resource with an empty path", () => {
    editorContext.openModel = () => { throw new Error("must not run"); };
    expect(h.registered.opener.openCodeEditor(null, { scheme: "file", path: "" }, null)).toBe(false);
  });
});

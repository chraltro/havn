import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Monaco never loads in jsdom, so stand in for it with a recorder, the same
// shape Editor.diagnostics.test.jsx uses.
const h = vi.hoisted(() => {
  const registered = { rename: null, command: null };
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
      registerCompletionItemProvider: () => {},
      registerHoverProvider: () => {},
      registerDefinitionProvider: () => {},
      registerCodeLensProvider: () => {},
      registerRenameProvider: (_lang, p) => { registered.rename = p; },
    },
    editor: {
      defineTheme: () => {},
      setModelMarkers: vi.fn(),
      getModel: () => null,
      registerCommand: (id, handler) => { registered.command = { id, handler }; },
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

import { api } from "./api";
import {
  renameTargetAt,
  blockerLines,
  siteLabel,
  qualifierBefore,
  editorContext,
} from "./Editor";

const BIND = {
  model: "silver.customers",
  columns: [{ name: "customer_id", type: "INTEGER" }, { name: "name", type: "VARCHAR" }],
  upstream: {
    "bronze.customers": [{ name: "customer_id", type: "INTEGER" }, { name: "email", type: "VARCHAR" }],
    "bronze.orders": [{ name: "order_id", type: "INTEGER" }],
  },
};

const TABLE_REFS = [
  { schema: "bronze", table: "customers", alias: "c" },
  { schema: "bronze", table: "orders", alias: "o" },
];

/** A text model stub with the offset arithmetic the plan mapping needs. */
function fakeModel(text, path = "transform/silver/customers.sql") {
  return {
    uri: { path: `/${path}`, toString: () => `file:///${path}` },
    getValue: () => text,
    getPositionAt: (offset) => {
      const before = text.slice(0, offset);
      const lines = before.split("\n");
      return { lineNumber: lines.length, column: lines[lines.length - 1].length + 1 };
    },
  };
}

describe("renameTargetAt", () => {
  it("resolves the current model's own output column", () => {
    expect(renameTargetAt({ word: "customer_id", qualifier: null, bind: BIND, tableRefs: TABLE_REFS }))
      .toEqual({ model: "silver.customers", column: "customer_id" });
  });

  it("resolves alias.column into the upstream model it comes from", () => {
    expect(renameTargetAt({ word: "email", qualifier: "c", bind: BIND, tableRefs: TABLE_REFS }))
      .toEqual({ model: "bronze.customers", column: "email" });
  });

  it("resolves an unqualified column that exactly one upstream has", () => {
    expect(renameTargetAt({ word: "order_id", qualifier: null, bind: BIND, tableRefs: TABLE_REFS }))
      .toEqual({ model: "bronze.orders", column: "order_id" });
  });

  it("refuses a word no bind result accounts for", () => {
    expect(renameTargetAt({ word: "coalesce", qualifier: null, bind: BIND, tableRefs: TABLE_REFS })).toBeNull();
  });

  it("refuses a qualifier that names no relation in the query", () => {
    expect(renameTargetAt({ word: "customer_id", qualifier: "zz", bind: BIND, tableRefs: TABLE_REFS })).toBeNull();
  });

  it("refuses without a bind result, rather than guessing from the text", () => {
    expect(renameTargetAt({ word: "customer_id", qualifier: null, bind: null })).toBeNull();
  });
});

describe("qualifierBefore", () => {
  it("reads the alias in alias.column", () => {
    expect(qualifierBefore("SELECT c.customer_id", { startColumn: 10, endColumn: 21 })).toBe("c");
  });

  it("returns null for an unqualified column", () => {
    expect(qualifierBefore("SELECT customer_id", { startColumn: 8, endColumn: 19 })).toBeNull();
  });
});

describe("blocker display", () => {
  const blocked = [
    { reason: "select_star", model: "gold.orders", path: "transform/gold/orders.sql", message: "the output expands SELECT *", line: null },
    { reason: "yaml_mention", model: "", path: "metrics/revenue.yml", message: "names the column in 2 places", line: null },
  ];

  it("renders one line per blocker, path first", () => {
    expect(blockerLines(blocked)).toEqual([
      "transform/gold/orders.sql: the output expands SELECT *",
      "metrics/revenue.yml: names the column in 2 places",
    ]);
  });

  it("tolerates a missing list", () => {
    expect(blockerLines(undefined)).toEqual([]);
  });
});

describe("site labels", () => {
  it("names the clause a reference sits in", () => {
    expect(siteLabel({ kind: "reference", clause: "where", resolved: true })).toBe("where clause");
    expect(siteLabel({ kind: "reference", clause: "select", resolved: true })).toBe("projection");
  });

  it("marks an unresolved reference", () => {
    expect(siteLabel({ kind: "reference", clause: "join", resolved: false })).toBe("join clause, unresolved");
  });

  it("says where the definition is and where the name stops", () => {
    expect(siteLabel({ kind: "definition", clause: "select", resolved: true })).toBe("defined here (projection)");
    expect(siteLabel({ kind: "alias", clause: "select", resolved: true })).toBe("re-aliased here, the name stops");
    expect(siteLabel({ kind: "yaml", clause: "yaml", resolved: false })).toBe("named in YAML");
  });
});

describe("the rename provider", () => {
  beforeEach(async () => {
    api.planColumnRename.mockReset();
    api.applyColumnRename.mockReset();
    await import("./Editor");
    await vi.waitFor(() => expect(h.registered.rename).toBeTruthy());
    editorContext.bindResults.set("transform/silver/customers.sql", BIND);
    editorContext.confirmBlockers = null;
  });

  const path = "transform/silver/customers.sql";
  const text = "SELECT customer_id FROM bronze.customers c\n";

  function modelWithWord() {
    const model = fakeModel(text, path);
    model.getWordAtPosition = () => ({ word: "customer_id", startColumn: 8, endColumn: 19 });
    model.getLineContent = () => "SELECT customer_id FROM bronze.customers c";
    return model;
  }

  it("refuses to rename a word outside transform/", async () => {
    const model = modelWithWord();
    model.uri = { path: "/ingest/load.sql", toString: () => "file:///ingest/load.sql" };
    const result = await h.registered.rename.resolveRenameLocation(model, { lineNumber: 1, column: 10 });
    expect(result.rejectReason).toMatch(/transform models/);
  });

  it("offers the identifier's range for a resolvable column", async () => {
    const result = await h.registered.rename.resolveRenameLocation(modelWithWord(), { lineNumber: 1, column: 10 });
    expect(result.text).toBe("customer_id");
    expect(result.range).toMatchObject({ startLineNumber: 1, startColumn: 8, endColumn: 19 });
  });

  it("applies through the API and leaves the splicing to nobody", async () => {
    api.planColumnRename.mockResolvedValue({
      blocked: [],
      edits: [{ path, start: 7, end: 18, old_text: "customer_id", new_text: "cust_id" }],
      files: [{ path, content: "SELECT cust_id FROM bronze.customers c\n", file_hash: "abc123" }],
    });
    api.applyColumnRename.mockResolvedValue({ status: "applied", files: [] });

    const result = await h.registered.rename.provideRenameEdits(modelWithWord(), { lineNumber: 1, column: 10 }, "cust_id");

    expect(api.applyColumnRename).toHaveBeenCalledWith(
      "silver.customers", "customer_id", "cust_id", { [path]: "abc123" }, false,
    );
    expect(result.edits).toEqual([]);
  });

  it("never applies with blockers when nothing can confirm them", async () => {
    api.planColumnRename.mockResolvedValue({
      blocked: [{ reason: "select_star", model: "gold.orders", path: "transform/gold/orders.sql", message: "expands SELECT *", line: null }],
      edits: [],
      files: [],
    });

    const result = await h.registered.rename.provideRenameEdits(modelWithWord(), { lineNumber: 1, column: 10 }, "cust_id");

    expect(result.edits).toEqual([]);
    expect(result.rejectReason).toMatch(/cannot see through/);
    expect(api.applyColumnRename).not.toHaveBeenCalled();
  });

  it("re-plans with force once the blockers are confirmed", async () => {
    api.planColumnRename
      .mockResolvedValueOnce({
        blocked: [{ reason: "select_star", model: "gold.orders", path: "transform/gold/orders.sql", message: "expands SELECT *", line: null }],
        edits: [],
        files: [],
      })
      .mockResolvedValueOnce({
        blocked: [],
        edits: [{ path, start: 7, end: 18, old_text: "customer_id", new_text: "cust_id" }],
        files: [{ path, content: "SELECT cust_id FROM bronze.customers c\n", file_hash: "abc123" }],
      });
    api.applyColumnRename.mockResolvedValue({ status: "applied", files: [] });
    editorContext.confirmBlockers = vi.fn().mockResolvedValue(true);

    const result = await h.registered.rename.provideRenameEdits(modelWithWord(), { lineNumber: 1, column: 10 }, "cust_id");

    expect(editorContext.confirmBlockers).toHaveBeenCalled();
    expect(api.planColumnRename).toHaveBeenLastCalledWith("silver.customers", "customer_id", "cust_id", true);
    expect(api.applyColumnRename).toHaveBeenCalledWith(
      "silver.customers", "customer_id", "cust_id", { [path]: "abc123" }, true,
    );
    expect(result.edits).toEqual([]);
  });

  it("surfaces a stale-file conflict from the apply call", async () => {
    api.planColumnRename.mockResolvedValue({
      blocked: [],
      edits: [{ path, start: 7, end: 18, old_text: "customer_id", new_text: "cust_id" }],
      files: [{ path, content: "…", file_hash: "abc123" }],
    });
    api.applyColumnRename.mockRejectedValue(new Error("Error (409): Files were modified"));

    const result = await h.registered.rename.provideRenameEdits(modelWithWord(), { lineNumber: 1, column: 10 }, "cust_id");
    expect(result.edits).toEqual([]);
    expect(result.rejectReason).toMatch(/409/);
  });
});

describe("the blocker dialog", () => {
  it("only reports a rename once the user says to go ahead", async () => {
    const { default: Editor } = await import("./Editor");
    render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile="transform/silver/customers.sql" />,
    );
    const answer = editorContext.confirmBlockers([
      { reason: "select_star", model: "gold.orders", path: "transform/gold/orders.sql", message: "expands SELECT *", line: null },
    ]);
    expect(await screen.findByText("transform/gold/orders.sql: expands SELECT *")).toBeTruthy();
    await userEvent.click(screen.getByText("Rename anyway"));
    expect(await answer).toBe(true);
  });
});

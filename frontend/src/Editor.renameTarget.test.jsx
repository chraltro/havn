import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const h = vi.hoisted(() => {
  const registered = { rename: null };
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
      registerRenameProvider: (_lang, p) => { registered.rename = p; },
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
    readFile: vi.fn(),
    bindSql: vi.fn(),
    listCtes: vi.fn(),
    lintFile: vi.fn(),
    columnReferences: vi.fn(),
    planColumnRename: vi.fn(),
    applyColumnRename: vi.fn(),
  },
}));

import { api } from "./api";
import Editor, { cteLocalNames, renameTargetAt, targetPrompt, editorContext } from "./Editor";

const PATH = "transform/silver/orders.sql";
const CTE_SQL = `@config materialized=table

WITH lines AS (
    SELECT order_id, price * qty AS amount
    FROM bronze.line_items
)

SELECT order_id, SUM(amount) AS order_total
FROM lines
GROUP BY 1
`;

const BIND = {
  model: "silver.orders",
  // `amount` is a CTE alias, not an output column of the model.
  columns: [{ name: "order_id", type: "INTEGER" }, { name: "order_total", type: "DECIMAL" }],
  upstream: { "bronze.line_items": [{ name: "order_id", type: "INTEGER" }, { name: "amount", type: "DECIMAL" }] },
};

function modelOn(word, line, text = CTE_SQL) {
  return {
    uri: { path: `/${PATH}`, toString: () => `file:///${PATH}` },
    getValue: () => text,
    getLineContent: () => line,
    getWordAtPosition: () => word,
  };
}

beforeEach(async () => {
  api.planColumnRename.mockReset();
  api.applyColumnRename.mockReset();
  await vi.waitFor(() => expect(h.registered.rename).toBeTruthy());
  editorContext.bindResults.set(PATH, BIND);
  editorContext.activeFile = PATH;
  editorContext.dirty = false;
  editorContext.confirmBlockers = null;
  editorContext.confirmTarget = null;
  editorContext.reloadFile = null;
});

describe("cteLocalNames", () => {
  it("collects the CTE name and the aliases defined inside it", () => {
    const names = cteLocalNames(CTE_SQL);
    expect(names.has("lines")).toBe(true);
    expect(names.has("amount")).toBe(true);
  });

  it("leaves aliases in the final SELECT out of it", () => {
    expect(cteLocalNames(CTE_SQL).has("order_total")).toBe(false);
  });

  it("handles several CTEs in one WITH chain", () => {
    const names = cteLocalNames(
      "WITH a AS (SELECT 1 AS x), b AS (SELECT x * 2 AS y FROM a)\nSELECT y FROM b",
    );
    expect([...names].sort()).toEqual(["a", "b", "x", "y"]);
  });

  it("is empty for a query with no WITH", () => {
    expect(cteLocalNames("SELECT amount FROM bronze.line_items").size).toBe(0);
  });
});

describe("a CTE-local column", () => {
  it("resolves to no rename target at all", () => {
    expect(renameTargetAt({
      word: "amount", qualifier: null, bind: BIND, tableRefs: [], cteLocal: cteLocalNames(CTE_SQL),
    })).toBeNull();
  });

  it("is refused by the rename provider", async () => {
    const model = modelOn({ word: "amount", startColumn: 19, endColumn: 25 }, "SELECT order_id, SUM(amount) AS order_total");
    const loc = await h.registered.rename.resolveRenameLocation(model, { lineNumber: 8, column: 22 });
    expect(loc.rejectReason).toMatch(/not a column this editor can resolve/);
    const result = await h.registered.rename.provideRenameEdits(model, { lineNumber: 8, column: 22 }, "total");
    expect(result.edits).toEqual([]);
    expect(api.planColumnRename).not.toHaveBeenCalled();
  });

  it("still resolves the same word when no CTE defines it", () => {
    expect(renameTargetAt({
      word: "amount", qualifier: null, bind: BIND, tableRefs: [], cteLocal: cteLocalNames("SELECT amount FROM bronze.line_items"),
    })).toEqual({ model: "bronze.line_items", column: "amount" });
  });
});

describe("renaming a column of another model", () => {
  const PLAIN = "SELECT amount FROM bronze.line_items\n";

  function plainModel() {
    return modelOn({ word: "amount", startColumn: 8, endColumn: 14 }, "SELECT amount FROM bronze.line_items", PLAIN);
  }

  const PLAN = {
    blocked: [],
    edits: [{ path: PATH, start: 7, end: 13, old_text: "amount", new_text: "amount_eur" }],
    files: [
      { path: PATH, content: "SELECT amount_eur FROM bronze.line_items\n", file_hash: "a1" },
      { path: "transform/bronze/line_items.sql", content: "…", file_hash: "b2" },
      { path: "transform/gold/orders.sql", content: "…", file_hash: "c3" },
      { path: "metrics/revenue.yml", content: "…", file_hash: "d4" },
    ],
  };

  it("asks first, naming the model, the column and the file count", async () => {
    api.planColumnRename.mockResolvedValue(PLAN);
    api.applyColumnRename.mockResolvedValue({ status: "applied", files: [] });
    editorContext.confirmTarget = vi.fn().mockResolvedValue(true);

    const result = await h.registered.rename.provideRenameEdits(plainModel(), { lineNumber: 1, column: 10 }, "amount_eur");

    expect(editorContext.confirmTarget).toHaveBeenCalledWith({
      model: "bronze.line_items", column: "amount", files: 4,
    });
    expect(targetPrompt({ model: "bronze.line_items", column: "amount", files: 4 }))
      .toBe("Rename bronze.line_items.amount in 4 files?");
    expect(api.applyColumnRename).toHaveBeenCalled();
    expect(result.edits).toEqual([]);
  });

  it("writes nothing when the confirmation is declined", async () => {
    api.planColumnRename.mockResolvedValue(PLAN);
    editorContext.confirmTarget = vi.fn().mockResolvedValue(false);

    const result = await h.registered.rename.provideRenameEdits(plainModel(), { lineNumber: 1, column: 10 }, "amount_eur");

    expect(result.rejectReason).toMatch(/bronze\.line_items\.amount was not confirmed/);
    expect(api.applyColumnRename).not.toHaveBeenCalled();
  });

  it("writes nothing when there is no dialog to ask with", async () => {
    api.planColumnRename.mockResolvedValue(PLAN);
    editorContext.confirmTarget = null;

    const result = await h.registered.rename.provideRenameEdits(plainModel(), { lineNumber: 1, column: 10 }, "amount_eur");

    expect(result.edits).toEqual([]);
    expect(api.applyColumnRename).not.toHaveBeenCalled();
  });

  it("does not ask for the model's own output column", async () => {
    api.planColumnRename.mockResolvedValue({
      blocked: [],
      edits: [{ path: PATH, start: 7, end: 15, old_text: "order_id", new_text: "id" }],
      files: [{ path: PATH, content: "SELECT id FROM bronze.line_items\n", file_hash: "a1" }],
    });
    api.applyColumnRename.mockResolvedValue({ status: "applied", files: [] });
    editorContext.confirmTarget = vi.fn().mockResolvedValue(true);

    const model = modelOn({ word: "order_id", startColumn: 8, endColumn: 16 }, "SELECT order_id FROM bronze.line_items", "SELECT order_id FROM bronze.line_items\n");
    await h.registered.rename.provideRenameEdits(model, { lineNumber: 1, column: 10 }, "id");

    expect(editorContext.confirmTarget).not.toHaveBeenCalled();
    expect(api.applyColumnRename).toHaveBeenCalled();
  });
});

describe("the target dialog", () => {
  it("names the target and resolves once the user goes ahead", async () => {
    render(<Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={PATH} />);
    let answer;
    await act(async () => {
      answer = editorContext.confirmTarget({ model: "bronze.line_items", column: "amount", files: 4 });
    });
    expect(await screen.findByText("Rename bronze.line_items.amount in 4 files?")).toBeTruthy();
    await userEvent.click(screen.getByText("Rename"));
    expect(await answer).toBe(true);
  });

  it("resolves false on Cancel", async () => {
    render(<Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={PATH} />);
    let answer;
    await act(async () => {
      answer = editorContext.confirmTarget({ model: "bronze.line_items", column: "amount", files: 1 });
    });
    expect(await screen.findByLabelText("Confirm rename target")).toBeTruthy();
    await userEvent.click(screen.getByText("Cancel"));
    expect(await answer).toBe(false);
  });
});

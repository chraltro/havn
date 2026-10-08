import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";

const h = vi.hoisted(() => {
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
      registerEditorOpener: () => {},
    },
  };
  return { monaco };
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
    bindSql: vi.fn(() => new Promise(() => {})),
    listCtes: vi.fn(),
    lintFile: vi.fn(() => new Promise(() => {})),
    columnReferences: vi.fn(),
    planColumnRename: vi.fn(),
    applyColumnRename: vi.fn(),
  },
}));

import Editor, { editorContext } from "./Editor";

const A = "transform/silver/customers.sql";
const B = "transform/gold/orders.sql";
const BLOCKED = [{ reason: "select_star", model: "gold.orders", path: "transform/gold/orders.sql", message: "expands SELECT *", line: null }];

function view(activeFile = A) {
  return render(<Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={activeFile} />);
}

beforeEach(() => {
  editorContext.confirmBlockers = null;
  editorContext.confirmTarget = null;
});

describe("a dialog nobody can answer", () => {
  it("refuses the blockers when the file changes under the dialog", async () => {
    const v = view(A);
    let answer;
    await act(async () => { answer = editorContext.confirmBlockers(BLOCKED); });
    expect(await screen.findByLabelText("Rename blockers")).toBeTruthy();

    await act(async () => {
      v.rerender(<Editor content="SELECT 2" language="sql" onChange={() => {}} activeFile={B} />);
    });

    expect(await answer).toBe(false);
    expect(screen.queryByLabelText("Rename blockers")).toBeNull();
  });

  it("refuses the blockers when the editor unmounts", async () => {
    const v = view(A);
    let answer;
    await act(async () => { answer = editorContext.confirmBlockers(BLOCKED); });
    expect(await screen.findByLabelText("Rename blockers")).toBeTruthy();

    await act(async () => { v.unmount(); });

    expect(await answer).toBe(false);
  });

  it("refuses the target confirmation when the file changes", async () => {
    const v = view(A);
    let answer;
    await act(async () => {
      answer = editorContext.confirmTarget({ model: "bronze.customers", column: "email", files: 3 });
    });
    expect(await screen.findByLabelText("Confirm rename target")).toBeTruthy();

    await act(async () => {
      v.rerender(<Editor content="SELECT 2" language="sql" onChange={() => {}} activeFile={B} />);
    });

    expect(await answer).toBe(false);
  });

  it("refuses the target confirmation when the editor unmounts", async () => {
    const v = view(A);
    let answer;
    await act(async () => {
      answer = editorContext.confirmTarget({ model: "bronze.customers", column: "email", files: 3 });
    });
    await act(async () => { v.unmount(); });
    expect(await answer).toBe(false);
  });

  it("leaves a dialog alone while the same file stays open", async () => {
    const v = view(A);
    let settled = false;
    await act(async () => {
      editorContext.confirmBlockers(BLOCKED).then(() => { settled = true; });
    });
    await act(async () => {
      v.rerender(<Editor content="SELECT 1 + 1" language="sql" onChange={() => {}} activeFile={A} />);
    });
    expect(settled).toBe(false);
    expect(screen.getByLabelText("Rename blockers")).toBeTruthy();
  });
});

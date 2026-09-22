import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

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
    bindSql: vi.fn().mockResolvedValue({ errors: [], columns: [], upstream: {} }),
    listCtes: vi.fn(),
    lintFile: vi.fn().mockResolvedValue({ violations: [] }),
    columnReferences: vi.fn(),
    planColumnRename: vi.fn(),
    applyColumnRename: vi.fn(),
  },
}));

import Editor, { editorContext } from "./Editor";

const PACKAGE_FILE = "havn_packages/crm/transform/silver/customers.sql";

describe("package banner", () => {
  it("shows the package name for a file under havn_packages/<pkg>/", () => {
    render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={PACKAGE_FILE} />,
    );
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("From installed package");
    expect(banner.textContent).toContain("crm");
    expect(banner.textContent).toContain("havn packages install");
  });

  it("shows no banner for a project file", () => {
    render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}}
        activeFile="transform/silver/customers.sql" />,
    );
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows no banner for a path that merely mentions the packages dir", () => {
    render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}}
        activeFile="transform/havn_packages/x.sql" />,
    );
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("still renders the references panel and the blocker dialog above the editor", async () => {
    render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={PACKAGE_FILE} />,
    );
    // The command handler in the module feeds this setter.
    editorContext.showReferences({
      model: "crm_silver.customers",
      column: "email",
      sites: [{ path: "transform/gold/x.sql", line: 4, start: 1, end: 2, kind: "reference", clause: "where", resolved: true }],
      blocked: [],
    });
    expect(await screen.findByLabelText("Column references")).toBeTruthy();
    editorContext.confirmBlockers([{ path: "transform/gold/x.sql", message: "expands SELECT *" }]);
    expect(await screen.findByLabelText("Rename blockers")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("From installed package");
  });
});

import React, { useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act, screen } from "@testing-library/react";

// The rename plan's offsets are offsets into the file on disk. These tests
// pin down what the editor does with a buffer that no longer matches it.
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
    KeyMod: { CtrlCmd: 1, Shift: 2 },
    KeyCode: { KeyF: 10, Enter: 11 },
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
import Editor, { editorContext, bufferIsDirty, renamedContentFor } from "./Editor";

const PATH = "transform/silver/customers.sql";
const DISK = "SELECT depth_km AS depth_km\nFROM bronze.quakes\n";
const RENAMED = "SELECT depth AS depth\nFROM bronze.quakes\n";
const BIND = {
  model: "silver.customers",
  columns: [{ name: "depth_km", type: "DOUBLE" }],
  upstream: {},
};

const PLAN = {
  blocked: [],
  edits: [{ path: PATH, start: 7, end: 15, old_text: "depth_km", new_text: "depth" }],
  files: [{ path: PATH, content: RENAMED, file_hash: "abc123" }],
};

/** A text model whose buffer is what the user sees, which may not be disk. */
function bufferModel(text) {
  return {
    uri: { path: `/${PATH}`, toString: () => `file:///${PATH}` },
    getValue: () => text,
    getLineContent: () => text.split("\n")[0],
    getWordAtPosition: () => ({ word: "depth_km", startColumn: 8, endColumn: 16 }),
    getPositionAt: (offset) => {
      const lines = text.slice(0, offset).split("\n");
      return { lineNumber: lines.length, column: lines[lines.length - 1].length + 1 };
    },
  };
}

/** The App-side wiring: the buffer, its dirty flag, and the reload hook. */
function Harness({ initial, initialDirty }) {
  const [text, setText] = useState(initial);
  const [dirty, setDirty] = useState(!!initialDirty);
  return (
    <>
      <div data-testid="buffer">{text}</div>
      <div data-testid="dirty">{dirty ? "dirty" : "clean"}</div>
      <Editor
        content={text}
        language="sql"
        onChange={(val) => { setText(val); setDirty(true); }}
        activeFile={PATH}
        dirty={dirty}
        onReloadFile={(path, next) => { if (path === PATH) { setText(next); setDirty(false); } }}
      />
    </>
  );
}

beforeEach(async () => {
  api.planColumnRename.mockReset();
  api.applyColumnRename.mockReset();
  api.readFile.mockReset();
  await vi.waitFor(() => expect(h.registered.rename).toBeTruthy());
  editorContext.bindResults.set(PATH, BIND);
  editorContext.confirmBlockers = null;
  editorContext.activeFile = PATH;
  editorContext.dirty = false;
  editorContext.reloadFile = null;
});

describe("bufferIsDirty", () => {
  it("is true for the file on screen while it has unsaved edits", () => {
    editorContext.activeFile = PATH;
    editorContext.dirty = true;
    expect(bufferIsDirty(PATH)).toBe(true);
  });

  it("is false for a file that is not the one on screen", () => {
    editorContext.activeFile = "transform/gold/orders.sql";
    editorContext.dirty = true;
    expect(bufferIsDirty(PATH)).toBe(false);
  });

  it("is false once the buffer is saved", () => {
    editorContext.activeFile = PATH;
    editorContext.dirty = false;
    expect(bufferIsDirty(PATH)).toBe(false);
  });
});

describe("renaming a dirty buffer", () => {
  it("refuses the rename location, naming the save", async () => {
    render(<Harness initial={`-- note\n${DISK}`} initialDirty />);
    const model = bufferModel(`-- note\n${DISK}`);
    const loc = await h.registered.rename.resolveRenameLocation(model, { lineNumber: 2, column: 10 });
    expect(loc.rejectReason).toBe("Save the file before renaming");
  });

  it("writes nothing when the edits are asked for anyway", async () => {
    render(<Harness initial={`-- note\n${DISK}`} initialDirty />);
    api.planColumnRename.mockResolvedValue(PLAN);
    const model = bufferModel(`-- note\n${DISK}`);
    const result = await h.registered.rename.provideRenameEdits(model, { lineNumber: 2, column: 10 }, "depth");
    expect(result.rejectReason).toBe("Save the file before renaming");
    expect(result.edits).toEqual([]);
    expect(api.planColumnRename).not.toHaveBeenCalled();
    expect(api.applyColumnRename).not.toHaveBeenCalled();
    expect(screen.getByTestId("buffer").textContent).toBe(`-- note\n${DISK}`);
  });
});

describe("renaming a clean buffer", () => {
  it("leaves the buffer equal to disk and not dirty, with no edits to splice", async () => {
    render(<Harness initial={DISK} />);
    api.planColumnRename.mockResolvedValue(PLAN);
    api.applyColumnRename.mockResolvedValue({ status: "applied", files: [{ path: PATH, file_hash: "def456" }] });

    let result;
    await act(async () => {
      result = await h.registered.rename.provideRenameEdits(bufferModel(DISK), { lineNumber: 1, column: 10 }, "depth");
    });

    expect(api.applyColumnRename).toHaveBeenCalled();
    // Nothing is spliced: the whole file is taken from the server's plan.
    expect(result.edits).toEqual([]);
    expect(screen.getByTestId("buffer").textContent).toBe(RENAMED);
    expect(screen.getByTestId("dirty").textContent).toBe("clean");
  });

  it("re-reads the file when the plan carries no content for it", async () => {
    render(<Harness initial={DISK} />);
    api.planColumnRename.mockResolvedValue({ ...PLAN, files: [{ path: PATH, file_hash: "abc123" }] });
    api.applyColumnRename.mockResolvedValue({ status: "applied", files: [] });
    api.readFile.mockResolvedValue({ content: RENAMED, language: "sql" });

    await act(async () => {
      await h.registered.rename.provideRenameEdits(bufferModel(DISK), { lineNumber: 1, column: 10 }, "depth");
    });

    expect(api.readFile).toHaveBeenCalledWith(PATH);
    expect(screen.getByTestId("buffer").textContent).toBe(RENAMED);
    expect(screen.getByTestId("dirty").textContent).toBe("clean");
  });
});

describe("renamedContentFor", () => {
  it("picks this file's new content out of the plan", () => {
    expect(renamedContentFor(PLAN, PATH)).toBe(RENAMED);
  });

  it("returns null for a file the plan does not carry", () => {
    expect(renamedContentFor(PLAN, "transform/gold/orders.sql")).toBeNull();
    expect(renamedContentFor(null, PATH)).toBeNull();
  });
});

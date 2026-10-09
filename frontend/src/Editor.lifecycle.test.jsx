import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";

// A Monaco stand-in with a real model registry, so a model can be checked for
// being disposed and markers can be attributed to a specific URI.
const h = vi.hoisted(() => {
  const models = new Map();
  class FakeRange {
    constructor(startLineNumber, startColumn, endLineNumber, endColumn) {
      Object.assign(this, { startLineNumber, startColumn, endLineNumber, endColumn });
    }
  }
  function makeModel(uriStr, value) {
    return {
      uri: { scheme: "file", path: uriStr.replace(/^file:\/\//, ""), toString: () => uriStr },
      _value: value,
      getValue() { return this._value; },
      setValue(v) { this._value = v; },
      getLineMaxColumn: () => 2,
      getLineContent: () => "",
      getWordAtPosition: () => null,
      disposed: false,
      isDisposed() { return this.disposed; },
      dispose() { this.disposed = true; models.delete(uriStr); },
    };
  }
  const markerCalls = [];
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
      registerRenameProvider: () => {},
    },
    editor: {
      defineTheme: () => {},
      setModelMarkers: (model, owner, markers) => {
        markerCalls.push({ uri: model.uri.toString(), owner, markers });
      },
      getModel: (uri) => models.get(uri.toString()) || null,
      registerCommand: () => {},
      registerEditorOpener: () => {},
    },
  };
  return { monaco, models, markerCalls, makeModel };
});

vi.mock("@monaco-editor/react", () => {
  const React = require("react");
  return {
    loader: { init: () => Promise.resolve(h.monaco) },
    // Mimics @monaco-editor/react 4.7: get-or-create a model per `path`,
    // never dispose it on a path change, push `value` into it.
    default: function MonacoEditorMock({ path, value, onMount }) {
      const mounted = React.useRef(false);
      React.useEffect(() => {
        if (!h.models.has(path)) h.models.set(path, h.makeModel(path, value ?? ""));
      }, [path]);
      React.useEffect(() => {
        const m = h.models.get(path);
        if (m && value !== undefined && m.getValue() !== value) m.setValue(value);
      }, [path, value]);
      React.useEffect(() => {
        if (mounted.current) return;
        mounted.current = true;
        onMount?.({ addAction: () => {}, getPosition: () => null, getModel: () => h.models.get(path) }, h.monaco);
      }, []);
      return null;
    },
  };
});

const deferred = () => {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
};

vi.mock("./api", () => ({
  getMacros: vi.fn().mockResolvedValue([]),
  api: {
    listTables: vi.fn().mockResolvedValue([]),
    describeTable: vi.fn().mockResolvedValue(null),
    listModels: vi.fn().mockResolvedValue([]),
    bindSql: vi.fn(),
    listCtes: vi.fn(),
    lintFile: vi.fn().mockResolvedValue({ violations: [] }),
    columnReferences: vi.fn(),
    planColumnRename: vi.fn(),
    applyColumnRename: vi.fn(),
  },
}));

import { api } from "./api";
import Editor, { BIND_MARKER_OWNER, MAX_OPEN_MODELS, pruneOpenModels } from "./Editor";

const A = "transform/silver/a.sql";
const B = "transform/silver/b.sql";
const uri = (p) => `file:///${p}`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

beforeEach(() => {
  h.markerCalls.length = 0;
  h.models.clear();
  api.bindSql.mockReset();
});

describe("bind markers across a file switch", () => {
  it("never paints file A's diagnostics on file B's model", async () => {
    const slowA = deferred();
    api.bindSql.mockImplementation((path) => {
      if (path === A) return slowA.promise;
      return Promise.resolve({ model: B, errors: [], columns: [], upstream: {} });
    });

    const view = render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={A} />,
    );
    await act(async () => { await sleep(500); });
    expect(api.bindSql).toHaveBeenCalledWith(A, "SELECT 1");

    await act(async () => {
      view.rerender(<Editor content="SELECT 2" language="sql" onChange={() => {}} activeFile={B} />);
      await sleep(0);
    });
    await act(async () => {
      slowA.resolve({
        model: A,
        errors: [{ severity: "error", message: "stale A error", line: 1, col: 1, end_line: 1, end_col: 2 }],
        columns: [], upstream: {},
      });
      await sleep(600);
    });

    const painted = h.markerCalls.filter((c) => c.owner === BIND_MARKER_OWNER && c.markers.length);
    for (const call of painted) {
      expect(call.markers.some((m) => m.message === "stale A error")).toBe(false);
    }
    expect(painted.every((c) => c.uri === uri(B) || c.markers.length === 0)).toBe(true);
  });

  it("clears both marker owners on A's model when the file switches", async () => {
    api.bindSql.mockResolvedValue({ model: A, errors: [], columns: [], upstream: {} });
    const view = render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={A} />,
    );
    await act(async () => { await sleep(500); });
    h.markerCalls.length = 0;
    await act(async () => {
      view.rerender(<Editor content="SELECT 2" language="sql" onChange={() => {}} activeFile={B} />);
      await sleep(0);
    });
    const cleared = h.markerCalls.filter((c) => c.uri === uri(A) && c.markers.length === 0);
    expect(cleared.map((c) => c.owner).sort()).toEqual(["havn-bind", "havn-lint"]);
  });

  it("cancels the debounced bind and lint when the editor unmounts", async () => {
    api.bindSql.mockResolvedValue({ model: A, errors: [], columns: [], upstream: {} });
    const view = render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={A} />,
    );
    await act(async () => { await sleep(10); });
    view.unmount();
    await act(async () => { await sleep(2000); });
    expect(api.bindSql).not.toHaveBeenCalled();
    expect(api.lintFile).not.toHaveBeenCalled();
  });
});

describe("pruneOpenModels", () => {
  it("disposes only what fell past the keep count", () => {
    for (const p of ["x", "y", "z"]) h.models.set(uri(p), h.makeModel(uri(p), p));
    const disposed = pruneOpenModels(h.monaco, ["x", "y", "z"], 2);
    expect(disposed).toEqual(["z"]);
    expect(h.models.has(uri("x"))).toBe(true);
    expect(h.models.has(uri("y"))).toBe(true);
    expect(h.models.has(uri("z"))).toBe(false);
  });

  it("clears a model's markers before letting it go", () => {
    h.models.set(uri("z"), h.makeModel(uri("z"), "z"));
    h.markerCalls.length = 0;
    pruneOpenModels(h.monaco, ["x", "z"], 1);
    expect(h.markerCalls.map((c) => c.owner).sort()).toEqual(["havn-bind", "havn-lint"]);
    expect(h.markerCalls.every((c) => c.markers.length === 0)).toBe(true);
  });

  it("does nothing for a path with no model, or without Monaco", () => {
    expect(pruneOpenModels(h.monaco, ["never-opened"], 0)).toEqual([]);
    expect(pruneOpenModels(null, ["x"], 0)).toEqual([]);
  });
});

describe("bind status across a file switch", () => {
  it("clears the previous file's error count as soon as the file changes", async () => {
    const seen = [];
    api.bindSql.mockImplementation((path) => {
      if (path === A) {
        return Promise.resolve({
          model: A,
          errors: [{ severity: "error", message: "boom", line: 1, col: 1, end_line: 1, end_col: 2 }],
          columns: [], upstream: {},
        });
      }
      return new Promise(() => {});   // B never answers
    });
    const view = render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={A} onStatus={(s) => seen.push(s)} />,
    );
    await act(async () => { await sleep(500); });
    expect(seen[seen.length - 1]).toMatchObject({ errorCount: 1 });

    await act(async () => {
      view.rerender(
        <Editor content="SELECT 2" language="sql" onChange={() => {}} activeFile={B} onStatus={(s) => seen.push(s)} />,
      );
      await sleep(0);
    });
    expect(seen[seen.length - 1]).toBeNull();
  });

  it("clears it for a file the SQL features do not apply to either", async () => {
    const seen = [];
    api.bindSql.mockResolvedValue({
      model: A,
      errors: [{ severity: "error", message: "boom", line: 1, col: 1, end_line: 1, end_col: 2 }],
      columns: [], upstream: {},
    });
    const view = render(
      <Editor content="SELECT 1" language="sql" onChange={() => {}} activeFile={A} onStatus={(s) => seen.push(s)} />,
    );
    await act(async () => { await sleep(500); });
    await act(async () => {
      view.rerender(
        <Editor content="print(1)" language="python" onChange={() => {}} activeFile="ingest/load.py" onStatus={(s) => seen.push(s)} />,
      );
      await sleep(600);
    });
    expect(seen[seen.length - 1]).toBeNull();
  });
});

describe("model retention while switching files", () => {
  it("keeps the recent files and disposes the ones before them", async () => {
    api.bindSql.mockResolvedValue({ model: A, errors: [], columns: [], upstream: {} });
    const paths = Array.from({ length: MAX_OPEN_MODELS + 2 }, (_, i) => `transform/silver/f${i}.sql`);

    const view = render(
      <Editor content="body 0" language="sql" onChange={() => {}} activeFile={paths[0]} />,
    );
    await act(async () => { await sleep(0); });
    for (let i = 1; i < paths.length; i++) {
      await act(async () => {
        view.rerender(
          <Editor content={`body ${i}`} language="sql" onChange={() => {}} activeFile={paths[i]} />,
        );
        await sleep(0);
      });
    }

    const alive = [...h.models.keys()].sort();
    expect(alive).toEqual(paths.slice(2).map(uri).sort());
    // The file on screen is never a candidate.
    expect(h.models.has(uri(paths[paths.length - 1]))).toBe(true);
  });

  it("keeps the current file's markers and restores content from props on the way back", async () => {
    api.bindSql.mockResolvedValue({
      model: A,
      errors: [{ severity: "error", message: "boom", line: 1, col: 1, end_line: 1, end_col: 2 }],
      columns: [], upstream: {},
    });
    const view = render(
      <Editor content="A body" language="sql" onChange={() => {}} activeFile={A} />,
    );
    await act(async () => { await sleep(500); });
    await act(async () => {
      view.rerender(<Editor content="B body" language="sql" onChange={() => {}} activeFile={B} />);
      await sleep(0);
    });
    // B's bind debounce only starts once the rerender's effects have run.
    await act(async () => { await sleep(600); });

    // B is on screen and still carries its own bind markers.
    const painted = h.markerCalls.filter(
      (c) => c.uri === uri(B) && c.owner === BIND_MARKER_OWNER && c.markers.length,
    );
    expect(painted.length).toBeGreaterThan(0);
    expect(h.models.get(uri(B)).isDisposed()).toBe(false);

    // Coming back to A rebuilds the model from the content prop.
    await act(async () => {
      view.rerender(<Editor content="A body" language="sql" onChange={() => {}} activeFile={A} />);
      await sleep(0);
    });
    expect(h.models.get(uri(A)).getValue()).toBe("A body");
  });
});

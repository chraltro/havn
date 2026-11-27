import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Monaco never loads in jsdom: a textarea with the same value/onChange shape.
vi.mock("@monaco-editor/react", async () => {
  const React = await import("react");
  return {
    default: ({ value, onChange }) =>
      React.createElement("textarea", { value, onChange: (e) => onChange(e.target.value) }),
  };
});

vi.mock("./api", () => ({
  api: {
    listNotebooks: vi.fn(),
    getNotebook: vi.fn(),
    saveNotebook: vi.fn(),
    runCell: vi.fn(),
  },
}));

// The panel keeps unsaved drafts at module level (they outlive a tab
// switch), so every test takes fresh copies of the panel and its api mock.
let api;
let NotebookPanel;
let hasUnsavedNotebooks;

const NOTEBOOK = {
  title: "nb",
  cells: [
    { id: "a", type: "sql", source: "SELECT 1", outputs: [] },
    { id: "b", type: "sql", source: "SELECT 2", outputs: [] },
  ],
};

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

async function openNotebook() {
  render(<NotebookPanel openPath="nb" />);
  await screen.findByText("nb");
  return screen.getAllByRole("textbox");
}

async function saveAndGetCells() {
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(api.saveNotebook).toHaveBeenCalled());
  const [, saved] = api.saveNotebook.mock.calls.at(-1);
  return Object.fromEntries(saved.cells.map((c) => [c.id, c]));
}

beforeEach(async () => {
  vi.resetModules();
  ({ api } = await import("./api"));
  ({ default: NotebookPanel, hasUnsavedNotebooks } = await import("./NotebookPanel"));
  vi.clearAllMocks();
  api.listNotebooks.mockResolvedValue([]);
  api.getNotebook.mockResolvedValue(NOTEBOOK);
  api.saveNotebook.mockResolvedValue({ status: "saved" });
});

describe("a run result landing after edits", () => {
  it("keeps what was typed into another cell while one cell ran", async () => {
    const run = deferred();
    api.runCell.mockReturnValue(run.promise);
    const [, editorB] = await openNotebook();

    await userEvent.click(screen.getAllByText("▶")[0]);
    fireEvent.change(editorB, { target: { value: "SELECT 2 + 2" } });
    await act(async () => { run.resolve({ outputs: [{ type: "text", text: "ran a" }], duration_ms: 3 }); });

    const cells = await saveAndGetCells();
    expect(cells.b.source).toBe("SELECT 2 + 2");
    expect(cells.a.outputs).toEqual([{ type: "text", text: "ran a" }]);
  });

  it("keeps edits made during Run All", async () => {
    const first = deferred();
    api.runCell
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce({ outputs: [{ type: "text", text: "ran b" }], duration_ms: 1 });
    const [, editorB] = await openNotebook();

    await userEvent.click(screen.getByRole("button", { name: "Run All" }));
    await waitFor(() => expect(api.runCell).toHaveBeenCalledTimes(1));
    fireEvent.change(editorB, { target: { value: "SELECT 22" } });
    await act(async () => { first.resolve({ outputs: [{ type: "text", text: "ran a" }], duration_ms: 2 }); });
    await screen.findByRole("button", { name: "Run All" });

    const cells = await saveAndGetCells();
    expect(cells.b.source).toBe("SELECT 22");
    expect(cells.a.outputs).toEqual([{ type: "text", text: "ran a" }]);
    expect(cells.b.outputs).toEqual([{ type: "text", text: "ran b" }]);
  });
});

describe("save feedback", () => {
  it("shows unsaved changes, then Saved", async () => {
    const [editorA] = await openNotebook();
    expect(screen.getByRole("status").textContent).toBe("");

    fireEvent.change(editorA, { target: { value: "SELECT 10" } });
    expect(screen.getByRole("status").textContent).toBe("Unsaved changes");

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved"));
  });

  it("shows a failed save while the notebook is open", async () => {
    api.saveNotebook.mockRejectedValue(new Error("Error (500): disk full"));
    const [editorA] = await openNotebook();
    fireEvent.change(editorA, { target: { value: "SELECT 10" } });

    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect((await screen.findByRole("alert")).textContent).toContain("Error (500): disk full");
    expect(screen.getByRole("status").textContent).toBe("Unsaved changes");
  });
});

describe("a result table", () => {
  it("has a sticky header inside the one bounded scroller", async () => {
    api.getNotebook.mockResolvedValue({
      title: "nb",
      cells: [{ id: "a", type: "sql", source: "SELECT 1", outputs: [{ type: "table", columns: ["x"], rows: [[1]] }] }],
    });
    await openNotebook();

    const th = screen.getByRole("columnheader", { name: "x" });
    expect(th.style.position).toBe("sticky");
    expect(th.style.top).toBe("0px");
    let scroller = th.parentElement;
    while (scroller && !/(auto|scroll)/.test(`${scroller.style.overflow} ${scroller.style.overflowX} ${scroller.style.overflowY}`)) {
      scroller = scroller.parentElement;
    }
    // The nearest scroller is the bounded output area, not an unbounded wrapper.
    expect(scroller.style.maxHeight).toBe("300px");
  });
});

describe("unsaved edits", () => {
  it("survive the panel unmounting, as on a tab switch", async () => {
    const [editorA] = await openNotebook();
    fireEvent.change(editorA, { target: { value: "SELECT 10" } });
    expect(hasUnsavedNotebooks()).toBe(true);
    // Leaving the Editor tab unmounts the panel.
    cleanup();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    const [again] = await openNotebook();
    expect(again.value).toBe("SELECT 10");
    expect(screen.getByRole("status").textContent).toBe("Unsaved changes");
    expect(api.getNotebook).toHaveBeenCalledTimes(1);
  });

  it("are dropped from the drafts once saved", async () => {
    const [editorA] = await openNotebook();
    fireEvent.change(editorA, { target: { value: "SELECT 10" } });
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved"));
    expect(hasUnsavedNotebooks()).toBe(false);
  });
});

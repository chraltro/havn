import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const h = vi.hoisted(() => ({ commands: [] }));

// Monaco never loads in jsdom: a textarea, plus the onMount call that lets
// the component register its Ctrl/Cmd+S command.
vi.mock("@monaco-editor/react", async () => {
  const React = await import("react");
  function FakeMonaco({ value, onChange, onMount }) {
    React.useEffect(() => {
      onMount?.(
        { addCommand: (keybinding, handler) => h.commands.push({ keybinding, handler }) },
        { KeyMod: { CtrlCmd: 2048 }, KeyCode: { KeyS: 49 } },
      );
    }, []);
    return React.createElement("textarea", {
      "aria-label": "Model source", value, onChange: (e) => onChange(e.target.value),
    });
  }
  return { default: FakeMonaco };
});

vi.mock("./api", () => ({
  api: {
    getModelNotebookView: vi.fn(),
    saveFile: vi.fn(),
    runTransform: vi.fn(),
  },
}));

import { api } from "./api";
import ModelNotebookView from "./ModelNotebookView";

const PATH = "transform/silver/customers.sql";
const VIEW = {
  model: "silver.customers",
  path: PATH,
  materialized: "table",
  language: "sql",
  sql_source: "SELECT 1 AS id",
  sample_data: { columns: ["id", "note"], rows: [[1, "a long note that gets cut off"], [2, null]] },
  upstream: [],
  downstream: [],
  lineage: {},
};

function pressCtrlS() {
  const command = h.commands.filter((c) => c.keybinding === (2048 | 49)).at(-1);
  command.handler();
}

async function renderView(props = {}) {
  render(<ModelNotebookView modelName="silver.customers" onClose={vi.fn()} {...props} />);
  return screen.findByLabelText("Model source");
}

beforeEach(() => {
  vi.clearAllMocks();
  h.commands.length = 0;
  api.getModelNotebookView.mockResolvedValue(VIEW);
  api.saveFile.mockResolvedValue({});
  api.runTransform.mockResolvedValue({});
});

describe("Ctrl/Cmd+S", () => {
  it("saves an edit made after the editor mounted", async () => {
    const editor = await renderView();
    fireEvent.change(editor, { target: { value: "SELECT 2 AS id" } });

    await act(async () => { pressCtrlS(); });

    expect(api.saveFile).toHaveBeenCalledWith(PATH, "SELECT 2 AS id");
    await waitFor(() => expect(screen.queryByText("modified")).toBeNull());
  });

  it("does nothing with no unsaved edits", async () => {
    await renderView();
    await act(async () => { pressCtrlS(); });
    expect(api.saveFile).not.toHaveBeenCalled();
  });
});

describe("Run", () => {
  it("saves unsaved edits before building and keeps them on screen", async () => {
    const editor = await renderView();
    fireEvent.change(editor, { target: { value: "SELECT 2 AS id" } });

    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(api.getModelNotebookView).toHaveBeenCalledTimes(2));
    expect(api.saveFile).toHaveBeenCalledWith(PATH, "SELECT 2 AS id");
    expect(api.runTransform).toHaveBeenCalledWith(["silver.customers"], false);
    expect(api.saveFile.mock.invocationCallOrder[0]).toBeLessThan(api.runTransform.mock.invocationCallOrder[0]);
    expect(screen.getByLabelText("Model source").value).toBe("SELECT 2 AS id");
  });

  it("does not build when that save fails", async () => {
    api.saveFile.mockRejectedValue(new Error("Error (403): read only"));
    const editor = await renderView();
    fireEvent.change(editor, { target: { value: "SELECT 2 AS id" } });

    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText("Error (403): read only")).toBeTruthy();
    expect(api.runTransform).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Model source").value).toBe("SELECT 2 AS id");
  });
});

describe("Close", () => {
  it("asks before discarding unsaved edits", async () => {
    const onClose = vi.fn();
    const showConfirm = vi.fn().mockResolvedValue(false);
    const editor = await renderView({ onClose, showConfirm });
    fireEvent.change(editor, { target: { value: "SELECT 2 AS id" } });

    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(
      "Unsaved changes", expect.stringContaining(PATH), "Discard", true,
    ));
    expect(onClose).not.toHaveBeenCalled();

    showConfirm.mockResolvedValue(true);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("closes straight away with nothing unsaved", async () => {
    const onClose = vi.fn();
    const showConfirm = vi.fn();
    await renderView({ onClose, showConfirm });

    await userEvent.click(screen.getByRole("button", { name: "Close" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(showConfirm).not.toHaveBeenCalled();
  });
});

describe("sample output", () => {
  it("pins the header and titles values the cell cuts off", async () => {
    await renderView();
    const th = screen.getByRole("columnheader", { name: "note" });
    expect(th.style.position).toBe("sticky");
    expect(th.style.top).toBe("0px");
    expect(screen.getByText("a long note that gets cut off")).toHaveAttribute("title", "a long note that gets cut off");
  });
});

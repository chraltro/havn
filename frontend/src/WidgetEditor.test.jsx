import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

vi.mock("./api", () => ({
  api: {
    listTables: vi.fn(() => Promise.resolve([])),
    describeTable: vi.fn(() => Promise.resolve({ columns: [] })),
    sampleTable: vi.fn(() => Promise.resolve(null)),
    runQuery: vi.fn(() => Promise.resolve({ columns: [], rows: [] })),
  },
}));
// Charts measure the DOM; the editor's save/close logic is what is under test.
vi.mock("./ChartPanel", () => ({ default: () => <div data-testid="chart" /> }));
vi.mock("./DashboardCharts", () => ({ default: () => <div data-testid="chart" /> }));

const { default: DashboardContext } = await import("./DashboardContext");
const { default: WidgetEditor } = await import("./WidgetEditor");

// An existing SQL widget opens in SQL mode: no automatic preview query.
const EXISTING = {
  id: "w1", widget_type: "table", chart_type: null, title: "Orders", sql_query: "SELECT 1",
  config: {}, position: { x: 1, y: 1, w: 8, h: 4 }, cache_ttl: 0,
};
const NEW_TEXT = {
  widget_type: "text", chart_type: null, title: "", sql_query: "", config: { page_id: "main" },
  position: { x: 1, y: 1, w: 6, h: 3 }, filters: [], cache_ttl: 0, sort_order: 0, _fromVisual: true,
};

let ctx;
let onClose;
let onSave;

// Settles the editor's mount-time table list load inside act().
async function renderEditor(widget) {
  const result = render(
    <DashboardContext.Provider value={ctx}>
      <WidgetEditor widget={widget} onClose={onClose} onSave={onSave} />
    </DashboardContext.Provider>,
  );
  await act(async () => {});
  return result;
}

beforeEach(() => {
  ctx = {
    dashboard: { id: "d1", settings: {} },
    addWidget: vi.fn(),
    updateWidget: vi.fn(),
    refreshWidget: vi.fn(),
  };
  onClose = vi.fn();
  onSave = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WidgetEditor", () => {
  it("stays open with the settings intact when the save fails", async () => {
    ctx.updateWidget.mockResolvedValue(null);
    await renderEditor(EXISTING);
    fireEvent.change(screen.getByPlaceholderText("Widget title"), { target: { value: "Orders by day" } });

    fireEvent.click(screen.getByRole("button", { name: "Save Changes" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Changes not saved");
    expect(onClose).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("Widget title")).toHaveValue("Orders by day");
  });

  it("adds the widget once on a double click", async () => {
    let resolveAdd;
    ctx.addWidget.mockImplementation(() => new Promise(r => { resolveAdd = r; }));
    await renderEditor(NEW_TEXT);
    const button = screen.getByRole("button", { name: "Add to Dashboard" });

    fireEvent.click(button);
    fireEvent.click(button);

    expect(ctx.addWidget).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    await act(async () => resolveAdd({ id: "w9" }));
    expect(ctx.refreshWidget).toHaveBeenCalledWith("w9");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape straight away when nothing changed", async () => {
    const confirm = vi.spyOn(window, "confirm");
    await renderEditor(EXISTING);

    fireEvent.keyDown(screen.getByPlaceholderText("Widget title"), { key: "Escape" });

    expect(confirm).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("asks before Escape throws edits away", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await renderEditor(EXISTING);
    const titleInput = screen.getByPlaceholderText("Widget title");
    fireEvent.change(titleInput, { target: { value: "Renamed" } });

    fireEvent.keyDown(titleInput, { key: "Escape" });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
    expect(titleInput).toHaveValue("Renamed");

    confirm.mockReturnValue(true);
    fireEvent.keyDown(titleInput, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("opens chart style sections from the keyboard", async () => {
    await renderEditor({ ...EXISTING, widget_type: "chart", chart_type: "bar" });
    const palette = screen.getByRole("button", { name: "Palette" });
    expect(palette).toHaveAttribute("aria-expanded", "false");

    fireEvent.keyDown(palette, { key: "Enter" });

    expect(palette).toHaveAttribute("aria-expanded", "true");
  });

  it("leaves keyboard focus rings alone", async () => {
    await renderEditor(EXISTING);
    expect(document.getElementById("havn-widget-editor-focus-fix")).toBeNull();
    expect(screen.getByRole("button", { name: "SQL" }).style.outline).toBe("");
    expect(screen.getByRole("button", { name: /Table/ }).style.outline).toBe("");
  });
});

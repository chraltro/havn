import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, act, waitFor, within } from "@testing-library/react";

vi.mock("./api", () => ({
  api: {
    getAutocomplete: vi.fn(() => Promise.resolve({ columns: [] })),
  },
}));
vi.mock("./ChartPanel", () => ({ default: () => <div data-testid="chart" /> }));

const { default: DashboardContext } = await import("./DashboardContext");
const { default: DashboardCanvas } = await import("./DashboardCanvas");

// The canvas picks its column count from the grid's measured width.
let gridWidth = 1400;
class FakeResizeObserver {
  constructor(cb) { this.cb = cb; }
  observe() { this.cb([{ contentRect: { width: gridWidth } }]); }
  disconnect() {}
}

const PAGES = [{ id: "p1", name: "Main" }, { id: "p2", name: "Ops" }];

let ctx;
function makeCtx(dashboard) {
  return {
    dashboard,
    editMode: true,
    setEditMode: vi.fn(),
    saveDashboard: vi.fn(() => Promise.resolve(true)),
    refreshAll: vi.fn(),
    addWidget: vi.fn(),
    removeWidget: vi.fn(),
    updateWidget: vi.fn(),
    updatePositions: vi.fn(),
    autoRefresh: 0,
    setAutoRefresh: vi.fn(),
    undo: vi.fn(),
    redo: vi.fn(),
    canUndo: false,
    canRedo: false,
    isDirty: false,
    lastSavedAt: null,
    widgetData: {},
    refreshWidget: vi.fn(),
    setCrossFilter: vi.fn(),
    showToast: vi.fn(),
  };
}

function renderCanvas(props = {}) {
  return render(
    <DashboardContext.Provider value={ctx}>
      <DashboardCanvas onBack={() => {}} onEditWidget={() => {}} {...props} />
    </DashboardContext.Provider>,
  );
}

beforeEach(() => {
  gridWidth = 1400;
  globalThis.ResizeObserver = FakeResizeObserver;
  ctx = makeCtx({
    id: "d1", name: "Sales", description: "", filters: [], settings: { pages: PAGES },
    widgets: [
      { id: "w1", widget_type: "text", title: "Ops note", config: { page_id: "p2", content: "a" }, position: { x: 1, y: 1, w: 6, h: 3 } },
      { id: "w2", widget_type: "text", title: "Ops todo", config: { page_id: "p2", content: "b" }, position: { x: 7, y: 1, w: 6, h: 3 } },
    ],
  });
});

afterEach(() => {
  delete globalThis.ResizeObserver;
  vi.restoreAllMocks();
});

describe("DashboardCanvas", () => {
  it("closes the settings dialog on Escape without leaving edit mode", async () => {
    renderCanvas();
    fireEvent.click(screen.getByTitle("Dashboard settings"));
    const dialog = screen.getByRole("dialog", { name: "Dashboard Settings" });

    // Focus elsewhere: the canvas shortcut must still stand down while a modal is open.
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(ctx.setEditMode).not.toHaveBeenCalled();

    fireEvent.keyDown(within(dialog).getByRole("button", { name: "Close" }), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(ctx.setEditMode).not.toHaveBeenCalled();
    await act(async () => {}); // the settings panel's column lookup
  });

  it("asks before deleting a page and says its widgets will be hidden", async () => {
    const showConfirm = vi.fn(() => Promise.resolve(false));
    renderCanvas({ showConfirm });

    fireEvent.contextMenu(screen.getByRole("button", { name: "Ops" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(showConfirm).toHaveBeenCalled());
    expect(showConfirm.mock.calls[0][1]).toMatch(/2 widgets will no longer be shown on any page/);
    expect(ctx.saveDashboard).not.toHaveBeenCalled();

    showConfirm.mockResolvedValueOnce(true);
    fireEvent.contextMenu(screen.getByRole("button", { name: "Ops" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(ctx.saveDashboard).toHaveBeenCalledWith({ settings: { pages: [PAGES[0]] } }));
  });

  it("stacks narrow widgets across their full height in grid rows", () => {
    gridWidth = 500;
    ctx.dashboard.settings = {};
    ctx.dashboard.widgets = [
      { id: "w1", widget_type: "text", title: "Tall", config: { content: "a" }, position: { x: 1, y: 1, w: 12, h: 5 } },
      { id: "w2", widget_type: "text", title: "Next", config: { content: "b" }, position: { x: 13, y: 1, w: 12, h: 3 } },
    ];
    renderCanvas();

    const cell = (title) => screen.getByTitle(title).closest('[style*="grid-column"]');
    expect(cell("Tall").style.gridRow).toBe("span 5");
    expect(cell("Tall").style.minHeight).toBe("0px");
    expect(cell("Next").style.gridRow).toBe("span 3");
  });

  it("keeps an embedded canvas in the page flow instead of pinning it over the filter bar", () => {
    const { container } = renderCanvas({ embedMode: true });
    expect(container.firstChild.style.position).toBe("");
    expect(container.firstChild.style.flex).toMatch(/^1/);
  });
});

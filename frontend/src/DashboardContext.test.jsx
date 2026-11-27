import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, act } from "@testing-library/react";

const getDashboard = vi.fn();
const updateDashboard = vi.fn();
const deleteWidget = vi.fn();
const updateWidget = vi.fn();

vi.mock("./api", () => ({
  api: {
    getDashboard: (...a) => getDashboard(...a),
    updateDashboard: (...a) => updateDashboard(...a),
    deleteWidget: (...a) => deleteWidget(...a),
    updateWidget: (...a) => updateWidget(...a),
  },
}));

const { DashboardProvider, useDashboard } = await import("./DashboardContext");

const DASH = {
  id: "d1", name: "Sales", description: "", layout: {}, filters: [], settings: {},
  widgets: [{ id: "w1", widget_type: "text", title: "Note", config: {}, position: { x: 1, y: 1, w: 6, h: 2 } }],
};

let ctx;
function Probe() {
  ctx = useDashboard();
  return null;
}

async function open() {
  render(<DashboardProvider><Probe /></DashboardProvider>);
  await act(() => ctx.loadDashboard("d1"));
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, "error").mockImplementation(() => {});
  getDashboard.mockResolvedValue(structuredClone(DASH));
  updateDashboard.mockResolvedValue({ id: "d1" });
  deleteWidget.mockResolvedValue({ status: "deleted" });
});

describe("undo", () => {
  it("says a widget change can't be undone instead of pretending to restore it", async () => {
    await open();
    await act(() => ctx.removeWidget("w1"));
    expect(ctx.canUndo).toBe(true);

    await act(() => ctx.undo());

    // PUT /api/dashboards has no widgets field: nothing may claim to bring the widget back.
    expect(updateDashboard).not.toHaveBeenCalled();
    expect(ctx.dashboard.widgets).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("can't be undone");
    expect(ctx.canUndo).toBe(false);
  });

  it("writes back only dashboard fields, keeping the widgets and views saved since", async () => {
    await open();
    await act(() => ctx.saveDashboard({ name: "Revenue" }));
    await act(() => ctx.saveView("Q3"));
    updateDashboard.mockClear();

    await act(() => ctx.undo());

    const [id, body] = updateDashboard.mock.calls[0];
    expect(id).toBe("d1");
    expect(body.name).toBe("Sales");
    expect(body).not.toHaveProperty("widgets");
    expect(body.settings.saved_views.map(v => v.name)).toEqual(["Q3"]);
    expect(ctx.dashboard.name).toBe("Sales");
    expect(ctx.dashboard.widgets).toHaveLength(1);
    expect(ctx.canRedo).toBe(true);
  });

  it("puts the change back and reports it when the undo can't be saved", async () => {
    await open();
    await act(() => ctx.saveDashboard({ name: "Revenue" }));
    updateDashboard.mockRejectedValueOnce(new Error("Error (500): locked"));

    await act(() => ctx.undo());

    expect(ctx.dashboard.name).toBe("Revenue");
    expect(ctx.canUndo).toBe(true);
    expect(ctx.canRedo).toBe(false);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't undo: Error (500): locked");
  });
});

describe("failed writes", () => {
  it("reports a failed widget update and resolves null", async () => {
    await open();
    updateWidget.mockRejectedValueOnce(new Error("Error (403): not allowed"));

    let result;
    await act(async () => { result = await ctx.updateWidget("w1", { title: "x" }); });

    expect(result).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't save the widget: Error (403): not allowed");
    expect(ctx.canUndo).toBe(false);
  });

  it("rolls back a saved view the server refused", async () => {
    await open();
    updateDashboard.mockRejectedValueOnce(new Error("boom"));

    await act(() => ctx.saveView("Mine"));

    expect(ctx.savedViews).toEqual([]);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't save the view: boom");
  });

  it("resolves false from a failed dashboard save and leaves nothing to undo", async () => {
    await open();
    updateDashboard.mockRejectedValueOnce(new Error("boom"));

    let ok;
    await act(async () => { ok = await ctx.saveDashboard({ name: "Nope" }); });

    expect(ok).toBe(false);
    expect(ctx.dashboard.name).toBe("Sales");
    expect(ctx.canUndo).toBe(false);
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("./api", () => ({ api: {} }));
vi.mock("./ChartPanel", () => ({ default: () => <div data-testid="chart" /> }));

const { default: DashboardContext } = await import("./DashboardContext");
const { default: DashboardWidget } = await import("./DashboardWidget");

let ctx;
function renderWidget(widget) {
  return render(
    <DashboardContext.Provider value={ctx}>
      <DashboardWidget widget={widget} style={{ height: "100%" }} />
    </DashboardContext.Provider>,
  );
}

beforeEach(() => {
  ctx = {
    dashboard: { id: "d1" },
    editMode: true,
    widgetData: {},
    refreshWidget: vi.fn(),
    setCrossFilter: vi.fn(),
    updateWidget: vi.fn(),
    showToast: vi.fn(),
  };
});

describe("DashboardWidget", () => {
  it("closes fullscreen on Escape without the keypress reaching the canvas", () => {
    const canvasShortcut = vi.fn();
    document.addEventListener("keydown", canvasShortcut);
    try {
      renderWidget({ id: "w1", widget_type: "text", title: "Note", config: { content: "Hello" }, position: { h: 4 } });
      fireEvent.click(screen.getByTitle(/Expand to fullscreen/));
      expect(screen.getByRole("button", { name: "Close fullscreen" })).toBeInTheDocument();

      fireEvent.keyDown(document.body, { key: "Escape" });

      expect(screen.queryByRole("button", { name: "Close fullscreen" })).toBeNull();
      expect(canvasShortcut).not.toHaveBeenCalled();
    } finally {
      document.removeEventListener("keydown", canvasShortcut);
    }
  });

  it("names the pager buttons and lets the table own the only scroller", () => {
    const rows = Array.from({ length: 120 }, (_, i) => [i]);
    ctx.widgetData = { t1: { columns: ["n"], rows, row_count: rows.length } };
    const { container } = renderWidget({ id: "t1", widget_type: "table", title: "Numbers", sql_query: "SELECT n", config: {}, position: { h: 6 } });

    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByText("2 / 3")).toBeInTheDocument();

    // A sticky header pins to its nearest scroller: there must be exactly one
    // between the table and the widget, the table's own.
    const scrollers = [];
    for (let el = container.querySelector("table").parentElement; el && el !== container; el = el.parentElement) {
      if (["auto", "scroll"].includes(el.style.overflow) || ["auto", "scroll"].includes(el.style.overflowY)) scrollers.push(el);
    }
    expect(scrollers).toHaveLength(1);
  });
});

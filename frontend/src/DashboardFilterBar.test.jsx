import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const runQuery = vi.fn();
vi.mock("./api", () => ({ api: { runQuery: (...a) => runQuery(...a) } }));

const { default: DashboardContext } = await import("./DashboardContext");
const { default: DashboardFilterBar } = await import("./DashboardFilterBar");

let ctx;
function renderBar(filters, extra = {}) {
  ctx = {
    dashboard: { id: "d1", filters, settings: {} },
    globalFilters: {},
    setFilter: vi.fn(),
    parameters: {},
    setParameter: vi.fn(),
    clearCrossFilter: vi.fn(),
    crossFilter: null,
    savedViews: [],
    saveView: vi.fn(),
    loadView: vi.fn(),
    deleteView: vi.fn(),
    ...extra,
  };
  return render(
    <DashboardContext.Provider value={ctx}>
      <DashboardFilterBar />
    </DashboardContext.Provider>,
  );
}

// The options cache is shared across mounts, so each test uses its own SQL.
const dropdown = (sql) => [{ id: "f1", label: "Region", type: "dropdown", column: "region", options_sql: sql }];

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DashboardFilterBar", () => {
  it("asks before deleting a saved view", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderBar([], { savedViews: [{ id: "v1", name: "Q3" }] });
    fireEvent.click(screen.getByRole("button", { name: /Views/ }));

    fireEvent.click(screen.getByRole("button", { name: "Delete view Q3" }));
    expect(confirm).toHaveBeenCalledWith('Delete the saved view "Q3"?');
    expect(ctx.deleteView).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Delete view Q3" }));
    expect(ctx.deleteView).toHaveBeenCalledWith("v1");
  });

  it("says when filter options failed to load, and retries", async () => {
    runQuery
      .mockRejectedValueOnce(new Error("Error (400): table not found"))
      .mockResolvedValueOnce({ columns: ["region"], rows: [["north"]] });
    renderBar(dropdown("SELECT DISTINCT region FROM a ORDER BY 1"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Options didn't load");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("option", { name: "north" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reuses cached options for a minute, then loads them again", async () => {
    const sql = "SELECT DISTINCT region FROM b ORDER BY 1";
    runQuery.mockResolvedValue({ columns: ["region"], rows: [["north"]] });
    const first = renderBar(dropdown(sql));
    await screen.findByRole("option", { name: "north" });
    first.unmount();

    const second = renderBar(dropdown(sql));
    await screen.findByRole("option", { name: "north" });
    expect(runQuery).toHaveBeenCalledTimes(1);
    second.unmount();

    const later = Date.now() + 61 * 1000;
    vi.spyOn(Date, "now").mockReturnValue(later);
    runQuery.mockResolvedValue({ columns: ["region"], rows: [["north"], ["south"]] });
    renderBar(dropdown(sql));
    expect(await screen.findByRole("option", { name: "south" })).toBeInTheDocument();
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));
  });

  it("names the date range inputs", () => {
    renderBar([{ id: "f2", label: "Order date", type: "date_range", column: "order_date" }]);
    expect(screen.getByLabelText("Order date from")).toHaveAttribute("type", "date");
    expect(screen.getByLabelText("Order date to")).toHaveAttribute("type", "date");
  });
});

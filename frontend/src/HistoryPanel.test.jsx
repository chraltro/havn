import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

const getPipelineRuns = vi.fn();
const getHistory = vi.fn();
const getPipelineRunDetail = vi.fn();

vi.mock("./api", () => ({
  api: {
    getPipelineRuns: (...args) => getPipelineRuns(...args),
    getHistory: (...args) => getHistory(...args),
    getPipelineRunDetail: (...args) => getPipelineRunDetail(...args),
    getRunComparison: () => Promise.resolve(null),
  },
}));

const { default: HistoryPanel } = await import("./HistoryPanel");

const RUN = {
  pipeline_run_id: "p1", run_type: "transform", target: "silver.orders", targets: ["silver.orders"],
  model_count: 2, total_duration_ms: 1200, status: "error", error_count: 1, started_at: null,
};

beforeEach(() => {
  for (const fn of [getPipelineRuns, getHistory, getPipelineRunDetail]) fn.mockReset();
  getPipelineRuns.mockResolvedValue([RUN]);
  getHistory.mockResolvedValue([
    { run_id: "a", run_type: "transform", target: "silver.orders", status: "error", error: "Binder Error: column x not found", started_at: null },
  ]);
});

describe("HistoryPanel", () => {
  it("expands a run from the keyboard and says why its detail did not load", async () => {
    getPipelineRunDetail.mockRejectedValue(new Error("database is locked"));
    render(<HistoryPanel onOpenFile={vi.fn()} />);

    const row = await screen.findByRole("button", { name: /silver\.orders/ });
    expect(row.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(row, { key: "Enter" });
    expect(row.getAttribute("aria-expanded")).toBe("true");

    expect((await screen.findByRole("alert")).textContent).toContain("database is locked");
    expect(screen.queryByText("Loading...")).toBeNull();
  });

  it("sorts the grouped view from buttons, with a header cell for every row cell", async () => {
    render(<HistoryPanel onOpenFile={vi.fn()} />);
    const sortByModels = await screen.findByRole("button", { name: "Sort by Models" });
    fireEvent.click(sortByModels);
    expect(screen.getByRole("button", { name: "Sort by Models, ascending" })).toBe(sortByModels);

    // The rows have an error-count cell before Time; the header needs the same
    // so the labels line up with their columns.
    const header = sortByModels.parentElement;
    const row = screen.getByRole("button", { name: /silver\.orders/ });
    // The row also starts with the expand arrow, which the header's left padding stands in for.
    expect(header.children.length).toBe(row.children.length - 1);
  });

  it("sorts the flat view from header buttons and shows a long error in full on hover", async () => {
    getPipelineRuns.mockResolvedValue([]);
    render(<HistoryPanel onOpenFile={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "All entries" }));

    const status = screen.getByRole("button", { name: "Status" });
    fireEvent.click(status);
    expect(status.closest("th").getAttribute("aria-sort")).toBe("ascending");
    expect(screen.getByTitle("Binder Error: column x not found")).toBeTruthy();
  });
});

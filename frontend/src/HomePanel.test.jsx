import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getHome = vi.fn();
vi.mock("./api", () => ({ api: { getHome: (...a) => getHome(...a) } }));

const { default: HomePanel, fmtDuration, fmtBytes, runLabel } = await import("./HomePanel");

const HOME = {
  project_name: "harbour",
  is_sample: false,
  has_data: true,
  tiles: {
    models: { total: 5, changed: 1, never_built: 1, up_to_date: 3 },
    checks: { passed: 10, failed: 1, warned: 1, contracts_failed: 0 },
    last_run: { pipeline_run_id: "r1", target: "full-refresh", started_at: null, status: "failed", duration_ms: 108000, model_count: 5, error_count: 1, rows: 2100 },
    warehouse: { size_bytes: 1932735283, last_backup: null },
  },
  attention: [
    { kind: "assertion", severity: "error", title: "Check failed in silver.orders", subject: "silver.orders",
      detail: "no_nulls(customer_id) · 14 nulls", at: null, path: "transform/silver/orders.sql",
      sql: 'SELECT * FROM silver.orders WHERE "customer_id" IS NULL' },
    { kind: "anomaly", severity: "warn", title: "Unusual row count in gold.revenue", subject: "gold.revenue",
      detail: "row count dropped", at: null, path: null, sql: null },
  ],
  attention_total: 2,
  runs: [
    { pipeline_run_id: "r0", target: "full-refresh", started_at: null, status: "success", duration_ms: 50000, model_count: 5, error_count: 0, rows: 10 },
    { pipeline_run_id: "r1", target: "full-refresh", started_at: null, status: "failed", duration_ms: 108000, model_count: 5, error_count: 1, rows: 10 },
  ],
  layers: [
    { schema: "silver", models: [
      { name: "orders", full_name: "silver.orders", path: "transform/silver/orders.sql", status: "failing", materialized: "table", last_run_at: null, row_count: 3 },
      { name: "dim", full_name: "silver.dim", path: "transform/silver/dim.sql", status: "fresh", materialized: "table", last_run_at: null, row_count: 3 },
    ] },
  ],
};

function renderHome(props = {}) {
  const handlers = {
    onNavigate: vi.fn(), onOpenFile: vi.fn(), onRunPipeline: vi.fn(), onQuery: vi.fn(),
    onClearSample: vi.fn(), onAttentionCount: vi.fn(),
  };
  render(<HomePanel running={false} refreshKey={0} firstRun={<div>first run</div>} {...handlers} {...props} />);
  return handlers;
}

describe("HomePanel", () => {
  beforeEach(() => { getHome.mockReset(); getHome.mockResolvedValue(HOME); });

  it("names a run by its one target or its step count", () => {
    expect(runLabel({ model_count: 1, target: "gold.orders" })).toBe("gold.orders");
    expect(runLabel({ model_count: 6, target: "bronze.x" })).toBe("6 steps");
  });

  it("formats durations and sizes", () => {
    expect(fmtDuration(38)).toBe("38 ms");
    expect(fmtDuration(108000)).toBe("1m 48s");
    expect(fmtBytes(1932735283)).toEqual({ value: "1.8", unit: "GB" });
  });

  it("renders tiles and reports the error count", async () => {
    const h = renderHome();
    await screen.findByText("Pipeline health");
    expect(screen.getByText("1m 48s")).toBeTruthy();
    expect(screen.getByText("1 failing")).toBeTruthy();
    expect(screen.getByText("no backup yet")).toBeTruthy();
    expect(h.onAttentionCount).toHaveBeenCalledWith(1);
  });

  it("offers the next action on each attention item", async () => {
    const h = renderHome();
    await screen.findByText("Check failed in silver.orders");
    fireEvent.click(screen.getByText("See rows"));
    expect(h.onQuery).toHaveBeenCalledWith(HOME.attention[0].sql);
    fireEvent.click(screen.getByText("Open"));
    expect(h.onOpenFile).toHaveBeenCalledWith("transform/silver/orders.sql");
    fireEvent.click(screen.getByText("Quality"));
    expect(h.onNavigate).toHaveBeenCalledWith("Quality");
  });

  it("opens a model from its layer chip", async () => {
    const h = renderHome();
    await screen.findByText("Pipeline health");
    fireEvent.click(screen.getByTitle(/silver\.orders · failing/));
    expect(h.onOpenFile).toHaveBeenCalledWith("transform/silver/orders.sql");
  });

  it("says so when nothing needs attention", async () => {
    getHome.mockResolvedValue({ ...HOME, attention: [], attention_total: 0 });
    renderHome();
    expect(await screen.findByText("Nothing needs attention")).toBeTruthy();
  });

  it("shows the first-run panel before there is any data", async () => {
    getHome.mockResolvedValue({ ...HOME, has_data: false });
    renderHome();
    expect(await screen.findByText("first run")).toBeTruthy();
  });

  it("offers a retry when loading fails", async () => {
    getHome.mockRejectedValueOnce(new Error("boom"));
    renderHome();
    fireEvent.click(await screen.findByText("Retry"));
    await waitFor(() => expect(getHome).toHaveBeenCalledTimes(2));
  });
});

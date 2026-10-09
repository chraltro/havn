import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getPerfSummary = vi.fn();
const getPerfModel = vi.fn();
const getPerfBuild = vi.fn();
const getPerfAdvice = vi.fn();
const setPerfAdviceState = vi.fn();
const getPerfCriticalPath = vi.fn();

vi.mock("./api", () => ({
  api: {
    getPerfSummary: (...a) => getPerfSummary(...a),
    getPerfModel: (...a) => getPerfModel(...a),
    getPerfBuild: (...a) => getPerfBuild(...a),
    getPerfAdvice: (...a) => getPerfAdvice(...a),
    setPerfAdviceState: (...a) => setPerfAdviceState(...a),
    getPerfCriticalPath: (...a) => getPerfCriticalPath(...a),
  },
}));

const { default: PerformancePanel, fmtMs, fmtRows } = await import("./PerformancePanel");

const ADVICE = {
  key: "order_by_non_final:silver.sorted",
  rule: "order_by_non_final",
  model: "silver.sorted",
  severity: "low",
  title: "ORDER BY in a model other models read",
  explanation: "silver.sorted ends with ORDER BY k.",
  suggestion: "Remove the ORDER BY here.",
  evidence: { order_by: "ORDER BY k" },
  status: "open",
  snoozed_until: null,
};

const SUMMARY = {
  slowest: [
    { model_path: "gold.orders", materialized: "table", builds: 4, median_ms: 2400, last_ms: 2600, max_ms: 3000, last_rows: 1200000, peak_memory_bytes: 1048576 },
  ],
  trend: { "gold.orders": [{ duration_ms: 2000 }, { duration_ms: 2400 }, { duration_ms: 2600 }] },
  regressions: [
    { id: "r1", model_path: "gold.orders", ratio: 3.1, message: "gold.orders took 7.4 s, 3.1x its median", detected_at: "2026-10-09T10:00:00",
      plan_diff: { summary: ["HASH_JOIN got 5.0 s slower"], operators: [{ label: "HASH_JOIN (a = b)", fast_ms: 200, slow_ms: 5200, delta_ms: 5000, fast_rows: 10, slow_rows: 10 }] } },
  ],
  advice: [ADVICE],
  runs: [{ pipeline_run_id: "run-1", started_at: "2026-10-09T10:00:00", builds: 2, wall_ms: 3000 }],
  settings: { enabled: true, capture_plans: "true", sample_rate: 0.25 },
};

const CP = {
  wall_ms: 3000, busy_ms: 3100, parallelism: 1.03, wait_ms: 0, longest_chain_ms: 2900, tiers: 2,
  path: [{ model: "bronze.orders" }, { model: "gold.orders" }],
  models: [
    { model: "bronze.orders", start_offset_ms: 0, duration_ms: 400, tier: 0, status: "success", on_path: true },
    { model: "gold.orders", start_offset_ms: 400, duration_ms: 2600, tier: 1, status: "success", on_path: true },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  getPerfSummary.mockResolvedValue(SUMMARY);
  getPerfCriticalPath.mockResolvedValue(CP);
  getPerfAdvice.mockResolvedValue([]);
  setPerfAdviceState.mockResolvedValue({});
});

describe("formatters", () => {
  it("formats durations and row counts", () => {
    expect(fmtMs(450)).toBe("450 ms");
    expect(fmtMs(2400)).toBe("2.4 s");
    expect(fmtMs(125000)).toBe("2.1 min");
    expect(fmtRows(1200000)).toBe("1.2M");
  });
});

describe("PerformancePanel", () => {
  it("shows slowest models, regressions, advice and the critical path", async () => {
    render(<PerformancePanel />);
    expect(await screen.findByText("gold.orders took 7.4 s, 3.1x its median")).toBeTruthy();
    expect(screen.getAllByText("gold.orders").length).toBeGreaterThan(0);
    expect(screen.getByText("ORDER BY in a model other models read")).toBeTruthy();
    await waitFor(() => expect(getPerfCriticalPath).toHaveBeenCalledWith("run-1"));
    expect(await screen.findByText(/No number of workers gets it under/)).toBeTruthy();
    expect(screen.getByText("Plans: every build")).toBeTruthy();
  });

  it("opens the plan diff of a regression", async () => {
    render(<PerformancePanel />);
    fireEvent.click(await screen.findByText("Plan diff"));
    expect(screen.getByText("HASH_JOIN got 5.0 s slower")).toBeTruthy();
    expect(screen.getByText("+5.0 s")).toBeTruthy();
  });

  it("dismisses and snoozes advice", async () => {
    render(<PerformancePanel />);
    fireEvent.click(await screen.findByText("Dismiss"));
    await waitFor(() =>
      expect(setPerfAdviceState).toHaveBeenCalledWith("silver.sorted", "order_by_non_final", "dismissed", null)
    );
    await waitFor(() => expect(screen.queryByText("ORDER BY in a model other models read")).toBeNull());
  });

  it("snoozes for seven days", async () => {
    render(<PerformancePanel />);
    fireEvent.click(await screen.findByText("Snooze 7 days"));
    await waitFor(() =>
      expect(setPerfAdviceState).toHaveBeenCalledWith("silver.sorted", "order_by_non_final", "snoozed", 7)
    );
  });

  it("drills into a model and loads another build's plan", async () => {
    getPerfModel.mockResolvedValue({
      model: "gold.orders",
      history: [
        { id: "b2", status: "success", duration_ms: 2600, rows_out: 10, plan_captured: true, finished_at: "2026-10-09T10:05:00", materialized: "table" },
        { id: "b1", status: "success", duration_ms: 2000, rows_out: 10, plan_captured: true, finished_at: "2026-10-09T09:05:00", materialized: "table" },
      ],
      plan: { operator: "HASH_JOIN", actual_rows: 10, actual_time_ms: 50, _total_time_ms: 50 },
      plan_build: { id: "b2" },
      regressions: [],
      advice: [],
    });
    getPerfBuild.mockResolvedValue({ id: "b1", plan: { operator: "SEQ_SCAN", table: "bronze.orders", actual_rows: 5, actual_time_ms: 9, _total_time_ms: 9 } });
    render(<PerformancePanel />);
    fireEvent.click((await screen.findAllByText("gold.orders"))[0]);
    expect(await screen.findByText("HASH_JOIN")).toBeTruthy();
    expect(screen.getByText("Median build")).toBeTruthy();
    // The second bar from the right is the older build b1.
    const bars = document.querySelectorAll("svg[aria-label='Build durations'] rect[fill='transparent']");
    fireEvent.click(bars[0]);
    await waitFor(() => expect(getPerfBuild).toHaveBeenCalledWith("b1"));
    expect(await screen.findByText("SEQ_SCAN")).toBeTruthy();
    fireEvent.click(screen.getByText(/All models/));
    expect(await screen.findByText("ORDER BY in a model other models read")).toBeTruthy();
  });

  it("explains an empty warehouse", async () => {
    getPerfSummary.mockResolvedValue({ ...SUMMARY, slowest: [], regressions: [], advice: [], runs: [], trend: {} });
    render(<PerformancePanel />);
    expect(await screen.findByText(/No builds recorded in this window/)).toBeTruthy();
    expect(screen.getByText("No runs recorded yet.")).toBeTruthy();
  });
});

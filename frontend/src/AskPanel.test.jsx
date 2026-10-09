import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getAskStatus = vi.fn();
const ask = vi.fn();
const acceptSuggestedMetric = vi.fn();
const applyChangeSet = vi.fn();

vi.mock("./api", () => ({
  api: {
    getAskStatus: (...a) => getAskStatus(...a),
    ask: (...a) => ask(...a),
    acceptSuggestedMetric: (...a) => acceptSuggestedMetric(...a),
    applyChangeSet: (...a) => applyChangeSet(...a),
    verifyChangeSet: vi.fn(),
    discardChangeSet: vi.fn(),
  },
}));

// ChartPanel measures the DOM; the panel's own logic is what is under test.
vi.mock("./ChartPanel", () => ({ default: ({ forcedType }) => <div data-testid="chart">{forcedType}</div> }));

const { default: AskPanel, pivotForChart } = await import("./AskPanel");
const { default: ChangeSetCard } = await import("./ChangeSetCard");

const ANSWER = {
  status: "answered",
  question: "revenue by region",
  explanation: "Revenue per region",
  spec: { metrics: ["revenue"], dimensions: ["region"], filters: [] },
  sql: "SELECT region, SUM(amount) AS revenue FROM gold.orders GROUP BY 1",
  result: { columns: ["region", "revenue"], column_types: ["VARCHAR", "DOUBLE"], rows: [["north", 150], ["south", 30]], truncated: false },
  chart: { type: "bar", x: "region", y: ["revenue"], series: null },
  metrics: [{ name: "revenue", measure: "SUM(amount)", model: "gold.orders", filters: [], source_path: "sales.yml" }],
  lineage: [{ root: "gold.orders", nodes: [{ name: "gold.orders" }, { name: "landing.orders" }], sources: ["landing.orders"] }],
  freshness: [{ model: "gold.orders", last_run_at: "2026-10-09 10:00", hours_since_run: 1, is_stale: true }],
  warnings: ["gold.orders is stale"],
};

beforeEach(() => {
  vi.clearAllMocks();
  getAskStatus.mockResolvedValue({ configured: true, metrics: 3, provider: "openai", model: "qwen", is_local: true });
});

describe("AskPanel", () => {
  it("shows the answer with chart, spec and provenance, and sends history on follow-ups", async () => {
    ask.mockResolvedValueOnce(ANSWER).mockResolvedValueOnce({ ...ANSWER, spec: { ...ANSWER.spec, grain: "month" } });
    render(<AskPanel />);
    await screen.findByText(/on this machine/);
    const input = screen.getByLabelText("Question");
    fireEvent.change(input, { target: { value: "revenue by region" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByText("Verified")).toBeInTheDocument();
    expect(screen.getByTestId("chart").textContent).toBe("bar");
    expect(screen.getByText(/gold.orders is stale/)).toBeInTheDocument();
    fireEvent.click(screen.getByText(/How this was answered/));
    expect(screen.getByText(ANSWER.sql)).toBeInTheDocument();
    expect(screen.getByText(/gold.orders ← landing.orders/)).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "now by month" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(ask).toHaveBeenCalledTimes(2));
    expect(ask.mock.calls[1][1]).toEqual([{ question: "revenue by region", spec: ANSWER.spec }]);
  });

  it("explains an unanswerable question and can save the suggested metric", async () => {
    ask.mockResolvedValue({
      status: "unanswerable", question: "avg order", explanation: "No metric measures averages.",
      closest_metrics: [{ name: "revenue", description: "" }],
      suggested_metric: { yaml: "metrics:\n- name: aov\n", path: "metrics/aov.yml", definition: { name: "aov" }, errors: [] },
      warnings: [],
    });
    acceptSuggestedMetric.mockResolvedValue({ path: "metrics/aov.yml" });
    render(<AskPanel />);
    await screen.findByText(/on this machine/);
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "avg order" } });
    fireEvent.click(screen.getByText("Ask"));
    expect(await screen.findByText(/No defined metric answers this/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Add to metrics/"));
    expect(await screen.findByText(/Saved metrics\/aov.yml/)).toBeInTheDocument();
    expect(acceptSuggestedMetric).toHaveBeenCalledWith({ name: "aov" }, "metrics/aov.yml");
  });

  it("pivots a series dimension into one column per value", () => {
    const out = pivotForChart(
      { columns: ["month", "region", "revenue"], rows: [["2026-01", "n", 1], ["2026-01", "s", 2], ["2026-02", "n", 3]] },
      { x: "month", y: ["revenue"], series: "region" },
    );
    expect(out.columns).toEqual(["month", "n", "s"]);
    expect(out.rows).toEqual([["2026-01", 1, 2], ["2026-02", 3, null]]);
  });
});

describe("ChangeSetCard", () => {
  const base = {
    id: "abc123", revision: 1, source: "agent:claude", title: "only paid", status: "ready",
    files: [{ path: "transform/bronze/orders.sql", action: "modify" }], ignored: [], stale_files: [],
    report: {
      ok: true,
      checks: [{ name: "unit_tests", status: "pass", summary: "1 test(s) passed", details: [] }],
      diffs: [{ model: "bronze.orders", total_before: 4, total_after: 3, added: 0, removed: 1, modified: 0, schema_changes: [] }],
    },
  };

  it("offers Apply only when ready", async () => {
    applyChangeSet.mockResolvedValue({ ...base, status: "applied" });
    render(<ChangeSetCard changeset={base} />);
    expect(screen.getByText("Ready to apply")).toBeInTheDocument();
    expect(screen.getByText(/4 → 3 rows/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Apply"));
    expect(await screen.findByText("Applied")).toBeInTheDocument();
    expect(applyChangeSet).toHaveBeenCalledWith("abc123");
  });

  it("shows failures and hides Apply when verification failed", () => {
    const failed = {
      ...base, status: "failed",
      report: { ok: false, checks: [{ name: "bind", status: "fail", summary: "1 bind error(s)", details: [{ model: "gold.x", message: "column nope not found" }] }], diffs: [] },
    };
    render(<ChangeSetCard changeset={failed} />);
    expect(screen.getByText("Verification failed")).toBeInTheDocument();
    expect(screen.getByText(/column nope not found/)).toBeInTheDocument();
    expect(screen.queryByText("Apply")).toBeNull();
    expect(screen.getByText("Apply anyway")).toBeInTheDocument();
  });
});

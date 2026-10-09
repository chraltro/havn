import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getLiveStatus = vi.fn();
const pauseLiveModel = vi.fn();
const resumeLiveModel = vi.fn();
const refreshLiveModel = vi.fn();
const startLiveRunner = vi.fn();
const stopLiveRunner = vi.fn();
let emit = null;

vi.mock("./api", () => ({
  api: {
    getLiveStatus: (...a) => getLiveStatus(...a),
    pauseLiveModel: (...a) => pauseLiveModel(...a),
    resumeLiveModel: (...a) => resumeLiveModel(...a),
    refreshLiveModel: (...a) => refreshLiveModel(...a),
    startLiveRunner: (...a) => startLiveRunner(...a),
    stopLiveRunner: (...a) => stopLiveRunner(...a),
    streamLiveEvents: (onEvent) => { emit = onEvent; return () => { emit = null; }; },
  },
}));

const { default: LivePanel, fmtLag, fmtAgo } = await import("./LivePanel");
const { parseSSEFrame } = await vi.importActual("./api");

const STATUS = {
  runner: { running: true, refreshes: 12, cycles: 30 },
  settings: { max_lag: 300 },
  max_lag_seconds: 4.2,
  sources: [
    { source: "landing.orders", kind: "source", watermark: 340, rows_total: 340,
      advanced_at: new Date().toISOString(), events_per_second: 2.5, consumers: ["bronze.orders"] },
  ],
  models: [
    { model: "bronze.orders", materialized: "incremental", strategy: "merge", cdc: true,
      status: "live", lag_seconds: 0, events_per_second: 2.5, refreshes: 12,
      last_refresh_at: new Date().toISOString(), last_duration_ms: 14, last_lag_ms: 800,
      inputs: [{ source: "landing.orders", consumed: 340, watermark: 340, behind: 0 }] },
    { model: "silver.orders", materialized: "view", status: "live", lag_seconds: 0, inputs: [] },
    { model: "gold.by_region", materialized: "incremental", strategy: "delete+insert",
      status: "failing", lag_seconds: 4.2, consecutive_failures: 2,
      next_retry_at: new Date(Date.now() + 8000).toISOString(),
      last_error: "assert row_count > 0: 0 rows", refreshes: 3,
      inputs: [{ source: "bronze.orders", consumed: 330, watermark: 340, behind: 10 }] },
  ],
};

describe("LivePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getLiveStatus.mockResolvedValue(STATUS);
    pauseLiveModel.mockResolvedValue({});
    refreshLiveModel.mockResolvedValue({});
  });

  it("lists live models with status, lag and the failure", async () => {
    render(<LivePanel />);
    expect(await screen.findByText("bronze.orders", { selector: "div" })).toBeTruthy();
    expect(screen.getByText("runner up")).toBeTruthy();
    expect(screen.getByText("incremental · merge · cdc ← landing.orders")).toBeTruthy();
    expect(screen.getByText("failing")).toBeTruthy();
    expect(screen.getByText("4.2s")).toBeTruthy();
    expect(screen.getByText("assert row_count > 0: 0 rows")).toBeTruthy();
    expect(screen.getByText(/retry in/)).toBeTruthy();
    expect(screen.getByText("reads live")).toBeTruthy();
  });

  it("pauses a model and retries a failing one", async () => {
    render(<LivePanel />);
    await screen.findByText("gold.by_region", { selector: "div" });
    fireEvent.click(screen.getAllByText("Pause")[0]);
    await waitFor(() => expect(pauseLiveModel).toHaveBeenCalledWith("bronze.orders"));
    fireEvent.click(screen.getByText("Retry now"));
    await waitFor(() => expect(refreshLiveModel).toHaveBeenCalledWith("gold.by_region"));
  });

  it("shows runner events in the activity feed", async () => {
    render(<LivePanel />);
    await screen.findByText("bronze.orders", { selector: "div" });
    emit("refresh", { model: "bronze.orders", status: "built", events: 3, duration_ms: 9, lag_ms: 420, ts: Date.now() / 1000 });
    expect(await screen.findByText("bronze.orders refreshed · 3 events · 9ms · lag 420ms")).toBeTruthy();
  });

  it("explains how to opt in when there are no live models", async () => {
    getLiveStatus.mockResolvedValue({ ...STATUS, models: [], sources: [], runner: { running: false } });
    render(<LivePanel />);
    expect(await screen.findByText("No live models yet")).toBeTruthy();
    expect(screen.getByText("Start runner")).toBeTruthy();
  });
});

describe("formatting", () => {
  it("formats lag", () => {
    expect(fmtLag(0)).toBe("0s");
    expect(fmtLag(0.25)).toBe("250ms");
    expect(fmtLag(3.14)).toBe("3.1s");
    expect(fmtLag(600)).toBe("10m");
    expect(fmtAgo(null, Date.now())).toBe("never");
  });

  it("parses SSE frames", () => {
    expect(parseSSEFrame("id: 4\nevent: refresh\ndata: {\"a\":1}")).toEqual({ id: "4", event: "refresh", data: { a: 1 } });
    expect(parseSSEFrame(": keepalive")).toBeNull();
  });
});

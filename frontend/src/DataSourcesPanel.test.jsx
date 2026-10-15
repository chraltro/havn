import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

const listConfiguredConnectors = vi.fn();
const uploadFile = vi.fn();
const regenerateConnector = vi.fn();
const resetCDCWatermark = vi.fn();
const getCDCStatus = vi.fn();

vi.mock("./api", () => ({
  api: {
    listAvailableConnectors: () => Promise.resolve([]),
    listConfiguredConnectors: (...args) => listConfiguredConnectors(...args),
    getConnectorHealth: () => Promise.resolve([]),
    uploadFile: (...args) => uploadFile(...args),
    regenerateConnector: (...args) => regenerateConnector(...args),
    getCDCStatus: (...args) => getCDCStatus(...args),
    resetCDCWatermark: (...args) => resetCDCWatermark(...args),
  },
}));

const { default: DataSourcesPanel } = await import("./DataSourcesPanel");

const CONNECTOR = { name: "pg", type: "postgres", has_script: true, params: {} };

beforeEach(() => {
  for (const fn of [listConfiguredConnectors, uploadFile, regenerateConnector, resetCDCWatermark, getCDCStatus]) fn.mockReset();
  listConfiguredConnectors.mockResolvedValue([CONNECTOR]);
  getCDCStatus.mockResolvedValue([]);
});

describe("DataSourcesPanel", () => {
  it("asks before regenerating, since that rewrites the ingest script", async () => {
    const showConfirm = vi.fn().mockResolvedValue(false);
    render(<DataSourcesPanel addOutput={vi.fn()} showConfirm={showConfirm} />);
    fireEvent.click(await screen.findByRole("button", { name: "Manage (1)" }));

    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));

    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    expect(showConfirm.mock.calls[0][1]).toContain("ingest/pg.py");
    expect(showConfirm.mock.calls[0][3]).toBe(true);
    expect(regenerateConnector).not.toHaveBeenCalled();
  });

  it("regenerates once confirmed", async () => {
    regenerateConnector.mockResolvedValue({ status: "success", script_path: "ingest/pg.py" });
    const addOutput = vi.fn();
    render(<DataSourcesPanel addOutput={addOutput} showConfirm={vi.fn().mockResolvedValue(true)} />);
    fireEvent.click(await screen.findByRole("button", { name: "Manage (1)" }));

    fireEvent.click(screen.getByRole("button", { name: "Regenerate" }));

    await waitFor(() => expect(regenerateConnector).toHaveBeenCalledWith("pg"));
  });

  it("disables the file input and Import while a file uploads", async () => {
    let finish;
    uploadFile.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<DataSourcesPanel addOutput={vi.fn()} showConfirm={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Upload File" }));

    const input = document.querySelector('input[type="file"]');
    fireEvent.change(input, { target: { files: [new File(["a,b\n1,2\n"], "data.csv", { type: "text/csv" })] } });

    expect(input.disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toBe("Uploading…");
    expect(screen.getByRole("button", { name: "Uploading…" }).disabled).toBe(true);

    await act(async () => { finish({ name: "data.csv", path: "/uploads/data.csv", size: 8 }); });

    expect(input.disabled).toBe(false);
    expect(screen.getByRole("button", { name: "Import" }).disabled).toBe(false);
    expect(screen.getByText("Uploaded: data.csv")).toBeInTheDocument();
  });

  it("reports a failed watermark reset instead of only logging it", async () => {
    getCDCStatus.mockResolvedValue([{ connector: "pg", table: "orders", cdc_mode: "incremental" }]);
    resetCDCWatermark.mockRejectedValue(new Error("connector is syncing"));
    const addOutput = vi.fn();
    render(<DataSourcesPanel addOutput={addOutput} showConfirm={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Manage (1)" }));

    fireEvent.click(await screen.findByRole("button", { name: "Reset Watermark" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(addOutput).toHaveBeenCalledWith("error", expect.stringContaining("connector is syncing")));
  });
});

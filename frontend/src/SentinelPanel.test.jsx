import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getSentinelImpacts = vi.fn().mockResolvedValue([]);
const getSentinelHistory = vi.fn().mockResolvedValue([]);

vi.mock("./api", () => ({
  api: {
    getSentinelDiffs: () => Promise.resolve([
      { diff_id: 1, source_name: "landing.orders", created_at: "2026-10-01T06:00:00", changes: [] },
      { diff_id: 2, source_name: "landing.customers", created_at: "2026-10-02T06:00:00", changes: [] },
    ]),
    getSentinelImpacts: (...args) => getSentinelImpacts(...args),
    getSentinelSources: () => Promise.resolve([{ name: "landing.orders", exists: true }]),
    getSentinelHistory: (...args) => getSentinelHistory(...args),
  },
}));

const { default: SentinelPanel } = await import("./SentinelPanel");

describe("SentinelPanel", () => {
  it("opens a recorded diff from the keyboard and marks it current", async () => {
    render(<SentinelPanel />);
    fireEvent.click(screen.getByRole("tab", { name: "Diffs" }));

    const second = await screen.findByRole("button", { name: /landing\.customers/ });
    expect(second.tabIndex).toBe(0);
    await waitFor(() => expect(getSentinelImpacts).toHaveBeenCalledWith(1));
    fireEvent.keyDown(second, { key: "Enter" });

    await waitFor(() => expect(getSentinelImpacts).toHaveBeenCalledWith(2));
    expect(second.getAttribute("aria-current")).toBe("true");
  });

  it("opens a source's schema history from the keyboard", async () => {
    render(<SentinelPanel />);
    fireEvent.click(screen.getByRole("tab", { name: "History" }));

    fireEvent.keyDown(await screen.findByRole("button", { name: /landing\.orders/ }), { key: "Enter" });

    await waitFor(() => expect(getSentinelHistory).toHaveBeenCalledWith("landing.orders"));
  });
});

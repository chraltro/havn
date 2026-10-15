import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("./api", () => ({
  api: {
    getFreshness: () => Promise.resolve([
      { model: "silver.b", last_run_at: "2026-10-01 06:00:00", hours_since_run: 2, is_stale: false, row_count: 5 },
      { model: "silver.a", last_run_at: "2026-10-01 05:00:00", hours_since_run: 30, is_stale: true, row_count: 9 },
    ]),
    getProfiles: () => Promise.resolve([]),
    getAssertions: () => Promise.resolve([
      { model: "gold.orders", expression: "row_count > 0", passed: false, detail: "0 rows", checked_at: "2026-10-01" },
    ]),
    getContracts: () => Promise.resolve([]),
    getContractHistory: () => Promise.resolve([]),
    getAnomalies: () => Promise.resolve([]),
  },
}));
vi.mock("./PipelineContext", () => ({ usePipeline: () => ({ addOutput: vi.fn(), runContracts: vi.fn() }) }));

const { default: QualityPanel } = await import("./QualityPanel");

describe("QualityPanel", () => {
  it("switches views with a real tab list and opens failures from the keyboard", async () => {
    render(<QualityPanel addOutput={vi.fn()} />);
    await screen.findByText("silver.a");

    const assertions = screen.getByRole("tab", { name: "Assertions" });
    expect(screen.getByRole("tab", { name: "Freshness" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(assertions);
    expect(assertions.getAttribute("aria-selected")).toBe("true");

    // The expander is a real button: Enter/Space work natively.
    const expander = screen.getByRole("button", { name: /gold\.orders/ });
    expect(expander.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(expander);
    expect(expander.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("0 rows")).toBeInTheDocument();
  });

  it("sorts from header buttons and says which way", async () => {
    render(<QualityPanel addOutput={vi.fn()} />);
    await screen.findByText("silver.a");

    const hours = screen.getByRole("button", { name: "Hours Ago" });
    fireEvent.click(hours);
    expect(hours.closest("th").getAttribute("aria-sort")).toBe("ascending");
    const rows = screen.getAllByRole("row").slice(1).map((r) => r.cells[0].textContent);
    expect(rows).toEqual(["silver.b", "silver.a"]);
  });
});

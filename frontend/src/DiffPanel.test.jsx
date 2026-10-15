import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("./WarehouseContext", () => ({ useWarehouse: () => ({ tables: [] }) }));

const { default: DiffPanel } = await import("./DiffPanel");

describe("DiffPanel", () => {
  it("opens a changed model's diff from the keyboard", async () => {
    const api = {
      runDiff: vi.fn().mockResolvedValue([
        {
          model: "silver.orders", total_before: 3, total_after: 4, added: 1, removed: 0, modified: 0,
          schema_changes: [{ change_type: "added", column: "paid", new_type: "BOOLEAN" }],
        },
      ]),
    };
    render(<DiffPanel api={api} addOutput={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Run diff comparison" }));

    const row = await screen.findByRole("button", { name: "Diff details for silver.orders" });
    expect(row.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(row, { key: " " });

    expect(row.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Schema Changes")).toBeInTheDocument();
  });
});

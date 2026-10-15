import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

const table = (name) => ({
  name, full_name: `silver.${name}`, type: "table", description: "", columns: [{ name: "id", type: "INTEGER", nullable: false }],
});

vi.mock("./api", () => ({
  api: {
    getStructuredDocs: () => Promise.resolve({ schemas: [{ name: "silver", tables: [table("customers"), table("orders")] }] }),
  },
}));

const { default: DocsPanel } = await import("./DocsPanel");

describe("DocsPanel navigation", () => {
  it("selects tables and folds schemas with buttons", async () => {
    render(<DocsPanel />);
    const orders = await screen.findByRole("button", { name: /orders/ });
    expect(screen.getByText("silver.customers")).toBeInTheDocument();

    fireEvent.click(orders);
    expect(screen.getByText("silver.orders")).toBeInTheDocument();
    expect(orders.getAttribute("aria-current")).toBe("true");
    expect(screen.getByTitle("orders")).toBeTruthy();

    const schema = screen.getByRole("button", { name: /silver/ , expanded: true });
    fireEvent.click(schema);
    expect(schema.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("button", { name: /orders/ })).toBeNull();
  });
});

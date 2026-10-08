import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getUnitTests = vi.fn();
const runUnitTests = vi.fn();

vi.mock("./api", () => ({
  api: {
    getUnitTests: (...args) => getUnitTests(...args),
    runUnitTests: (...args) => runUnitTests(...args),
  },
}));

const { default: UnitTestsPanel } = await import("./UnitTestsPanel");

const LISTED = {
  tests: [
    {
      name: "counts orders per customer",
      model: "silver.customers",
      source_path: "customers.yml",
      expected_row_count: 2,
      ordered: false,
    },
    {
      name: "drops cancelled orders",
      model: "gold.orders",
      source_path: "orders.yml",
      expected_row_count: 1,
      ordered: false,
    },
  ],
  errors: [],
};

const RUN_RESULT = {
  passed: 1,
  failed: 1,
  errored: 0,
  ok: false,
  duration_ms: 42,
  load_errors: [],
  results: [
    {
      name: "counts orders per customer",
      model: "silver.customers",
      status: "pass",
      duration_ms: 12,
      message: "",
      source_path: "customers.yml",
      columns: ["customer_id", "name", "order_count"],
      missing_rows: [],
      unexpected_rows: [],
      missing_count: 0,
      unexpected_count: 0,
      warnings: ["mock for bronze.customers omits 2 column(s) present in the warehouse"],
    },
    {
      name: "drops cancelled orders",
      model: "gold.orders",
      status: "fail",
      duration_ms: 30,
      message: "1 expected row(s) missing, 1 unexpected row(s)",
      source_path: "orders.yml",
      columns: ["order_id", "status"],
      missing_rows: [{ order_id: 1, status: "open" }],
      unexpected_rows: [{ order_id: 1, status: null }],
      missing_count: 1,
      unexpected_count: 1,
      warnings: [],
    },
  ],
};

beforeEach(() => {
  getUnitTests.mockReset().mockResolvedValue(LISTED);
  runUnitTests.mockReset().mockResolvedValue(RUN_RESULT);
});

describe("UnitTestsPanel", () => {
  it("lists declared tests before anything is run", async () => {
    render(<UnitTestsPanel />);
    expect(await screen.findByText("counts orders per customer")).toBeInTheDocument();
    expect(screen.getByText("drops cancelled orders")).toBeInTheDocument();
    expect(screen.getAllByText("not run")).toHaveLength(2);
  });

  it("renders the run result with pass/fail status and a summary", async () => {
    render(<UnitTestsPanel />);
    await screen.findByText("counts orders per customer");

    fireEvent.click(screen.getByText("Run tests"));

    expect(await screen.findByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("1 passed")).toBeInTheDocument();
    expect(screen.getByText(", 1 failed")).toBeInTheDocument();
    expect(
      screen.getByText("1 expected row(s) missing, 1 unexpected row(s)"),
    ).toBeInTheDocument();
  });

  it("shows warnings attached to a result", async () => {
    render(<UnitTestsPanel />);
    await screen.findByText("counts orders per customer");
    fireEvent.click(screen.getByText("Run tests"));

    expect(
      await screen.findByText(/omits 2 column\(s\) present in the warehouse/),
    ).toBeInTheDocument();
  });

  it("expands a failing test to show the differing rows", async () => {
    render(<UnitTestsPanel />);
    await screen.findByText("counts orders per customer");
    fireEvent.click(screen.getByText("Run tests"));
    await screen.findByText("FAIL");

    expect(screen.queryByText("Expected, not produced (1)")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("drops cancelled orders"));

    expect(await screen.findByText("Expected, not produced (1)")).toBeInTheDocument();
    expect(screen.getByText("Produced, not expected (1)")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByText("NULL")).toBeInTheDocument();
  });

  it("passes the model filter to the run", async () => {
    render(<UnitTestsPanel />);
    await screen.findByText("counts orders per customer");

    fireEvent.change(screen.getByLabelText("Model filter"), {
      target: { value: "gold.orders" },
    });
    fireEvent.click(screen.getByText("Run tests"));

    await waitFor(() => expect(runUnitTests).toHaveBeenCalledWith("gold.orders"));
  });

  it("surfaces definition load errors", async () => {
    getUnitTests.mockResolvedValue({ tests: [], errors: ["customers.yml: invalid YAML"] });
    render(<UnitTestsPanel />);
    expect(
      await screen.findByText(/Definition error: customers.yml: invalid YAML/),
    ).toBeInTheDocument();
  });
});

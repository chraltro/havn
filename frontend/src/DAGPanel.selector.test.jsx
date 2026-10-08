import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listModels = vi.fn();
const runSelection = vi.fn();

vi.mock("./api", () => ({
  api: {
    getDAG: () => Promise.resolve(DAG),
    getLineage: () => Promise.resolve(null),
    getAllLineage: () => Promise.resolve(null),
    getRewindRuns: () => Promise.resolve([]),
    getRewindSnapshots: () => Promise.resolve([]),
    listModels: (...args) => listModels(...args),
  },
}));

vi.mock("./PipelineContext", () => ({
  usePipeline: () => ({ runSelection: (...args) => runSelection(...args) }),
}));

const DAG = {
  nodes: [
    { id: "bronze.orders", schema: "bronze", type: "table", path: "transform/bronze/orders.sql" },
    { id: "silver.orders", schema: "silver", type: "table", path: "transform/silver/orders.sql" },
    { id: "gold.orders", schema: "gold", type: "table", path: "transform/gold/orders.sql" },
  ],
  edges: [
    { source: "bronze.orders", target: "silver.orders" },
    { source: "silver.orders", target: "gold.orders" },
  ],
};

const { default: DAGPanel } = await import("./DAGPanel");

describe("DAGPanel selector input", () => {
  beforeEach(() => {
    listModels.mockReset();
    runSelection.mockReset();
  });

  it("highlights the nodes a previewed selector matched", async () => {
    listModels.mockResolvedValue([
      { full_name: "silver.orders" },
      { full_name: "gold.orders" },
    ]);
    const { container } = render(<DAGPanel onOpenFile={vi.fn()} showConfirm={vi.fn()} />);

    const input = await screen.findByTestId("dag-selector-input");
    expect(input.getAttribute("title")).toContain("state:modified+");

    fireEvent.change(input, { target: { value: "+gold.orders" } });
    fireEvent.click(screen.getByText("Preview"));

    await waitFor(() => expect(listModels).toHaveBeenCalledWith("+gold.orders"));

    const canvas = container.querySelector("canvas");
    await waitFor(() =>
      expect(canvas.dataset.selectorMatches).toBe("gold.orders,silver.orders")
    );
    expect(screen.getByTestId("dag-selector-count").textContent).toContain("2 matched");
  });

  it("runs the selector as the transform target", async () => {
    render(<DAGPanel onOpenFile={vi.fn()} showConfirm={vi.fn()} />);

    const input = await screen.findByTestId("dag-selector-input");
    fireEvent.change(input, { target: { value: "tag:daily" } });
    fireEvent.click(screen.getByText("Run selection"));

    expect(runSelection).toHaveBeenCalledWith("tag:daily");
    expect(listModels).not.toHaveBeenCalled();
  });

  it("reports a selector the server rejected and highlights nothing", async () => {
    listModels.mockRejectedValue(new Error("Unknown selector method 'nope:'"));
    const { container } = render(<DAGPanel onOpenFile={vi.fn()} showConfirm={vi.fn()} />);

    const input = await screen.findByTestId("dag-selector-input");
    fireEvent.change(input, { target: { value: "nope:x" } });
    fireEvent.click(screen.getByText("Preview"));

    const error = await screen.findByTestId("dag-selector-error");
    expect(error.textContent).toContain("Unknown selector method");
    expect(container.querySelector("canvas").dataset.selectorMatches).toBeUndefined();
  });
});

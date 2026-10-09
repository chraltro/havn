import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getBranchStatus = vi.fn();
const diffBranch = vi.fn();
const buildBranch = vi.fn();

vi.mock("./api", () => ({
  api: {
    getBranchStatus: (...a) => getBranchStatus(...a),
    diffBranch: (...a) => diffBranch(...a),
    buildBranch: (...a) => buildBranch(...a),
  },
}));

const { default: BranchDataChanges } = await import("./BranchDataChanges");

function status(over = {}) {
  return {
    enabled: true,
    active: true,
    branch: "feature/x",
    warehouse: { path: ".havn/branches/feature-x-1a2b3c4d.duckdb", exists: true },
    base: { label: "prod", path: "prod.duckdb", readable: true },
    models: {
      local: [{ name: "silver.totals", stale: false, stale_reasons: [] }],
      deferred: ["bronze.orders"],
      needs_build: [],
      prunable: [],
      total: 2,
    },
    ...over,
  };
}

const REPORT = {
  branch: "feature/x",
  base: "prod",
  commit: "abcdef1234",
  markdown: "<!-- havn-data-diff -->\n## diff",
  summary: { changed: 1 },
  models: [
    {
      model: "silver.totals", status: "changed", before: 5, after: 3,
      added: 0, removed: 2, modified: 3, primary_key: ["id"],
      schema_changes: [{ column: "tag", change: "added", old_type: null, new_type: "VARCHAR" }],
      sample_added: [], sample_removed: [{ id: 4, amount: 40 }], sample_modified: [],
    },
    {
      model: "silver.same", status: "unchanged", before: 1, after: 1,
      added: 0, removed: 0, modified: 0, schema_changes: [],
      sample_added: [], sample_removed: [], sample_modified: [],
    },
  ],
};

describe("BranchDataChanges", () => {
  beforeEach(() => {
    getBranchStatus.mockReset();
    diffBranch.mockReset();
    buildBranch.mockReset();
  });

  it("explains why there is no branch warehouse", async () => {
    getBranchStatus.mockResolvedValue({ enabled: false, active: false, branch: "main", reason: "branches are not enabled" });
    render(<BranchDataChanges />);
    expect(await screen.findByText("No branch warehouse")).toBeTruthy();
    expect(screen.getByText(/branches are not enabled/)).toBeTruthy();
    expect(diffBranch).not.toHaveBeenCalled();
  });

  it("shows the per-model diff with schema changes and samples", async () => {
    getBranchStatus.mockResolvedValue(status());
    diffBranch.mockResolvedValue(REPORT);
    render(<BranchDataChanges />);

    const rows = await screen.findAllByTestId("branch-diff-row");
    expect(rows).toHaveLength(1); // unchanged models are summarised, not listed
    expect(rows[0].textContent).toContain("silver.totals");
    expect(rows[0].textContent).toContain("5 → 3 rows");
    expect(rows[0].textContent).toContain("−2");
    expect(rows[0].textContent).toContain("+ tag");
    expect(screen.getByText(/1 model differs, 1 rebuilt with identical data/)).toBeTruthy();

    fireEvent.click(screen.getByText(/Sample rows/));
    expect(screen.getByText(/Removed · 2 rows, showing 1/)).toBeTruthy();
  });

  it("asks for a build when the branch is behind and builds on click", async () => {
    getBranchStatus
      .mockResolvedValueOnce(status({
        models: { ...status().models, local: [], needs_build: ["silver.totals"] },
      }))
      .mockResolvedValueOnce(status());
    diffBranch.mockResolvedValue({ ...REPORT, models: [] });
    buildBranch.mockResolvedValue({ built: ["silver.totals"], pruned: [], failed: {}, base: "prod" });
    const addOutput = vi.fn();
    render(<BranchDataChanges addOutput={addOutput} />);

    expect(await screen.findByText(/not built yet/)).toBeTruthy();
    fireEvent.click(screen.getByText("Build branch"));
    await waitFor(() => expect(buildBranch).toHaveBeenCalled());
    await waitFor(() => expect(addOutput).toHaveBeenCalledWith("info", expect.stringContaining("1 built")));
    await waitFor(() => expect(getBranchStatus).toHaveBeenCalledTimes(2));
  });

  it("does not diff when the base cannot be read", async () => {
    getBranchStatus.mockResolvedValue(status({ base: { label: "prod", readable: false, reason: "locked by another process" } }));
    render(<BranchDataChanges />);
    expect(await screen.findByText(/locked by another process/)).toBeTruthy();
    expect(diffBranch).not.toHaveBeenCalled();
    expect(screen.getByText("Build branch").disabled).toBe(true);
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listPrs = vi.fn();
const getPrReview = vi.fn();
const approvePr = vi.fn();
const mergePr = vi.fn();
const buildPr = vi.fn();
const requestPrChanges = vi.fn();
const updatePr = vi.fn();
const getGitStatus = vi.fn();
const initGit = vi.fn();

vi.mock("./api", () => ({
  api: {
    listPrs: (...a) => listPrs(...a),
    getPrReview: (...a) => getPrReview(...a),
    approvePr: (...a) => approvePr(...a),
    mergePr: (...a) => mergePr(...a),
    buildPr: (...a) => buildPr(...a),
    requestPrChanges: (...a) => requestPrChanges(...a),
    updatePr: (...a) => updatePr(...a),
    getDeployTargets: () => Promise.resolve({ environments: [{ name: "prod", production: true, active: false, exists: true }], default_ref: "main" }),
    getDeployPlan: () => Promise.resolve({ models: [], commit: "abc" }),
    listDeploys: () => Promise.resolve([]),
    getGitStatus: (...a) => getGitStatus(...a),
    initGit: (...a) => initGit(...a),
  },
}));
vi.mock("./AuthContext", () => ({ useAuth: () => ({ currentUser: { username: "ingrid" } }) }));

const { default: ShipPanel, layoutColumns, fmtMetric } = await import("./ShipPanel");

const PR = {
  id: "pr-1", title: "Net revenue", description: "", base_ref: "main", head_ref: "feature/net",
  author: "christian", status: "open", created_at: null, approvers: [], change_requesters: [],
};

function review(overrides = {}) {
  return {
    pr: PR,
    files: ["transform/gold/revenue.sql"],
    head_sha: "abc1234",
    impact: {
      nodes: [
        { name: "silver.orders", role: "upstream", path: "transform/silver/orders.sql" },
        { name: "gold.revenue", role: "changed", path: "transform/gold/revenue.sql" },
        { name: "gold.report", role: "impacted", path: "transform/gold/report.sql" },
      ],
      edges: [["gold.revenue", "gold.report"], ["silver.orders", "gold.revenue"]],
    },
    build: {
      status: "success", branch_head: "abc1234", started_at: "t0", finished_at: null, duration_ms: 900,
      metric_diff: [
        { metric: "revenue", model: "gold.revenue", description: "", base: 41200000, pr: 40211200, delta: -988800, delta_pct: -2.4,
          series: [{ bucket: "2026-08-01", base: 100, pr: 90 }, { bucket: "2026-09-01", base: 110, pr: 105 }], error: null },
      ],
      data_diff: {
        "gold.revenue": { status: "modified", main_rows: 10, pr_rows: 10, added_rows: 3, removed_rows: 3,
                          schema_changes: [{ type: "added", column: "net_revenue", data_type: "DECIMAL" }] },
        "silver.orders": { status: "unchanged", main_rows: 5, pr_rows: 5, added_rows: 0, removed_rows: 0, schema_changes: [] },
      },
    },
    gate: [
      { key: "build", label: "Built and checked", state: "pass", detail: "ok", required: false },
      { key: "changes", label: "No changes requested", state: "pass", detail: "", required: true },
      { key: "approval", label: "Approved", state: "pending", detail: "Needs at least one approval.", required: true },
      { key: "conflicts", label: "Merges cleanly", state: "pass", detail: "", required: true },
      { key: "clean", label: "Working tree clean", state: "pass", detail: "", required: true },
    ],
    ready: false,
    build_current: true,
    plan: ["Snapshot the warehouse", "Check out main and merge feature/net with --no-ff", "Mark merged"],
    after_merge: "Run the pipeline on main.",
    ...overrides,
  };
}

function renderShip(props = {}) {
  const handlers = {
    showConfirm: vi.fn().mockResolvedValue(true), addOutput: vi.fn(), onOpenFile: vi.fn(),
    onNavigate: vi.fn(), onMerged: vi.fn(),
  };
  Object.assign(handlers, props);
  render(<ShipPanel running={false} {...handlers} />);
  return handlers;
}

describe("layoutColumns", () => {
  it("puts upstream first and each model after its parents", () => {
    const { nodes, edges } = review().impact;
    const cols = layoutColumns(nodes, edges);
    expect(cols.map((c) => c.map((n) => n.name))).toEqual([["silver.orders"], ["gold.revenue"], ["gold.report"]]);
  });
});

describe("ShipPanel", () => {
  beforeEach(() => {
    for (const f of [listPrs, getPrReview, approvePr, mergePr, buildPr, requestPrChanges, updatePr, getGitStatus, initGit]) f.mockReset();
    getGitStatus.mockResolvedValue({ is_git_repo: true });
    listPrs.mockResolvedValue([PR]);
    getPrReview.mockResolvedValue(review());
  });

  it("selects the first open change and shows its impact, data diff and gate", async () => {
    renderShip();
    expect(await screen.findByRole("heading", { name: "Net revenue" })).toBeTruthy();
    expect(getPrReview).toHaveBeenCalledWith("pr-1");
    expect(screen.getByText("+ net_revenue")).toBeTruthy();
    expect(screen.getByText(/1 table differs, 1 unchanged/)).toBeTruthy();
    expect(screen.getByText("Waiting on: approved")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Merge into main" }).disabled).toBe(true);
  });

  it("approves as the signed-in user", async () => {
    approvePr.mockResolvedValue({});
    renderShip();
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(approvePr).toHaveBeenCalledWith("pr-1", "ingrid"));
  });

  it("merges when ready, confirming first", async () => {
    getPrReview.mockResolvedValue(review({ ready: true }));
    mergePr.mockResolvedValue({ success: true, merge_commit: "def5678" });
    const h = renderShip();
    fireEvent.click(await screen.findByRole("button", { name: "Merge into main" }));
    await waitFor(() => expect(mergePr).toHaveBeenCalledWith("pr-1", "ingrid"));
    expect(h.showConfirm).toHaveBeenCalledWith("Merge change", 'Merge "Net revenue" into main?', "Merge", false);
    expect(h.onMerged).toHaveBeenCalled();
  });

  it("warns before merging without a passing build of the latest commit", async () => {
    getPrReview.mockResolvedValue(review({ ready: true, build_current: false }));
    const h = renderShip({ showConfirm: vi.fn().mockResolvedValue(false) });
    fireEvent.click(await screen.findByRole("button", { name: "Merge into main" }));
    await waitFor(() => expect(h.showConfirm).toHaveBeenCalled());
    const [, msg, label, danger] = h.showConfirm.mock.calls[0];
    expect(msg).toMatch(/no passing build/);
    expect(label).toBe("Merge anyway");
    expect(danger).toBe(true);
    expect(mergePr).not.toHaveBeenCalled();
  });

  it("requests changes with a reason", async () => {
    requestPrChanges.mockResolvedValue({});
    renderShip();
    fireEvent.click(await screen.findByRole("button", { name: "Request changes" }));
    fireEvent.change(screen.getByLabelText("What needs to change"), { target: { value: "add a test" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(requestPrChanges).toHaveBeenCalledWith("pr-1", "ingrid", "add a test"));
  });

  it("points to Git → Reviews when there are no changes", async () => {
    listPrs.mockResolvedValue([]);
    const h = renderShip();
    fireEvent.click(await screen.findByRole("button", { name: "Create a change" }));
    expect(h.onNavigate).toHaveBeenCalledWith("Git:Reviews");
  });

  it("offers git init in place of the deploy box when the project is not a repository", async () => {
    listPrs.mockResolvedValue([]);
    getGitStatus.mockResolvedValueOnce({ is_git_repo: false }).mockResolvedValue({ is_git_repo: true });
    initGit.mockResolvedValue({});
    renderShip();
    fireEvent.click(await screen.findByRole("button", { name: "Initialize git" }));
    expect(screen.queryByLabelText("Deploy")).toBeNull();
    await waitFor(() => expect(initGit).toHaveBeenCalledWith("main"));
    expect(await screen.findByRole("button", { name: "Create a change" })).toBeTruthy();
  });

  it("stops an author approving their own change and offers the waiver", async () => {
    getPrReview.mockResolvedValue(review({ pr: { ...PR, author: "Ingrid", require_approval: true } }));
    updatePr.mockResolvedValue({});
    const h = renderShip();
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(approve.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Merge without review" }));
    await waitFor(() => expect(updatePr).toHaveBeenCalledWith("pr-1", { require_approval: false }));
    expect(h.showConfirm.mock.calls[0][0]).toBe("Merge without review");
  });

  it("shows how affected metrics move", async () => {
    renderShip();
    expect(await screen.findByText("revenue")).toBeTruthy();
    // Expectations go through fmtMetric so they hold in any locale the
    // suite runs under; fmtMetric's own tests pin the exact strings.
    // getByText collapses whitespace in the page text (a compact "mio." comes
    // after a non-breaking space) but not in the expected string, so match it.
    const shown = (text) => text.replace(/\s+/g, " ");
    expect(screen.getByText(shown(fmtMetric(41200000)))).toBeTruthy();
    expect(screen.getByText(shown(fmtMetric(40211200)))).toBeTruthy();
    expect(screen.getByText(shown(`\u25BC ${fmtMetric(-2.4)}%`))).toBeTruthy();
    expect(screen.getByRole("img", { name: "revenue by month, base and branch" })).toBeTruthy();
  });

  it("keeps a build on one change from marking every other change as building", async () => {
    // One page-wide busy flag used to put "Building…" on, and disable, every
    // other change's actions while change A built.
    const other = { ...PR, id: "pr-2", title: "Other change", head_ref: "feature/other" };
    listPrs.mockResolvedValue([PR, other]);
    getPrReview.mockImplementation((id) =>
      Promise.resolve(id === "pr-2" ? review({ pr: other, build: null }) : review()));
    buildPr.mockResolvedValue({});
    renderShip();

    fireEvent.click(await screen.findByRole("button", { name: "Rebuild" }));
    await waitFor(() => expect(buildPr).toHaveBeenCalled());

    fireEvent.click(screen.getByText("Other change"));
    expect(await screen.findByRole("heading", { name: "Other change" })).toBeTruthy();
    const builds = await screen.findAllByRole("button", { name: "Build" });
    expect(builds.every((b) => !b.disabled)).toBe(true);
    expect(screen.queryByText("Building…")).toBeNull();
  });

  it("formats metric values compactly", () => {
    expect(fmtMetric(41200000, "en-US")).toBe("41.2M");
    expect(fmtMetric(12345, "en-US")).toBe("12.3K");
    expect(fmtMetric(0.5, "en-US")).toBe("0.5");
    expect(fmtMetric(null)).toBe("–");
  });

  it("uses one decimal separator at every magnitude", () => {
    // Large values were formatted with toFixed and small ones localized, so
    // a Danish reader got "41.2M" next to "0,5".
    expect(fmtMetric(41200000, "da-DK")).toContain("41,2");
    expect(fmtMetric(0.5, "da-DK")).toBe("0,5");
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getDeployTargets = vi.fn();
const getDeployPlan = vi.fn();
const startDeploy = vi.fn();
const getDeploy = vi.fn();
const listDeploys = vi.fn();
vi.mock("./api", () => ({
  api: {
    getDeployTargets: (...a) => getDeployTargets(...a),
    getDeployPlan: (...a) => getDeployPlan(...a),
    startDeploy: (...a) => startDeploy(...a),
    getDeploy: (...a) => getDeploy(...a),
    listDeploys: (...a) => listDeploys(...a),
  },
}));

const { default: DeployCard } = await import("./DeployCard");

const TARGETS = {
  environments: [
    { name: "dev", active: true, production: false, exists: true },
    { name: "prod", active: false, production: true, exists: true },
  ],
  default_ref: "main",
};

describe("DeployCard", () => {
  beforeEach(() => {
    for (const f of [getDeployTargets, getDeployPlan, startDeploy, getDeploy, listDeploys]) f.mockReset();
    getDeployTargets.mockResolvedValue(TARGETS);
    getDeployPlan.mockResolvedValue({ env: "prod", ref: "main", commit: "abc1234def", models: ["silver.a", "gold.b"] });
    listDeploys.mockResolvedValue([]);
  });

  it("defaults to production and shows the plan", async () => {
    render(<DeployCard refName="main" showConfirm={vi.fn()} />);
    expect(await screen.findByText("2 models will rebuild:")).toBeTruthy();
    expect(getDeployPlan).toHaveBeenCalledWith("prod", "main");
    expect(screen.getByRole("combobox", { name: "Environment" }).value).toBe("prod");
    expect(screen.getByRole("button", { name: "Deploy to prod" })).toBeTruthy();
  });

  it("says when the environment is already up to date", async () => {
    getDeployPlan.mockResolvedValue({ models: [], commit: "abc" });
    render(<DeployCard refName="main" showConfirm={vi.fn()} />);
    expect(await screen.findByText(/prod is up to date with/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Deploy to prod" }).disabled).toBe(true);
  });

  it("confirms, deploys, and reports a rollback", async () => {
    const showConfirm = vi.fn().mockResolvedValue(true);
    startDeploy.mockResolvedValue({ id: "deploy-1", status: "running", env: "prod", models: ["silver.a", "gold.b"] });
    getDeploy.mockResolvedValue({
      id: "deploy-1", status: "rolled_back", env: "prod", commit: "abc1234def", models: ["silver.a", "gold.b"],
      failed: { "gold.b": { status: "assertion_failed", error: "assertion failed: n > 0 (got 0 rows)" } },
    });
    const onDeployed = vi.fn();
    render(<DeployCard refName="main" prId="pr-1" showConfirm={showConfirm} onDeployed={onDeployed} />);
    await screen.findByText("2 models will rebuild:");
    fireEvent.click(screen.getByRole("button", { name: "Deploy to prod" }));
    await waitFor(() => expect(startDeploy).toHaveBeenCalledWith({ env: "prod", ref: "main", pr_id: "pr-1" }));
    const [title, , label, danger] = showConfirm.mock.calls[0];
    expect([title, label, danger]).toEqual(["Deploy to prod", "Deploy to prod", true]);
    // The card polls every 1.5s until the deploy settles.
    expect(await screen.findByText(/Rolled back\. prod is exactly as it was/, {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByText(/assertion failed: n > 0/)).toBeTruthy();
    expect(onDeployed).toHaveBeenCalled();
  });

  it("switching to a non-production environment re-plans", async () => {
    render(<DeployCard refName="main" showConfirm={vi.fn()} />);
    await screen.findByText("2 models will rebuild:");
    fireEvent.change(screen.getByRole("combobox", { name: "Environment" }), { target: { value: "dev" } });
    await waitFor(() => expect(getDeployPlan).toHaveBeenLastCalledWith("dev", "main"));
  });
});

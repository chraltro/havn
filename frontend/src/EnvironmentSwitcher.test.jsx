import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

const getEnvironment = vi.fn();
const switchEnvironment = vi.fn();

const getBranch = vi.fn();

vi.mock("./api", () => ({
  api: {
    getEnvironment: (...args) => getEnvironment(...args),
    switchEnvironment: (...args) => switchEnvironment(...args),
    getBranch: (...args) => getBranch(...args),
  },
}));

const { default: EnvironmentSwitcher, GIT_CHECKOUT_EVENT } = await import("./EnvironmentSwitcher");

function environment(defer) {
  return {
    active: "dev",
    available: ["dev", "prod"],
    database_path: "dev.duckdb",
    defer,
  };
}

describe("EnvironmentSwitcher defer status", () => {
  beforeEach(() => {
    getEnvironment.mockReset();
    switchEnvironment.mockReset();
  });

  it("shows no defer badge when the environment has no defer target", async () => {
    getEnvironment.mockResolvedValue(environment(null));
    render(<EnvironmentSwitcher showConfirm={vi.fn()} />);
    await screen.findByText("dev");
    expect(screen.queryByTestId("defer-badge")).toBeNull();
  });

  it("marks a readable defer target green", async () => {
    getEnvironment.mockResolvedValue(
      environment({ target: "prod", path: "/w/prod.duckdb", lockable: true, reason: "" })
    );
    render(<EnvironmentSwitcher showConfirm={vi.fn()} />);

    const badge = await screen.findByTestId("defer-badge");
    expect(badge.textContent).toContain("defer: prod");
    expect(screen.getByTestId("defer-dot").dataset.state).toBe("readable");
    expect(badge.getAttribute("title")).toContain("/w/prod.duckdb");
    expect(badge.getAttribute("title")).toContain("--defer");
  });

  it("marks a locked defer target amber and points at --defer-snapshot", async () => {
    getEnvironment.mockResolvedValue(
      environment({
        target: "prod",
        path: "/w/prod.duckdb",
        lockable: false,
        reason: "held by another process",
      })
    );
    render(<EnvironmentSwitcher showConfirm={vi.fn()} />);

    const badge = await screen.findByTestId("defer-badge");
    await waitFor(() =>
      expect(screen.getByTestId("defer-dot").dataset.state).toBe("locked")
    );
    const title = badge.getAttribute("title");
    expect(title).toContain("held by another process");
    expect(title).toContain("--defer-snapshot");
  });
});

function branchInfo(over = {}) {
  return {
    enabled: true,
    active: true,
    reason: "on branch 'feature/x'",
    branch: "feature/x",
    warehouse: { path: ".havn/branches/feature-x-1a2b3c4d.duckdb", exists: true },
    base: { label: "prod", path: "prod.duckdb", exists: true },
    server: { head: "branch:feature/x", pending: null },
    ...over,
  };
}

describe("EnvironmentSwitcher branch warehouses", () => {
  beforeEach(() => {
    getEnvironment.mockReset();
    getBranch.mockReset();
  });

  it("names the branch and its base on a branch warehouse", async () => {
    getEnvironment.mockResolvedValue({
      active: null,
      available: ["dev", "prod"],
      database_path: ".havn/branches/feature-x-1a2b3c4d.duckdb",
      defer: { target: "prod", path: "prod.duckdb", lockable: true },
      branch: branchInfo(),
    });
    render(<EnvironmentSwitcher showConfirm={vi.fn()} />);
    const badge = await screen.findByTestId("branch-badge");
    expect(badge.textContent).toContain("feature/x");
    expect(badge.dataset.kind).toBe("branch");
    expect(badge.getAttribute("title")).toContain("read from prod");
    expect(screen.getByTestId("defer-badge").textContent).toContain("defer: prod");
  });

  it("shows a plain branch pill on main next to the environment", async () => {
    getEnvironment.mockResolvedValue({
      active: "dev",
      available: ["dev", "prod"],
      database_path: "dev.duckdb",
      defer: null,
      branch: branchInfo({ active: false, branch: "main", reason: "'main' is a main branch", server: { head: "branch:main" } }),
    });
    render(<EnvironmentSwitcher showConfirm={vi.fn()} />);
    const badge = await screen.findByTestId("branch-badge");
    expect(badge.dataset.kind).toBe("main");
    expect(screen.getByText("dev")).toBeTruthy();
  });

  it("follows a checkout and tells the app", async () => {
    getEnvironment
      .mockResolvedValueOnce({
        active: "dev", available: ["dev"], database_path: "dev.duckdb", defer: null,
        branch: branchInfo({ active: false, branch: "main", server: { head: "branch:main" } }),
      })
      .mockResolvedValueOnce({
        active: null, available: ["dev"], database_path: ".havn/branches/feature-x-1a2b3c4d.duckdb",
        defer: { target: "prod", lockable: true }, branch: branchInfo(),
      });
    getBranch.mockResolvedValue(branchInfo());
    const onBranchChange = vi.fn();
    render(<EnvironmentSwitcher showConfirm={vi.fn()} onBranchChange={onBranchChange} />);
    await screen.findByText("dev");

    window.dispatchEvent(new Event(GIT_CHECKOUT_EVENT));
    await waitFor(() => expect(onBranchChange).toHaveBeenCalledWith(expect.objectContaining({ branch: "feature/x" })));
    await waitFor(() => expect(screen.getByTestId("branch-badge").dataset.kind).toBe("branch"));
  });

  it("marks a switch the server is still holding back", async () => {
    getEnvironment.mockResolvedValue({
      active: null, available: [], database_path: "warehouse.duckdb", defer: null,
      branch: branchInfo({
        active: false, branch: "main",
        server: { head: "branch:main", pending: { branch: "feature/x", reason: "transform is running" } },
      }),
    });
    render(<EnvironmentSwitcher showConfirm={vi.fn()} />);
    const badge = await screen.findByTestId("branch-badge");
    expect(badge.dataset.kind).toBe("pending");
    expect(badge.getAttribute("title")).toContain("transform is running");
  });
});

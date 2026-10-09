import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

const getEnvironment = vi.fn();
const switchEnvironment = vi.fn();

vi.mock("./api", () => ({
  api: {
    getEnvironment: (...args) => getEnvironment(...args),
    switchEnvironment: (...args) => switchEnvironment(...args),
  },
}));

const { default: EnvironmentSwitcher } = await import("./EnvironmentSwitcher");

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

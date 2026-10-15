import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

const getOverview = vi.fn();

vi.mock("./api", () => ({
  api: {
    getOverview: (...args) => getOverview(...args),
    getGitStatus: () => Promise.resolve({ is_git_repo: false }),
  },
}));

const { default: OverviewPanel } = await import("./OverviewPanel");

const RUNS = [
  { run_id: 1, run_type: "transform", target: "silver.a", status: "success" },
  { run_id: 2, run_type: "transform", target: "silver.b", status: "skipped" },
  { run_id: 3, run_type: "ingest", target: "load.py", status: "running" },
  { run_id: 4, run_type: "transform", target: "gold.c", status: "error", error: "boom" },
];

function overview() {
  return (
    <OverviewPanel onNavigate={vi.fn()} onSelectTable={vi.fn()} onOpenFile={vi.fn()} refreshKey={0} />
  );
}

describe("OverviewPanel run status", () => {
  it("gives each status its own glyph, so running and skipped do not read as failed", async () => {
    getOverview.mockResolvedValue({
      has_data: true, total_tables: 1, total_rows: 1, connectors: 0, schemas: [], recent_runs: RUNS,
    });
    render(overview());

    expect((await screen.findByRole("img", { name: "running" })).textContent).toBe("○");
    expect(screen.getByRole("img", { name: "error" }).textContent).toBe("✗");
    expect(screen.getByRole("img", { name: "success" }).textContent).toBe("✓");
    expect(screen.getByRole("img", { name: "skipped" }).textContent).toBe("–");
  });

  it("counts and lists only real failures, behind a keyboard-operable expander", async () => {
    getOverview.mockResolvedValue({
      has_data: true, total_tables: 1, total_rows: 1, connectors: 0, schemas: [], recent_runs: RUNS,
    });
    render(overview());

    const runs = await screen.findByRole("button", { name: /1 failed/ });
    expect(runs.getAttribute("aria-expanded")).toBe("false");
    fireEvent.keyDown(runs, { key: "Enter" });
    expect(runs.getAttribute("aria-expanded")).toBe("true");

    // The failed card names gold.c only; skipped and running runs are not in it.
    expect(screen.getByRole("button", { name: "gold.c" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "silver.b" })).toBeNull();
    expect(screen.queryByRole("button", { name: "load.py" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Close failed runs" }));
    expect(screen.queryByRole("button", { name: "gold.c" })).toBeNull();
  });

  it("opens a run's target from the keyboard and shows it in full on hover", async () => {
    getOverview.mockResolvedValue({
      has_data: true, total_tables: 1, total_rows: 1, connectors: 0, schemas: [], recent_runs: RUNS,
    });
    const onSelectTable = vi.fn();
    render(<OverviewPanel onNavigate={vi.fn()} onSelectTable={onSelectTable} onOpenFile={vi.fn()} refreshKey={0} />);

    const target = await screen.findByRole("button", { name: "Open silver.a" });
    expect(target.getAttribute("title")).toBe("silver.a");
    fireEvent.keyDown(target, { key: "Enter" });
    expect(onSelectTable).toHaveBeenCalledWith("silver", "a");
  });
});

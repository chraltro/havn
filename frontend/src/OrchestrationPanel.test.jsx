import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

const listJobs = vi.fn();
const runJob = vi.fn();
const listJobRuns = vi.fn();
const cancelJobRun = vi.fn();
const getJobRun = vi.fn();

vi.mock("./api", () => ({
  api: {
    listJobs: (...args) => listJobs(...args),
    runJob: (...args) => runJob(...args),
    listJobRuns: (...args) => listJobRuns(...args),
    cancelJobRun: (...args) => cancelJobRun(...args),
    getJobRun: (...args) => getJobRun(...args),
  },
}));
vi.mock("./PipelineContext", () => ({ usePipeline: () => ({ running: false }) }));

const { default: OrchestrationPanel } = await import("./OrchestrationPanel");

const JOB = { name: "daily", targets: ["gold.orders"], schedules: [], tags: [], enabled: true };
const RUNNING = {
  id: "r1", job_name: "daily", status: "running", trigger: "manual",
  steps_completed: 0, steps_total: 2, steps_failed: 0, steps_skipped: 0, started_at: null,
};

beforeEach(() => {
  for (const fn of [listJobs, runJob, listJobRuns, cancelJobRun, getJobRun]) fn.mockReset();
  listJobs.mockResolvedValue([JOB]);
  listJobRuns.mockResolvedValue([]);
});

describe("OrchestrationPanel", () => {
  it("switches views with a real tab list", async () => {
    render(<OrchestrationPanel showConfirm={vi.fn()} />);
    expect(screen.getByRole("tablist")).toBeTruthy();
    const results = screen.getByRole("tab", { name: "Job Results" });
    expect(screen.getByRole("tab", { name: "Plan Jobs" }).getAttribute("aria-selected")).toBe("true");
    expect(results.getAttribute("aria-selected")).toBe("false");
    await screen.findByRole("button", { name: "Run" });

    fireEvent.click(results);
    expect(results.getAttribute("aria-selected")).toBe("true");
    expect(await screen.findByText("No job runs yet")).toBeInTheDocument();
  });

  it("starts a job once however often Run is clicked", async () => {
    let finish;
    runJob.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<OrchestrationPanel showConfirm={vi.fn()} />);

    const run = await screen.findByRole("button", { name: "Run" });
    fireEvent.click(run);
    fireEvent.click(run);

    expect(runJob).toHaveBeenCalledTimes(1);
    expect(run.disabled).toBe(true);
    expect(run.textContent).toBe("Starting…");

    await act(async () => { finish({}); });
    expect(screen.getByRole("tab", { name: "Job Results" }).getAttribute("aria-selected")).toBe("true");
  });

  it("asks before cancelling a running job, without opening the run", async () => {
    listJobRuns.mockResolvedValue([RUNNING]);
    const showConfirm = vi.fn().mockResolvedValue(false);
    render(<OrchestrationPanel showConfirm={showConfirm} />);
    fireEvent.click(screen.getByRole("tab", { name: "Job Results" }));

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    expect(showConfirm.mock.calls[0][1]).toContain("daily");
    expect(cancelJobRun).not.toHaveBeenCalled();
    expect(getJobRun).not.toHaveBeenCalled();
  });

  it("cancels once confirmed", async () => {
    listJobRuns.mockResolvedValue([RUNNING]);
    cancelJobRun.mockResolvedValue({ status: "cancelled" });
    render(<OrchestrationPanel showConfirm={vi.fn().mockResolvedValue(true)} />);
    fireEvent.click(screen.getByRole("tab", { name: "Job Results" }));

    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(cancelJobRun).toHaveBeenCalledWith("r1"));
  });

  it("opens a run from the keyboard and reruns it once", async () => {
    listJobRuns.mockResolvedValue([{ ...RUNNING, status: "success" }]);
    getJobRun.mockResolvedValue({ ...RUNNING, status: "success", step_details: [] });
    let finish;
    runJob.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<OrchestrationPanel showConfirm={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Job Results" }));

    const row = await screen.findByRole("button", { name: /Open run of daily/ });
    fireEvent.keyDown(row, { key: "Enter" });
    expect(getJobRun).toHaveBeenCalledWith("r1");

    const rerun = await screen.findByRole("button", { name: "Rerun" });
    fireEvent.click(rerun);
    fireEvent.click(rerun);
    expect(runJob).toHaveBeenCalledTimes(1);
    expect(rerun.disabled).toBe(true);
    await act(async () => { finish({}); });
  });

  it("names its icon-only dismiss button", async () => {
    listJobs.mockRejectedValue(new Error("cannot read orchestration/"));
    render(<OrchestrationPanel showConfirm={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss error" }));
    expect(screen.queryByText("cannot read orchestration/")).toBeNull();
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("./api", () => ({
  api: {
    getGitStatus: vi.fn(),
    getGitBranches: vi.fn(),
    getGitRemote: vi.fn(),
    getGitLog: vi.fn(),
    getGitStash: vi.fn(),
    getGitDiff: vi.fn(),
    gitDiscard: vi.fn(),
    initGit: vi.fn(),
  },
}));

import { api } from "./api";
import GitPanel from "./GitPanel";

const A = "transform/silver/a.sql";
const B = "transform/silver/b.sql";

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getGitStatus.mockResolvedValue({
    is_git_repo: true,
    branch: "main",
    dirty: true,
    files: [
      { path: A, status: "M", staged: false },
      { path: B, status: "M", staged: false },
    ],
  });
  api.getGitBranches.mockResolvedValue([{ name: "main", is_current: true, is_remote: false }]);
  api.getGitRemote.mockResolvedValue({ url: null });
  api.getGitLog.mockResolvedValue([]);
  api.getGitStash.mockResolvedValue([]);
  api.gitDiscard.mockResolvedValue({ status: "discarded" });
});

describe("discarding a file's changes", () => {
  it("asks first and leaves the file alone on Cancel", async () => {
    const showConfirm = vi.fn().mockResolvedValue(false);
    render(<GitPanel showConfirm={showConfirm} />);

    await userEvent.click(await screen.findByLabelText(`Discard changes to ${A}`));

    await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(
      "Discard Changes", expect.stringContaining(A), "Discard", true,
    ));
    expect(api.gitDiscard).not.toHaveBeenCalled();
  });

  it("discards once confirmed", async () => {
    render(<GitPanel showConfirm={vi.fn().mockResolvedValue(true)} />);
    await userEvent.click(await screen.findByLabelText(`Discard changes to ${A}`));
    await waitFor(() => expect(api.gitDiscard).toHaveBeenCalledWith([A]));
  });

  it("falls back to window.confirm without showConfirm", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<GitPanel />);
    await userEvent.click(await screen.findByLabelText(`Discard changes to ${A}`));
    expect(confirm).toHaveBeenCalled();
    expect(api.gitDiscard).not.toHaveBeenCalled();
    confirm.mockRestore();
  });
});

describe("the diff viewer", () => {
  it("keeps the newer file's diff when an older request answers last", async () => {
    const slow = deferred();
    api.getGitDiff.mockImplementation((path) => (path === A ? slow.promise : Promise.resolve({ diff: "+b line" })));
    render(<GitPanel />);

    await userEvent.click(await screen.findByRole("button", { name: `View diff of ${A}` }));
    await userEvent.click(screen.getByRole("button", { name: `View diff of ${B}` }));
    expect(await screen.findByText("+b line")).toBeTruthy();
    await act(async () => { slow.resolve({ diff: "+a line" }); });

    expect(screen.queryByText("+a line")).toBeNull();
    expect(screen.getByText("+b line")).toBeTruthy();
  });

  it("opens from the keyboard, with the path as the title", async () => {
    api.getGitDiff.mockResolvedValue({ diff: "+a line" });
    render(<GitPanel />);
    const file = await screen.findByRole("button", { name: `View diff of ${A}` });
    expect(file).toHaveAttribute("title", A);

    file.focus();
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByText("+a line")).toBeTruthy();
    expect(file).toHaveAttribute("aria-expanded", "true");
  });
});

describe("initializing a repository", () => {
  it("sends one init however often the button is clicked", async () => {
    api.getGitStatus.mockResolvedValue({ is_git_repo: false });
    api.initGit.mockReturnValue(new Promise(() => {}));
    render(<GitPanel />);
    const button = await screen.findByRole("button", { name: "Initialize git repository" });

    await userEvent.click(button);
    await userEvent.click(button);

    expect(api.initGit).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Initializing...");
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

const getPr = vi.fn();
const addPrComment = vi.fn();

const PR = {
  id: "PR-1", title: "Only paid orders", description: "", status: "open", author: "astrid",
  head_ref: "feature/paid", base_ref: "main", approvers: [], current_approvers: [], change_requesters: [],
  comments: [], created_at: null, require_approval: true,
};

vi.mock("./api", () => ({
  api: {
    listPrs: () => Promise.resolve([PR]),
    getPrStateStatus: () => Promise.resolve({ is_git_repo: true, dirty: false, unpushed_count: 0 }),
    getPr: (...args) => getPr(...args),
    getPrBuild: () => Promise.resolve(null),
    getPrDiff: () => Promise.resolve({ files: [] }),
    addPrComment: (...args) => addPrComment(...args),
  },
}));
vi.mock("./AuthContext", () => ({ useAuth: () => ({ currentUser: { username: "astrid" } }) }));

const { default: ReviewsPanel } = await import("./ReviewsPanel");

beforeEach(() => {
  getPr.mockReset().mockResolvedValue(PR);
  addPrComment.mockReset();
});

describe("ReviewsPanel", () => {
  it("filters by status with a real tab list", async () => {
    render(<ReviewsPanel showConfirm={vi.fn()} />);
    await screen.findByText("Only paid orders");
    const merged = screen.getByRole("tab", { name: "Merged" });
    expect(screen.getByRole("tab", { name: "Open" }).getAttribute("aria-selected")).toBe("true");

    fireEvent.click(merged);
    expect(merged.getAttribute("aria-selected")).toBe("true");
    expect(screen.queryByText("Only paid orders")).toBeNull();
  });

  it("opens a pull request from the keyboard and posts a comment once", async () => {
    let finish;
    addPrComment.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<ReviewsPanel showConfirm={vi.fn()} />);

    fireEvent.keyDown(await screen.findByRole("button", { name: /Open pull request PR-1/ }), { key: "Enter" });
    fireEvent.click(await screen.findByRole("tab", { name: "Comments" }));
    fireEvent.change(screen.getByPlaceholderText("Leave a comment…"), { target: { value: "Looks good" } });

    const post = screen.getByRole("button", { name: "Post comment" });
    fireEvent.click(post);
    fireEvent.click(post);

    expect(addPrComment).toHaveBeenCalledTimes(1);
    expect(post.disabled).toBe(true);
    expect(post.textContent).toBe("Posting…");
    await act(async () => { finish({}); });
  });
});

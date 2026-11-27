import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const discardChangeSet = vi.fn();

vi.mock("./api", () => ({
  api: {
    discardChangeSet: (...args) => discardChangeSet(...args),
    applyChangeSet: vi.fn(),
    verifyChangeSet: vi.fn(),
  },
}));

const { default: ChangeSetCard } = await import("./ChangeSetCard");

const CHANGESET = {
  id: "cs1", revision: 1, source: "agent:claude", title: "only paid orders", status: "ready",
  files: [{ path: "transform/bronze/orders.sql", action: "modify" }], ignored: [], stale_files: [],
  report: {
    ok: true,
    checks: [{ name: "unit_tests", status: "pass", summary: "1 test(s) passed", details: ["orders: ok"] }],
    diffs: [],
  },
};

describe("ChangeSetCard", () => {
  beforeEach(() => discardChangeSet.mockReset());

  it("asks before discarding, and keeps the change set when the answer is no", async () => {
    const showConfirm = vi.fn().mockResolvedValue(false);
    render(<ChangeSetCard changeset={CHANGESET} showConfirm={showConfirm} />);

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(showConfirm).toHaveBeenCalledTimes(1));
    expect(showConfirm.mock.calls[0][3]).toBe(true); // shown as a destructive action
    expect(discardChangeSet).not.toHaveBeenCalled();
    expect(screen.getByText("Ready to apply")).toBeInTheDocument();
  });

  it("discards once confirmed", async () => {
    discardChangeSet.mockResolvedValue({ ...CHANGESET, status: "discarded" });
    render(<ChangeSetCard changeset={CHANGESET} showConfirm={vi.fn().mockResolvedValue(true)} />);

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(await screen.findByText("Discarded")).toBeInTheDocument();
    expect(discardChangeSet).toHaveBeenCalledWith("cs1");
  });

  it("falls back to the browser's confirm when it is not given showConfirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      render(<ChangeSetCard changeset={CHANGESET} />);
      fireEvent.click(screen.getByRole("button", { name: "Discard" }));
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1));
      expect(discardChangeSet).not.toHaveBeenCalled();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it("opens a check's details from the keyboard", () => {
    render(<ChangeSetCard changeset={CHANGESET} />);
    const check = screen.getByRole("button", { name: /Unit tests/ });
    expect(check.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("orders: ok")).toBeNull();

    fireEvent.keyDown(check, { key: "Enter" });

    expect(check.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("orders: ok")).toBeInTheDocument();
  });
});

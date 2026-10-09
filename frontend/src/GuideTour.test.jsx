import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import GuideTour from "./GuideTour";

const STEPS = [
  { title: "Step one", description: "First", position: "center" },
  { title: "Step two", description: "Second", position: "center" },
  { title: "Step three", description: "Third", position: "center" },
];

describe("GuideTour keyboard", () => {
  it("advances one step when Enter is pressed on the focused Next button", () => {
    render(<GuideTour steps={STEPS} isOpen onComplete={() => {}} />);
    const next = screen.getByRole("button", { name: "Next" });
    next.focus();

    // A real Enter on a button fires keydown (which bubbles to window) and
    // then the button's click; the tour must count that as one advance.
    fireEvent.keyDown(next, { key: "Enter" });
    fireEvent.click(next);

    expect(screen.getByText("Step two")).toBeInTheDocument();
  });

  it("still advances on Enter when nothing interactive has focus", () => {
    render(<GuideTour steps={STEPS} isOpen onComplete={() => {}} />);
    fireEvent.keyDown(window, { key: "Enter" });
    expect(screen.getByText("Step two")).toBeInTheDocument();
  });

  it("closes on Escape even from a focused button", () => {
    const onComplete = vi.fn();
    render(<GuideTour steps={STEPS} isOpen onComplete={onComplete} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Next" }), { key: "Escape" });
    expect(onComplete).toHaveBeenCalled();
  });
});

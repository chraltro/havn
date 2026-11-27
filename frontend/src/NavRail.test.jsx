import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import NavRail from "./NavRail";
import { SECTIONS } from "./navigation";

describe("NavRail preloading", () => {
  it("asks for a destination's page on hover and on keyboard focus, before the click", () => {
    const onPreload = vi.fn();
    const onNavigate = vi.fn();
    render(<NavRail sections={SECTIONS} activeSection="Overview" onNavigate={onNavigate} onPreload={onPreload} onToggleAgent={() => {}} />);

    fireEvent.mouseEnter(screen.getByRole("button", { name: /^Data/ }));
    expect(onPreload).toHaveBeenLastCalledWith("Data");
    fireEvent.focus(screen.getByRole("button", { name: /^Settings/ }));
    expect(onPreload).toHaveBeenLastCalledWith("Configure");
    fireEvent.mouseEnter(screen.getByRole("button", { name: /^Agent/ }));
    expect(onPreload).toHaveBeenLastCalledWith("agent");
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("works without an onPreload handler", () => {
    render(<NavRail sections={SECTIONS} activeSection="Overview" onNavigate={() => {}} onToggleAgent={() => {}} />);
    expect(() => fireEvent.mouseEnter(screen.getByRole("button", { name: /^Build/ }))).not.toThrow();
  });
});

import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import CommandPalette from "./CommandPalette";

// jsdom does not implement scrollIntoView, which the palette uses to keep the
// selected result visible.
Element.prototype.scrollIntoView ||= () => {};

function palette(props = {}) {
  return (
    <CommandPalette isOpen onClose={() => {}} files={[]} tables={[]} streams={{}}
      onOpenFile={() => {}} onNavigate={() => {}} onRunStream={() => {}} {...props} />
  );
}

describe("CommandPalette", () => {
  beforeEach(() => localStorage.clear());

  it("puts the caret in the search field when it opens, even if focus is elsewhere", async () => {
    // Opening it while the code editor had focus left the palette open with
    // typing still going into the editor. Simulate something holding focus.
    const other = document.createElement("textarea");
    document.body.appendChild(other);
    other.focus();

    render(palette());
    const input = screen.getByLabelText("Command palette search");
    await waitFor(() => expect(document.activeElement).toBe(input));
    other.remove();
  });

  it("is a modal dialog whose search box points at the highlighted result", () => {
    render(palette());
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");

    const input = screen.getByRole("combobox", { name: "Command palette search" });
    const listbox = screen.getByRole("listbox");
    expect(input.getAttribute("aria-controls")).toBe(listbox.id);

    const options = screen.getAllByRole("option");
    expect(options.length).toBeGreaterThan(1);
    expect(input.getAttribute("aria-activedescendant")).toBe(options[0].id);
    expect(options[0].getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(input.getAttribute("aria-activedescendant")).toBe(options[1].id);
    expect(options[1].getAttribute("aria-selected")).toBe("true");
    expect(options[0].getAttribute("aria-selected")).toBe("false");
  });

  it("keeps Tab inside the palette instead of moving to the page behind it", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">behind the palette</button>
        {palette()}
      </>,
    );
    const input = screen.getByRole("combobox", { name: "Command palette search" });
    input.focus();
    await user.tab();
    expect(document.activeElement).toBe(input);
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(input);
  });

  it("closes once on Escape", () => {
    const onClose = vi.fn();
    render(palette({ onClose }));
    fireEvent.keyDown(screen.getByRole("combobox", { name: "Command palette search" }), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("says so when nothing matches, without an empty listbox", () => {
    render(palette());
    const input = screen.getByRole("combobox", { name: "Command palette search" });
    fireEvent.change(input, { target: { value: "zzzzqqqq" } });
    expect(screen.getByRole("status").textContent).toBe("No results");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input.getAttribute("aria-expanded")).toBe("false");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
  });
});

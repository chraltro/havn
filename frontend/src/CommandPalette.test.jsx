import { describe, it, expect } from "vitest";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

import CommandPalette from "./CommandPalette";

// jsdom does not implement scrollIntoView, which the palette uses to keep the
// selected result visible.
Element.prototype.scrollIntoView ||= () => {};

describe("CommandPalette", () => {
  it("puts the caret in the search field when it opens, even if focus is elsewhere", async () => {
    // Opening it while the code editor had focus left the palette open with
    // typing still going into the editor. Simulate something holding focus.
    const other = document.createElement("textarea");
    document.body.appendChild(other);
    other.focus();

    render(
      <CommandPalette isOpen onClose={() => {}} files={[]} tables={[]} streams={{}}
        onOpenFile={() => {}} onNavigate={() => {}} onRunStream={() => {}} />,
    );
    const input = screen.getByLabelText("Command palette search");
    await waitFor(() => expect(document.activeElement).toBe(input));
    other.remove();
  });
});

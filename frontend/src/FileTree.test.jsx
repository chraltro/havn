import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import FileTree from "./FileTree";

const FILES = [
  {
    type: "dir", name: "transform", path: "transform",
    children: [{ type: "file", name: "orders.sql", path: "transform/orders.sql" }],
  },
  { type: "file", name: "project.yml", path: "project.yml" },
];

function tree(props = {}) {
  return (
    <FileTree files={FILES} onSelect={vi.fn()} activeFile={null}
      onNewFile={vi.fn()} onDeleteFile={vi.fn()} onMoveFile={vi.fn()} {...props} />
  );
}

describe("FileTree keyboard access", () => {
  it("lets the keyboard reach and open a file, keeping the tree roles", () => {
    const onSelect = vi.fn();
    render(tree({ onSelect }));
    const row = screen.getByRole("treeitem", { name: /project\.yml/ });
    expect(row.tabIndex).toBe(0);

    fireEvent.keyDown(row, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("project.yml");

    // Keys on the row's own delete button do not also open the file.
    fireEvent.keyDown(screen.getByRole("button", { name: "Delete project.yml" }), { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("shows a file's delete button while the row has keyboard focus", () => {
    render(tree());
    const row = screen.getByRole("treeitem", { name: /project\.yml/ });
    const del = screen.getByRole("button", { name: "Delete project.yml" });
    expect(del.style.opacity).toBe("0");

    fireEvent.focus(row);
    expect(del.style.opacity).toBe("1");
    expect(del.style.pointerEvents).toBe("auto");

    fireEvent.blur(row, { relatedTarget: document.body });
    expect(del.style.opacity).toBe("0");
  });

  it("expands a folder from the keyboard and shows its + button on focus", () => {
    render(tree());
    expect(screen.queryByRole("treeitem", { name: /orders\.sql/ })).toBeNull();
    const folder = screen.getByRole("button", { name: "transform" });
    const add = screen.getByRole("button", { name: "New file in transform" });
    expect(folder.getAttribute("aria-expanded")).toBe("false");
    expect(add.style.opacity).toBe("0");

    fireEvent.focus(folder);
    expect(add.style.opacity).toBe("1");

    fireEvent.keyDown(folder, { key: "Enter" });
    expect(folder.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("treeitem", { name: /orders\.sql/ })).toBeTruthy();
  });
});

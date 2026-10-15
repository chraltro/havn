import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

const auth = { authEnabled: true, currentUser: { username: "ingrid", role: "viewer" } };

vi.mock("./api", () => ({
  api: {
    getMaskingMethods: () => Promise.resolve([]),
    listMaskingPolicies: () => Promise.resolve([{
      id: "p1", schema_name: "landing", table_name: "people", column_name: "ssn",
      method: "redact", method_config: {}, exempted_roles: ["admin"],
    }]),
    listTables: () => Promise.resolve([]),
  },
}));
vi.mock("./AuthContext", () => ({ useAuth: () => auth }));

const { default: MaskingPanel } = await import("./MaskingPanel");

describe("MaskingPanel", () => {
  it("offers no policy controls to a non-admin, whom the API would refuse", async () => {
    auth.currentUser = { username: "ingrid", role: "editor" };
    render(<MaskingPanel showConfirm={vi.fn()} />);
    expect(await screen.findByText("ssn")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Add Policy/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("offers them to an admin", async () => {
    auth.currentUser = { username: "astrid", role: "admin" };
    render(<MaskingPanel showConfirm={vi.fn()} />);
    expect(await screen.findByText("ssn")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /Add Policy/ }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Delete" })).toBeTruthy();
  });

  it("sorts from header buttons and says which way", async () => {
    auth.currentUser = { username: "astrid", role: "admin" };
    render(<MaskingPanel showConfirm={vi.fn()} />);
    await screen.findByText("ssn");
    const sortColumn = screen.getByRole("button", { name: "Column" });
    const th = sortColumn.closest("th");
    expect(th.hasAttribute("aria-sort")).toBe(false);

    fireEvent.click(sortColumn);
    expect(th.getAttribute("aria-sort")).toBe("ascending");
    fireEvent.click(sortColumn);
    expect(th.getAttribute("aria-sort")).toBe("descending");
  });

  it("brings the new-policy form into view with focus in it, and Condition opens from the keyboard", async () => {
    auth.currentUser = { username: "astrid", role: "admin" };
    const original = Element.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      render(<MaskingPanel showConfirm={vi.fn()} />);
      await screen.findByText("ssn");
      fireEvent.click(screen.getAllByRole("button", { name: /Add Policy/ })[0]);

      const schema = await screen.findByRole("combobox", { name: "Schema" });
      expect(document.activeElement).toBe(schema);
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });

      const toggle = screen.getByRole("button", { name: /Condition/ });
      expect(toggle.getAttribute("aria-expanded")).toBe("false");
      expect(screen.queryByLabelText("Condition column")).toBeNull();
      fireEvent.click(toggle);
      expect(toggle.getAttribute("aria-expanded")).toBe("true");
      expect(screen.getByLabelText("Condition column")).toBeTruthy();
      expect(screen.getByLabelText("Condition value")).toBeTruthy();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("names the button that dismisses an error", async () => {
    auth.currentUser = { username: "astrid", role: "admin" };
    const { api } = await import("./api");
    const list = api.listMaskingPolicies;
    api.listMaskingPolicies = () => Promise.reject(new Error("boom"));
    try {
      render(<MaskingPanel showConfirm={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Dismiss error" }));
      expect(screen.queryByText("boom")).toBeNull();
    } finally {
      api.listMaskingPolicies = list;
    }
  });
});

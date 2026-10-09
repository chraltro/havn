import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";

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
});

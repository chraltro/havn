import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen } from "@testing-library/react";

const auth = { authEnabled: true, currentUser: { username: "ingrid", role: "viewer" } };

vi.mock("./api", () => ({
  api: {
    getGovernance: () => Promise.resolve({
      classifications: [{ relation: "gold.people", columns: [{ column: "ssn", from: ["silver.people"], masked: false }] }],
      row_policies: [{ relation: "silver.customers", inherited_from: null, deny: false }],
      declassified: [], notes: [],
    }),
    listRowPolicies: () => Promise.resolve([{
      id: "r1", schema_name: "silver", table_name: "customers", filter_sql: "region = havn_attr('region')",
      applies_to_roles: ["viewer"], applies_to_users: [], exempted_roles: ["admin"], exempted_users: [], enabled: true,
    }]),
    listTables: () => Promise.resolve([]),
    listUsers: () => Promise.resolve([]),
  },
}));
vi.mock("./AuthContext", () => ({ useAuth: () => auth }));

const { default: GovernancePanel, policiesFor } = await import("./GovernancePanel");

describe("GovernancePanel", () => {
  it("offers a non-admin no policy controls", async () => {
    auth.currentUser = { username: "ingrid", role: "editor" };
    render(<GovernancePanel showConfirm={vi.fn()} />);
    expect(await screen.findByText("silver.customers")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Add Row Policy/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("offers them to an admin, with the filter visible", async () => {
    auth.currentUser = { username: "astrid", role: "admin" };
    render(<GovernancePanel showConfirm={vi.fn()} />);
    expect(await screen.findByText("region = havn_attr('region')")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Add Row Policy/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Delete" })).toBeTruthy();
  });

  it("works out which policies apply to a user", () => {
    const ps = [
      { id: "a", applies_to_roles: ["viewer"], exempted_roles: ["admin"], enabled: true },
      { id: "b", applies_to_roles: [], applies_to_users: [], exempted_users: ["bo"], enabled: true },
      { id: "c", applies_to_roles: [], enabled: false },
    ];
    expect(policiesFor(ps, { username: "ingrid", role: "viewer" }).map((p) => p.id)).toEqual(["a", "b"]);
    expect(policiesFor(ps, { username: "bo", role: "editor" })).toEqual([]);
  });
});

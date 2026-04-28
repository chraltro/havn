import { describe, it, expect } from "vitest";
import { isIdentifierLikeName } from "./SortableTable";

describe("isIdentifierLikeName", () => {
  it("matches columns ending in _id", () => {
    expect(isIdentifierLikeName("account_id")).toBe(true);
    expect(isIdentifierLikeName("customer_id")).toBe(true);
    expect(isIdentifierLikeName("branch_id")).toBe(true);
  });

  it("matches a column literally named 'id'", () => {
    expect(isIdentifierLikeName("id")).toBe(true);
    expect(isIdentifierLikeName("ID")).toBe(true);
  });

  it("matches number-shaped identifier suffixes", () => {
    expect(isIdentifierLikeName("invoice_no")).toBe(true);
    expect(isIdentifierLikeName("order_nr")).toBe(true);
    expect(isIdentifierLikeName("phone_number")).toBe(true);
    expect(isIdentifierLikeName("product_code")).toBe(true);
    expect(isIdentifierLikeName("postal_code")).toBe(true);
    expect(isIdentifierLikeName("zip")).toBe(true);
    expect(isIdentifierLikeName("zipcode")).toBe(true);
  });

  it("does not match value/quantity columns that happen to contain id-like substrings", () => {
    // "paid", "valid", "width" all contain "id" but not as a token boundary.
    expect(isIdentifierLikeName("paid")).toBe(false);
    expect(isIdentifierLikeName("valid")).toBe(false);
    expect(isIdentifierLikeName("width")).toBe(false);
    // "amount", "balance", "rate", "price" — the things we want to KEEP
    // formatted with thousand separators.
    expect(isIdentifierLikeName("amount")).toBe(false);
    expect(isIdentifierLikeName("balance")).toBe(false);
    expect(isIdentifierLikeName("running_balance_nok")).toBe(false);
    expect(isIdentifierLikeName("rate")).toBe(false);
    expect(isIdentifierLikeName("price")).toBe(false);
    expect(isIdentifierLikeName("count")).toBe(false);
  });

  it("handles empty/null", () => {
    expect(isIdentifierLikeName("")).toBe(false);
    expect(isIdentifierLikeName(null)).toBe(false);
    expect(isIdentifierLikeName(undefined)).toBe(false);
  });
});

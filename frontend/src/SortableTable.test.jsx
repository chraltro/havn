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

// Layout cannot be measured in jsdom, so these pin the STRUCTURE that makes a
// sticky header work in a real browser: exactly one scroll container around
// the table, bounded so it can scroll vertically itself, with the sticky
// element inside it. The original bug was an overflow-x:auto wrapper (which
// forces overflow-y:auto) that never scrolled, so the header stuck to nothing.
import { render, screen, fireEvent } from "@testing-library/react";
import SortableTable from "./SortableTable";

function scrollContainersAround(el) {
  const found = [];
  for (let p = el.parentElement; p; p = p.parentElement) {
    const s = p.style || {};
    if (/(auto|scroll)/.test(s.overflow || "") || /(auto|scroll)/.test(s.overflowX || "")
      || /(auto|scroll)/.test(s.overflowY || "")) found.push(p);
  }
  return found;
}

describe("SortableTable sticky header structure", () => {
  const columns = ["id", "name"];
  const few = Array.from({ length: 20 }, (_, i) => [i, `n${i}`]);
  const many = Array.from({ length: 500 }, (_, i) => [i, `n${i}`]);

  it("regular body: sticky thead inside a single bounded scroller", () => {
    // An explicit cap: jsdom cannot parse the min() default, browsers can.
    const { container } = render(<SortableTable columns={columns} rows={few} maxHeight={400} />);
    const thead = container.querySelector("thead");
    expect(thead.style.position).toBe("sticky");
    expect(thead.style.top).toBe("0px");
    const scrollers = scrollContainersAround(thead);
    expect(scrollers).toHaveLength(1);
    expect(scrollers[0].style.overflow).toBe("auto");
    expect(scrollers[0].style.maxHeight).toBe("400px");
    // Cells do not stick vertically on their own (the thead does).
    container.querySelectorAll("thead th").forEach((th) => {
      expect(th.style.top).toBe("");
    });
  });

  it("virtual body: the header TABLE is sticky, inside one bounded scroller", () => {
    const { container } = render(<SortableTable columns={columns} rows={many} />);
    const headerTable = container.querySelector("thead").closest("table");
    expect(headerTable.style.position).toBe("sticky");
    expect(headerTable.style.top).toBe("0px");
    const scrollers = scrollContainersAround(headerTable);
    expect(scrollers).toHaveLength(1);
    // No viewport-height guess: a 100vh-based cap overflows any pane that is
    // shorter than the window and brings back the outer scrollbar.
    expect(scrollers[0].style.maxHeight).not.toMatch(/100vh/);
  });

  it("fill mode: the scroller flexes into a bounded parent instead of capping", () => {
    const { container } = render(<SortableTable fill columns={columns} rows={few} />);
    const scroller = scrollContainersAround(container.querySelector("thead"))[0];
    expect(scroller.style.maxHeight).toBe("");
    expect(scroller.style.minHeight).toBe("0px");
    expect(scroller.style.flex).toMatch(/^1 1 0/);
  });
});

describe("SortableTable keyboard and clipboard", () => {
  const cols = ["name", "n"];
  const rows = [["b", 2], ["a", 1], ["c", 3]];

  function firstColumnValues() {
    return [...document.querySelectorAll("tbody tr")].map((tr) => tr.querySelectorAll("td")[0].textContent);
  }

  it("sorts from the keyboard", () => {
    render(<SortableTable columns={cols} rows={rows} />);
    const header = screen.getByRole("columnheader", { name: /name/ });
    expect(header).toHaveAttribute("tabindex", "0");
    fireEvent.keyDown(header, { key: "Enter" });
    expect(firstColumnValues()).toEqual(["a", "b", "c"]);
    expect(header).toHaveAttribute("aria-sort", "ascending");
  });

  it("opens the pin menu from the keyboard and closes it with Escape", () => {
    render(<SortableTable columns={cols} rows={rows} />);
    const header = screen.getByRole("columnheader", { name: /name/ });
    header.focus();
    fireEvent.contextMenu(header);
    const item = screen.getByRole("menuitem", { name: "Pin Left" });
    expect(document.activeElement).toBe(item);
    fireEvent.keyDown(document.activeElement, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(header);
  });

  it("does not throw on a cell click where the clipboard API is missing", () => {
    // Plain http on a LAN address is not a secure context: no navigator.clipboard.
    const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    try {
      render(<SortableTable columns={cols} rows={rows} />);
      expect(() => fireEvent.click(screen.getByText("b"))).not.toThrow();
    } finally {
      if (original) Object.defineProperty(navigator, "clipboard", original);
      else delete navigator.clipboard;
    }
  });
});

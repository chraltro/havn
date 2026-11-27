import { describe, it, expect } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import PivotTable from "./PivotTable";

const COLUMNS = ["region", "amount"];
const ROWS = [["north", 3], ["south", 1], ["north", 2]];

describe("PivotTable", () => {
  it("sorts from header buttons and reports the order", () => {
    render(<PivotTable columns={COLUMNS} rows={ROWS} config={{}} />);
    const header = screen.getByRole("columnheader", { name: "amount" });
    expect(header).toHaveAttribute("aria-sort", "none");

    fireEvent.click(screen.getByRole("button", { name: "amount" }));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: "amount" }));
    expect(header).toHaveAttribute("aria-sort", "descending");
  });

  it("toggles groups with buttons that say whether they are open", () => {
    render(<PivotTable columns={COLUMNS} rows={ROWS} config={{ group_column: "region" }} />);
    const north = screen.getByRole("button", { name: /^north \(2\)/ });
    expect(north).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("row")).toHaveLength(1 + 2 + 3); // header, 2 groups, 3 rows

    fireEvent.click(north);
    expect(north).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByRole("row")).toHaveLength(1 + 2 + 1);
  });
});

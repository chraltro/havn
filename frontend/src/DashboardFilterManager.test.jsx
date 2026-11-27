import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

vi.mock("./api", () => ({ api: { getAutocomplete: vi.fn(() => Promise.resolve({ columns: [] })) } }));

const { default: DashboardContext } = await import("./DashboardContext");
const { default: DashboardFilterManager } = await import("./DashboardFilterManager");

const FILTERS = [{ id: "f1", label: "Region", type: "text", column: "region" }];

async function renderManager(saveDashboard, onClose) {
  render(
    <DashboardContext.Provider value={{ dashboard: { id: "d1", filters: FILTERS }, saveDashboard }}>
      <DashboardFilterManager onClose={onClose} />
    </DashboardContext.Provider>,
  );
  await act(async () => {}); // the column picker's lookup
}

describe("DashboardFilterManager", () => {
  it("stays open with the edits when the save fails", async () => {
    const saveDashboard = vi.fn(() => Promise.resolve(false));
    const onClose = vi.fn();
    await renderManager(saveDashboard, onClose);
    fireEvent.change(screen.getByDisplayValue("Region"), { target: { value: "Sales region" } });

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save Filters" })); });

    expect(screen.getByRole("alert")).toHaveTextContent("Filters not saved");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("Sales region")).toBeInTheDocument();
  });

  it("closes once the filters are saved", async () => {
    const saveDashboard = vi.fn(() => Promise.resolve(true));
    const onClose = vi.fn();
    await renderManager(saveDashboard, onClose);

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Save Filters" })); });

    expect(saveDashboard).toHaveBeenCalledWith({ filters: FILTERS });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

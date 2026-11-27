import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, act, within } from "@testing-library/react";

const listDashboards = vi.fn();
const listDashboardTemplates = vi.fn();
const createDashboard = vi.fn();
const cloneDashboard = vi.fn();

vi.mock("./api", () => ({
  api: {
    listDashboards: (...a) => listDashboards(...a),
    listDashboardTemplates: (...a) => listDashboardTemplates(...a),
    createDashboard: (...a) => createDashboard(...a),
    cloneDashboard: (...a) => cloneDashboard(...a),
  },
}));

const { DashboardProvider } = await import("./DashboardContext");
const { default: DashboardListPanel } = await import("./DashboardListPanel");

const SALES = { id: "d1", name: "Sales", description: "Revenue by region and channel", widget_count: 2, updated_at: null };

let onOpen;
function renderPanel() {
  return render(
    <DashboardProvider>
      <DashboardListPanel onOpenDashboard={onOpen} />
    </DashboardProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, "error").mockImplementation(() => {});
  onOpen = vi.fn();
  listDashboards.mockResolvedValue([SALES]);
  listDashboardTemplates.mockResolvedValue([]);
});

describe("DashboardListPanel", () => {
  it("shows a failed load as an error with Retry, not as an empty list", async () => {
    listDashboards.mockRejectedValueOnce(new Error("Error (500): database is locked"));
    renderPanel();

    expect(await screen.findByText("Couldn't load dashboards")).toBeInTheDocument();
    expect(screen.getByText("Error (500): database is locked")).toBeInTheDocument();
    expect(screen.queryByText("No dashboards yet")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Sales")).toBeInTheDocument();
  });

  it("still lists dashboards when the template list fails", async () => {
    listDashboardTemplates.mockRejectedValueOnce(new Error("nope"));
    renderPanel();
    expect(await screen.findByText("Sales")).toBeInTheDocument();
  });

  it("opens a card from the keyboard and names its menu button", async () => {
    renderPanel();
    const card = await screen.findByRole("button", { name: "Open dashboard Sales" });

    fireEvent.keyDown(card, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledWith("d1");

    fireEvent.click(screen.getByRole("button", { name: "Actions for Sales" }));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it("reports a failed clone", async () => {
    cloneDashboard.mockRejectedValueOnce(new Error("Error (403): read only"));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Actions for Sales" }));

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Clone" })); });

    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't clone the dashboard: Error (403): read only");
  });

  it("runs New Dashboard as a dialog: Enter can't create twice, Escape closes", async () => {
    let resolveCreate;
    createDashboard.mockImplementation(() => new Promise(r => { resolveCreate = r; }));
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "+ New Dashboard" }));

    const dialog = screen.getByRole("dialog", { name: "New Dashboard" });
    const input = within(dialog).getByPlaceholderText("Dashboard name");
    fireEvent.change(input, { target: { value: "Ops" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(createDashboard).toHaveBeenCalledTimes(1);
    await act(async () => resolveCreate({ id: "d2" }));
    expect(onOpen).toHaveBeenCalledWith("d2");

    fireEvent.click(screen.getByRole("button", { name: "+ New Dashboard" }));
    fireEvent.keyDown(screen.getByPlaceholderText("Dashboard name"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

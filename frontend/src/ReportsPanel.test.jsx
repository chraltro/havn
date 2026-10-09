import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listReports = vi.fn();
const sendReport = vi.fn();
const reportCapabilities = vi.fn();
let currentUser = { username: "eve", role: "editor" };

vi.mock("./api", () => ({
  api: {
    listReports: (...a) => listReports(...a),
    sendReport: (...a) => sendReport(...a),
    reportCapabilities: (...a) => reportCapabilities(...a),
    listDashboards: vi.fn(async () => []),
    getDashboard: vi.fn(),
    getReport: vi.fn(async () => ({ deliveries: [] })),
  },
}));
vi.mock("./AuthContext", () => ({ useAuth: () => ({ currentUser }) }));

const { default: ReportsPanel, describeCron, draftToBody } = await import("./ReportsPanel");

const REPORT = {
  id: "r1", name: "Daily sales", dashboard_id: "d1", dashboard_name: "Sales", schedule: "0 7 * * 1-5",
  enabled: true, owner: "eve", recipients: { email: ["a@example.com"], slack: ["default"] }, formats: ["pdf"],
  filters: {}, parameters: {}, condition: null, subject: "", message: "", last_status: "sent",
  last_run_at: "2026-10-09T07:00:00", next_run_at: "2026-10-10T07:00:00",
};

beforeEach(() => {
  vi.clearAllMocks();
  reportCapabilities.mockResolvedValue({ charts: false, email_configured: true, slack_default_configured: true, allowed_recipient_domains: [] });
  listReports.mockResolvedValue([REPORT, { ...REPORT, id: "r2", name: "Someone else's", owner: "ada" }]);
});

describe("describeCron", () => {
  it("reads common schedules in plain words", () => {
    expect(describeCron("0 7 * * *")).toBe("Daily at 07:00");
    expect(describeCron("30 6 * * 1-5")).toBe("Weekdays at 06:30");
    expect(describeCron("0 7 * * 1")).toBe("Mondays at 07:00");
    expect(describeCron("0 7 1 * *")).toBe("Monthly on day 1 at 07:00");
    expect(describeCron("*/5 * * * *")).toBe("*/5 * * * *");
    expect(describeCron("")).toBe("Manual only");
  });
});

describe("draftToBody", () => {
  it("splits recipients and builds the condition", () => {
    const body = draftToBody({
      name: " Alert ", dashboard_id: "d1", widget_id: "", schedule: "", enabled: true,
      emails: "a@example.com, b@example.com\nc@example.com", slackDefault: true, slackExtra: "${TEAM}",
      formats: ["csv"], filters: { region: "", country: "NO", range: { min: null, max: null } },
      condition: { widget_id: "w1", op: "lt", value: "100" }, subject: "", message: "",
    });
    expect(body.name).toBe("Alert");
    expect(body.schedule).toBe(null);
    expect(body.recipients).toEqual({ email: ["a@example.com", "b@example.com", "c@example.com"], slack: ["default", "${TEAM}"] });
    expect(body.filters).toEqual({ country: "NO" });
    expect(body.condition).toEqual({ widget_id: "w1", op: "lt", value: 100 });
  });
});

describe("ReportsPanel", () => {
  it("lists reports and only offers actions on the user's own", async () => {
    currentUser = { username: "eve", role: "editor" };
    render(<ReportsPanel />);
    expect(await screen.findByText("Daily sales")).toBeInTheDocument();
    expect(screen.getByText("Someone else's")).toBeInTheDocument();
    expect(screen.getAllByText("Send now")).toHaveLength(1);
    expect(screen.getByText(/Charts are off on this server/)).toBeInTheDocument();
  });

  it("offers to send anyway when a conditional report is skipped", async () => {
    currentUser = { username: "eve", role: "editor" };
    sendReport.mockResolvedValueOnce({ status: "skipped", summary: { condition: "revenue is 900, not below 100" }, channels: [] });
    sendReport.mockResolvedValueOnce({ status: "sent", channels: [] });
    render(<ReportsPanel />);
    fireEvent.click(await screen.findByText("Send now"));
    expect(await screen.findByText(/not sent, revenue is 900/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Send anyway"));
    await waitFor(() => expect(sendReport).toHaveBeenLastCalledWith("r1", true));
  });
});

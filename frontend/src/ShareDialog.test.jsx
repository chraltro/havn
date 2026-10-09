import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const listDashboardShares = vi.fn();
const createDashboardShare = vi.fn();
const listReports = vi.fn();
const listUsers = vi.fn();
let currentUser = { username: "eve", role: "editor" };

vi.mock("./api", () => ({
  api: {
    listDashboardShares: (...a) => listDashboardShares(...a),
    createDashboardShare: (...a) => createDashboardShare(...a),
    listReports: (...a) => listReports(...a),
    listUsers: (...a) => listUsers(...a),
    revokeDashboardShare: vi.fn(),
    updateDashboardShare: vi.fn(),
  },
}));
vi.mock("./AuthContext", () => ({ useAuth: () => ({ currentUser }) }));

const { default: ShareDialog, embedSnippet } = await import("./ShareDialog");

beforeEach(() => {
  vi.clearAllMocks();
  listDashboardShares.mockResolvedValue([]);
  listReports.mockResolvedValue([]);
  listUsers.mockResolvedValue([{ username: "vic", role: "viewer" }]);
});

describe("embedSnippet", () => {
  it("escapes the URL and title and marks the frame as embedded", () => {
    const s = embedSnippet('https://h/p/x"><script>', 'A "B" <C>');
    expect(s).not.toContain("<script>");
    expect(s).toContain("embed=1");
    expect(s).toContain("&quot;B&quot;");
  });
});

describe("ShareDialog", () => {
  it("does not let an editor create a public link", async () => {
    currentUser = { username: "eve", role: "editor" };
    render(<ShareDialog dashboard={{ id: "d1", name: "Sales" }} onClose={() => {}} />);
    const pub = await screen.findByRole("radio", { name: /Public link/ });
    expect(pub).toBeDisabled();
  });

  it("creates a public link as a chosen role and shows it once with an embed snippet", async () => {
    currentUser = { username: "ada", role: "admin" };
    createDashboardShare.mockResolvedValue({
      id: "s1", mode: "public", view_as_role: "viewer", status: "active", view_count: 0,
      path: "/p/TOKEN123", url: "http://x/p/TOKEN123", token: "TOKEN123", expires_at: null, created_by: "ada",
    });
    render(<ShareDialog dashboard={{ id: "d1", name: "Sales" }} onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("radio", { name: /Public link/ }));
    fireEvent.click(screen.getByText("Create public link"));
    await waitFor(() => expect(createDashboardShare).toHaveBeenCalled());
    const [dashId, body] = createDashboardShare.mock.calls[0];
    expect(dashId).toBe("d1");
    expect(body).toMatchObject({ mode: "public", view_as_role: "viewer", expires_in_days: 30 });
    expect(await screen.findByText("Public link created")).toBeInTheDocument();
    expect(screen.getByDisplayValue("http://x/p/TOKEN123")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Embed in another site"));
    expect(screen.getByLabelText("iframe embed snippet").value).toContain("http://x/p/TOKEN123?embed=1");
  });
});

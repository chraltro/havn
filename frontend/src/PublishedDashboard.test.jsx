import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const getPublished = vi.fn();
const queryPublished = vi.fn();
const publishedFilterOptions = vi.fn();
const login = vi.fn();
const setToken = vi.fn();

vi.mock("./api", () => ({
  api: {
    getPublished: (...a) => getPublished(...a),
    queryPublished: (...a) => queryPublished(...a),
    publishedFilterOptions: (...a) => publishedFilterOptions(...a),
    login: (...a) => login(...a),
    setToken: (...a) => setToken(...a),
    runQuery: vi.fn(() => { throw new Error("published pages must not run ad-hoc SQL"); }),
    queryWidget: vi.fn(() => { throw new Error("published pages must not use editor endpoints"); }),
  },
}));

// Charts measure the DOM; the published page's logic is what is under test.
vi.mock("./ChartPanel", () => ({ default: () => <div data-testid="chart" /> }));

const { default: PublishedDashboard, stackOrder, stackGroups, relativeAge, publishedKeyFromPath } = await import("./PublishedDashboard");

const DEF = {
  dashboard: {
    id: "d1", name: "Paddle sales", description: "Orders", layout: {},
    filters: [{ id: "f1", label: "Region", type: "dropdown", column: "region", has_options: true }],
    settings: { parameters: [], pages: [] },
    widgets: [
      { id: "k1", widget_type: "kpi", title: "Revenue", config: {}, position: { x: 1, y: 1, w: 6, h: 2 }, has_query: true },
      { id: "t1", widget_type: "text", title: "Note", config: { content: "Gross numbers" }, position: { x: 7, y: 1, w: 6, h: 2 }, has_query: false },
    ],
  },
  share: { mode: "public", label: "", expires_at: null },
  viewer: { username: null },
  freshness: { as_of: "2026-10-09T06:00:00", newest: null, models: [], unknown: [] },
};

beforeEach(() => {
  vi.clearAllMocks();
  getPublished.mockResolvedValue(DEF);
  queryPublished.mockResolvedValue({ results: { k1: { columns: ["revenue"], rows: [[1234]], row_count: 1 } }, freshness: DEF.freshness });
  publishedFilterOptions.mockResolvedValue({ options: ["north", "south"] });
});

describe("helpers", () => {
  it("reads the key from /p/<key>", () => {
    expect(publishedKeyFromPath("/p/abc_DEF-1")).toBe("abc_DEF-1");
    expect(publishedKeyFromPath("/data/dashboards")).toBe(null);
  });

  it("stacks widgets in reading order and pairs KPIs", () => {
    const ws = [
      { id: "c", widget_type: "chart", position: { x: 1, y: 3 } },
      { id: "b", widget_type: "kpi", position: { x: 7, y: 1 } },
      { id: "a", widget_type: "kpi", position: { x: 1, y: 1 } },
    ];
    expect(stackOrder(ws).map(w => w.id)).toEqual(["a", "b", "c"]);
    const groups = stackGroups(ws);
    expect(groups[0].kpis.map(w => w.id)).toEqual(["a", "b"]);
    expect(groups[1].widget.id).toBe("c");
  });

  it("describes data age", () => {
    const now = Date.parse("2026-10-09T09:00:00");
    expect(relativeAge("2026-10-09T06:00:00", now)).toBe("3 h ago");
    expect(relativeAge(null, now)).toBe(null);
  });
});

describe("PublishedDashboard", () => {
  it("renders the saved widgets, freshness and no editing controls", async () => {
    render(<PublishedDashboard shareKey="tok" />);
    expect(await screen.findByText("Paddle sales")).toBeInTheDocument();
    expect(screen.getByText(/Data as of/)).toBeInTheDocument();
    expect(screen.getByText("Gross numbers")).toBeInTheDocument();
    await waitFor(() => expect(queryPublished).toHaveBeenCalled());
    expect(await screen.findByText("1.2K")).toBeInTheDocument();
    expect(screen.queryByText("Edit")).toBeNull();
    expect(screen.queryByText(/Save current view/)).toBeNull();
  });

  it("sends filter values, not SQL, and loads options from the saved query", async () => {
    render(<PublishedDashboard shareKey="tok" />);
    await screen.findByText("Paddle sales");
    await waitFor(() => expect(publishedFilterOptions).toHaveBeenCalledWith("tok", "f1"));
    const select = await screen.findByRole("combobox");
    await screen.findByRole("option", { name: "north" });
    fireEvent.change(select, { target: { value: "north" } });
    await waitFor(() => {
      const last = queryPublished.mock.calls.at(-1);
      expect(last[0]).toBe("tok");
      expect(last[1]).toEqual({ region: "north" });
    });
    for (const call of queryPublished.mock.calls) {
      expect(JSON.stringify(call)).not.toMatch(/select /i);
    }
  });

  it("asks a signed-in link's visitor to sign in", async () => {
    getPublished.mockRejectedValueOnce(new Error("Authentication required"));
    render(<PublishedDashboard shareKey="sid" />);
    expect(await screen.findByText("Sign in to view this dashboard")).toBeInTheDocument();
  });

  it("explains an expired or revoked link", async () => {
    getPublished.mockRejectedValueOnce(new Error("This link has expired"));
    render(<PublishedDashboard shareKey="old" />);
    expect(await screen.findByText("This dashboard isn't available")).toBeInTheDocument();
    expect(screen.getByText("This link has expired")).toBeInTheDocument();
  });
});

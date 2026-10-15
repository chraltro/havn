import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

const runQuery = vi.fn();
const explainQuery = vi.fn();
const formatSql = vi.fn();
const exportCsv = vi.fn();

vi.mock("./api", () => ({
  api: {
    runQuery: (...a) => runQuery(...a),
    explainQuery: (...a) => explainQuery(...a),
    formatSql: (...a) => formatSql(...a),
    exportCsv: (...a) => exportCsv(...a),
    listTables: () => Promise.resolve([]),
    listMaskingPolicies: () => Promise.resolve([]),
    describeTable: () => Promise.resolve({ columns: [] }),
  },
}));

// Both measure the DOM; the panel's own behaviour is what is under test.
vi.mock("./ChartPanel", () => ({ default: () => <div data-testid="chart" /> }));
vi.mock("./ExplainPanel", () => ({ default: () => <div data-testid="plan">plan</div> }));

// The panel keeps a module-level session (it outlives tab switches), so each
// test imports a fresh copy of the module.
async function freshPanel() {
  vi.resetModules();
  return (await import("./QueryPanel")).default;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const result = (rows, extra = {}) => ({
  columns: ["v"],
  column_types: ["VARCHAR"],
  rows: rows.map((r) => [r]),
  row_count: rows.length,
  truncated: false,
  ...extra,
});

// Render and let the mount-time fetches (tables, masking policies) settle.
async function mount(QueryPanel) {
  let view;
  await act(async () => { view = render(<QueryPanel addOutput={vi.fn()} />); });
  return view;
}

function editor() {
  return screen.getByLabelText("SQL query editor");
}

function type(text) {
  fireEvent.input(editor(), { target: { value: text } });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  formatSql.mockImplementation((sql) => Promise.resolve({ formatted: sql }));
});

describe("QueryPanel results", () => {
  it("replaces the previous rows with the error when a query fails", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    runQuery.mockResolvedValueOnce(result(["old-row-value"]));
    type("SELECT 'old-row-value' AS v");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    expect(await screen.findByText("old-row-value")).toBeInTheDocument();

    runQuery.mockRejectedValueOnce(new Error("Binder Error: column x not found"));
    type("SELECT x");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("column x not found");
    expect(screen.queryByText("old-row-value")).not.toBeInTheDocument();
  });

  it("keeps the result across a tab switch", async () => {
    const QueryPanel = await freshPanel();
    const first = await mount(QueryPanel);
    runQuery.mockResolvedValueOnce(result(["kept"]));
    type("SELECT 'kept' AS v");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    expect(await screen.findByText("kept")).toBeInTheDocument();
    first.unmount();

    await mount(QueryPanel);
    expect(screen.getByText("kept")).toBeInTheDocument();
  });

  it("picks up a run that finishes while the panel is unmounted", async () => {
    const QueryPanel = await freshPanel();
    const first = await mount(QueryPanel);
    const pending = deferred();
    runQuery.mockReturnValueOnce(pending.promise);
    type("SELECT 'late' AS v");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    first.unmount();

    await mount(QueryPanel);
    // Still running: the panel says so and offers Cancel, not a second Run.
    expect(screen.getByRole("status")).toHaveTextContent("Running query");
    expect(screen.getByRole("button", { name: "Cancel query" })).toBeInTheDocument();
    await act(async () => { pending.resolve(result(["late"])); });
    expect(await screen.findByText("late")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run query" })).toBeInTheDocument();
  });

  it("applies the automatic LIMIT when LIMIT only appears in a comment", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    runQuery.mockResolvedValue(result(["a"]));
    type("SELECT * FROM big -- TODO add a limit");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(1));
    expect(runQuery.mock.calls[0][2].limit).toBe(1000);

    await screen.findByText("a");
    type("SELECT * FROM big LIMIT 5");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));
    expect(runQuery.mock.calls[1][2].limit).toBeUndefined();
  });

  it("says why rows are missing and reruns without the limit on request", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    runQuery.mockResolvedValueOnce(result(["a", "b"], { truncated: true }));
    type("SELECT * FROM big");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    expect(await screen.findByText(/LIMIT 1000 is on/)).toBeInTheDocument();

    runQuery.mockResolvedValueOnce(result(["a", "b", "c"]));
    fireEvent.click(screen.getByRole("button", { name: "Run without limit" }));
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(2));
    expect(runQuery.mock.calls[1][2].limit).toBeUndefined();
    expect(await screen.findByText("c")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^LIMIT 1000/ })).toHaveAttribute("aria-pressed", "false");
  });

  it("runs from a parameter box on Enter", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    runQuery.mockResolvedValueOnce(result(["p"]));
    type("SELECT $day AS v");
    const box = screen.getByLabelText("Value for parameter day");
    fireEvent.change(box, { target: { value: "2024-01-01" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(runQuery).toHaveBeenCalledTimes(1));
    expect(runQuery.mock.calls[0][2].params).toEqual({ day: "2024-01-01" });
  });

  it("shows the results again when an Explain fails after a plan was open", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    runQuery.mockResolvedValueOnce(result(["row-under-plan"]));
    type("SELECT 1 AS v");
    fireEvent.click(screen.getByRole("button", { name: "Run query" }));
    await screen.findByText("row-under-plan");

    explainQuery.mockResolvedValueOnce({ plan: {}, raw: "" });
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByTestId("plan")).toBeInTheDocument();
    expect(screen.queryByText("row-under-plan")).not.toBeInTheDocument();

    explainQuery.mockRejectedValueOnce(new Error("Parser Error"));
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Parser Error");
    expect(screen.getByText("row-under-plan")).toBeInTheDocument();
  });
});

describe("QueryPanel editor actions", () => {
  it("drops a Format result when the text changed during the round trip", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    const pending = deferred();
    formatSql.mockReturnValueOnce(pending.promise);
    type("select 1");
    fireEvent.click(screen.getByRole("button", { name: /^Format/ }));
    type("select 2");
    await act(async () => { pending.resolve({ formatted: "SELECT\n  1" }); });
    expect(editor().value).toBe("select 2");
  });

  it("lists every history entry and gives focus back to History on Escape", async () => {
    const entries = Array.from({ length: 30 }, (_, i) => ({ sql: `SELECT ${i}`, ts: new Date().toISOString() }));
    localStorage.setItem("havn_query_history", JSON.stringify(entries));
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    const historyBtn = screen.getByRole("button", { name: "Query history" });
    fireEvent.click(historyBtn);
    expect(screen.getByTitle("SELECT 29")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTitle("SELECT 29")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(historyBtn);
  });

  it("asks for a second click before clearing the history", async () => {
    localStorage.setItem("havn_query_history", JSON.stringify([{ sql: "SELECT 1", ts: new Date().toISOString() }]));
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    fireEvent.click(screen.getByRole("button", { name: "Query history" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByTitle("SELECT 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear all 1?" }));
    expect(screen.queryByTitle("SELECT 1")).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("havn_query_history"))).toEqual([]);
  });

  it("shows a failed CSV export inline, not only in the output panel", async () => {
    const QueryPanel = await freshPanel();
    await mount(QueryPanel);
    exportCsv.mockRejectedValueOnce(new Error("Error (400): Catalog Error: no table big"));
    type("SELECT * FROM big");
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Export failed: Error (400): Catalog Error");
  });
});

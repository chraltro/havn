import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const getModelWorkbench = vi.fn();

vi.mock("./api", () => ({
  api: { getModelWorkbench: (...args) => getModelWorkbench(...args) },
}));

const { default: ModelWorkbench, findAssertionLine, insertColDoc, buildStatus } = await import("./ModelWorkbench");

const SQL = [
  "@config materialized=table, schema=silver",
  "@assert no_nulls(customer_id), severity=warn",
  "@assert order_id > 0",
  "",
  "SELECT order_id, customer_id FROM bronze.orders",
].join("\n");

const DATA = {
  model: "silver.enriched",
  path: "transform/silver/enriched.sql",
  upstream: [{ name: "bronze.orders", path: "transform/bronze/orders.sql" }],
  downstream: [{ name: "gold.report", path: "transform/gold/report.sql" }],
  downstream_all: ["gold.report", "gold.report2"],
  columns: [
    { name: "order_id", type: "INTEGER", description: "Primary key" },
    { name: "customer_id", type: "INTEGER", description: "" },
  ],
  checks: [
    {
      expression: "no_nulls(customer_id)", severity: "warn", passed: false,
      detail: "2 nulls out of 4 rows (50.0%)", checked_at: null,
      failing_sql: 'SELECT * FROM silver.enriched WHERE "customer_id" IS NULL',
    },
    {
      expression: "order_id > 0", severity: "error", passed: true,
      detail: "holds for all rows", checked_at: null, failing_sql: "x",
    },
  ],
  runs: [{ status: "success", started_at: null, duration_ms: 120, rows_affected: 4, error: null }],
  state: { built: true, last_run_at: null, row_count: 4, run_duration_ms: 120, changed_since_build: false },
};

function fakeEditor() {
  const collection = { set: vi.fn(), clear: vi.fn() };
  return {
    collection,
    createDecorationsCollection: vi.fn(() => collection),
    revealLineInCenter: vi.fn(),
    setPosition: vi.fn(),
    focus: vi.fn(),
  };
}

function renderWorkbench(props = {}) {
  const handlers = {
    onPreview: vi.fn(), onPreviewSql: vi.fn(), onClearPreview: vi.fn(), onSave: vi.fn(),
    onBuild: vi.fn(), onBuildDownstream: vi.fn(), onOpenFile: vi.fn(), onOpenDag: vi.fn(),
    onEditContent: vi.fn(),
  };
  const utils = render(
    <ModelWorkbench
      activeFile="transform/silver/enriched.sql"
      content={SQL}
      dirty={false}
      running={false}
      editor={null}
      preview={null}
      previewError={null}
      previewRunning={false}
      previewLabel={null}
      {...handlers}
      {...props}
    >
      <div data-testid="editor" />
    </ModelWorkbench>,
  );
  return { ...utils, handlers };
}

describe("workbench helpers", () => {
  it("finds the directive line for an assertion, ignoring a severity suffix", () => {
    expect(findAssertionLine(SQL, "no_nulls(customer_id)")).toBe(2);
    expect(findAssertionLine(SQL, "order_id > 0")).toBe(3);
    expect(findAssertionLine(SQL, "order_id > 1")).toBe(0);
    expect(findAssertionLine("-- assert: x > 0\nSELECT 1", "x > 0")).toBe(1);
    expect(findAssertionLine("@grain id\nSELECT 1", "grain(id)")).toBe(1);
  });

  it("inserts @col after the directive block", () => {
    const { text, line } = insertColDoc(SQL, "customer_id");
    expect(line).toBe(4);
    expect(text.split("\n")[3]).toBe("@col customer_id: ");
    expect(text.split("\n")[5]).toMatch(/^SELECT/);
  });

  it("describes the build state", () => {
    expect(buildStatus(DATA, true).text).toBe("Unsaved changes · 2 downstream models depend on this");
    expect(buildStatus({ ...DATA, state: { built: false } }, false).text).toMatch(/^Not built yet/);
    expect(buildStatus({ ...DATA, state: { ...DATA.state, changed_since_build: true } }, false).tone).toBe("warn");
    expect(buildStatus(DATA, false)).toMatchObject({ tone: "warn" });
    expect(buildStatus(DATA, false).text).toMatch(/4 rows · 1 check failing$/);
    const passing = { ...DATA, checks: [DATA.checks[1]] };
    expect(buildStatus(passing, false).text).toMatch(/^Up to date .* 4 rows$/);
  });
});

describe("ModelWorkbench", () => {
  beforeEach(() => {
    getModelWorkbench.mockReset();
    getModelWorkbench.mockResolvedValue(DATA);
  });

  it("shows lineage and opens neighbours", async () => {
    const { handlers } = renderWorkbench();
    await screen.findByText("silver.enriched");
    expect(getModelWorkbench).toHaveBeenCalledWith("transform/silver/enriched.sql");
    fireEvent.click(screen.getByText("bronze.orders"));
    expect(handlers.onOpenFile).toHaveBeenCalledWith("transform/bronze/orders.sql");
    expect(screen.getByText("+1 further")).toBeTruthy();
  });

  it("builds downstream with the model+ selector", async () => {
    const { handlers } = renderWorkbench();
    const btn = await screen.findByText("Build + downstream (2)");
    fireEvent.click(btn);
    expect(handlers.onBuildDownstream).toHaveBeenCalledWith("silver.enriched+");
    fireEvent.click(screen.getByText("Build model"));
    expect(handlers.onBuild).toHaveBeenCalledWith("silver.enriched");
  });

  it("lists checks and previews failing rows", async () => {
    const { handlers } = renderWorkbench();
    await screen.findByText("silver.enriched");
    fireEvent.click(screen.getByRole("tab", { name: /Checks/ }));
    expect(screen.getByText("2 nulls out of 4 rows (50.0%)")).toBeTruthy();
    const rowsButtons = screen.getAllByText("Show rows");
    expect(rowsButtons).toHaveLength(1); // only the failing check
    fireEvent.click(rowsButtons[0]);
    expect(handlers.onPreviewSql).toHaveBeenCalledWith(
      DATA.checks[0].failing_sql,
      "Failing rows · no_nulls(customer_id)",
    );
  });

  it("offers to document undocumented columns", async () => {
    const { handlers } = renderWorkbench();
    await screen.findByText("silver.enriched");
    fireEvent.click(screen.getByRole("tab", { name: /Columns/ }));
    expect(screen.getByText("Primary key")).toBeTruthy();
    fireEvent.click(screen.getByText("+ add @col"));
    expect(handlers.onEditContent).toHaveBeenCalledWith(insertColDoc(SQL, "customer_id").text);
  });

  it("marks failed assertions in the editor", async () => {
    const editor = fakeEditor();
    renderWorkbench({ editor });
    await waitFor(() => {
      const last = editor.collection.set.mock.calls.at(-1)?.[0];
      expect(last).toHaveLength(2);
      expect(last[0].range.startLineNumber).toBe(2);
      expect(last[0].options.className).toBe("havn-assert-warn-line");
      expect(last[1].options.after.content).toContain("2 nulls out of 4 rows");
    });
  });

  it("reloads after a run finishes", async () => {
    const { rerender } = renderWorkbench({ running: true });
    await screen.findByText("silver.enriched");
    const calls = getModelWorkbench.mock.calls.length;
    rerender(
      <ModelWorkbench activeFile="transform/silver/enriched.sql" content={SQL} dirty={false} running={false}
        editor={null} preview={null} previewError={null} previewRunning={false} previewLabel={null}
        onPreview={vi.fn()} onPreviewSql={vi.fn()} onClearPreview={vi.fn()} onSave={vi.fn()} onBuild={vi.fn()}
        onBuildDownstream={vi.fn()} onOpenFile={vi.fn()} onOpenDag={vi.fn()} onEditContent={vi.fn()}>
        <div />
      </ModelWorkbench>,
    );
    await waitFor(() => expect(getModelWorkbench.mock.calls.length).toBe(calls + 1));
  });

  it("ignores a slow response for a file it already left", async () => {
    let resolveOld;
    getModelWorkbench.mockReset();
    getModelWorkbench
      .mockImplementationOnce(() => new Promise((r) => { resolveOld = r; }))
      .mockResolvedValueOnce({ ...DATA, model: "gold.report", upstream: [], downstream: [], downstream_all: [] });
    const props = {
      content: SQL, dirty: false, running: false, editor: null, preview: null, previewError: null,
      previewRunning: false, previewLabel: null, onPreview: vi.fn(), onPreviewSql: vi.fn(),
      onClearPreview: vi.fn(), onSave: vi.fn(), onBuild: vi.fn(), onBuildDownstream: vi.fn(),
      onOpenFile: vi.fn(), onOpenDag: vi.fn(), onEditContent: vi.fn(),
    };
    const { rerender } = render(<ModelWorkbench activeFile="transform/silver/enriched.sql" {...props}><div /></ModelWorkbench>);
    rerender(<ModelWorkbench activeFile="transform/gold/report.sql" {...props}><div /></ModelWorkbench>);
    await screen.findByText("gold.report");
    resolveOld(DATA);
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText("silver.enriched")).toBeNull();
    expect(screen.getByText("gold.report")).toBeTruthy();
  });

  it("still offers preview when the file is not a model", async () => {
    getModelWorkbench.mockRejectedValue(new Error("No model is defined in 'transform/x.sql'"));
    const { handlers } = renderWorkbench();
    await waitFor(() => expect(getModelWorkbench).toHaveBeenCalled());
    const previewButtons = screen.getAllByRole("button", { name: /^Preview/ });
    fireEvent.click(previewButtons.at(-1));
    expect(handlers.onPreview).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: /Checks/ }));
    expect(await screen.findByText(/No model is defined/)).toBeTruthy();
  });
});

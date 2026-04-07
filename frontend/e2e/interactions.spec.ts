/**
 * Interaction tests for critical UI paths.
 *
 * These test actual user workflows, not just page loads:
 * - Query execution and result rendering
 * - CSV export
 * - Pipeline run via UI
 * - File editing and saving
 * - Table browsing and profiling
 *
 * Prerequisites:
 *   cd test-project && havn jobs run full-refresh --force && havn serve
 *
 * Run from frontend/:
 *   npx playwright test e2e/interactions.spec.ts
 */
import { test, expect, type Page } from "@playwright/test";

/* ------------------------------------------------------------------ */
/* helpers (same as smoke.spec.ts)                                     */
/* ------------------------------------------------------------------ */

const SECTION_MAP: Record<string, { section: string; subTab?: string }> = {
  Overview: { section: "Overview" },
  Editor: { section: "Develop", subTab: "Editor" },
  Notebooks: { section: "Develop", subTab: "Notebooks" },
  DAG: { section: "Develop", subTab: "DAG" },
  Query: { section: "Explore", subTab: "Query" },
  Tables: { section: "Explore", subTab: "Tables" },
  "Data Sources": { section: "Explore", subTab: "Data Sources" },
  Quality: { section: "Observe", subTab: "Quality" },
  History: { section: "Observe", subTab: "History" },
  Masking: { section: "Configure", subTab: "Masking" },
};

async function goTab(page: Page, name: string) {
  const mapping = SECTION_MAP[name];
  if (!mapping) throw new Error(`Unknown tab: ${name}`);
  const sectionNav = page.locator(`[data-havn-guide="tabs"]`);
  await sectionNav.getByText(mapping.section, { exact: true }).click();
  await page.waitForTimeout(300);
  if (mapping.subTab) {
    await page
      .locator(`[data-havn-tab]`)
      .getByText(mapping.subTab, { exact: true })
      .click();
    await page.waitForTimeout(300);
  }
}

async function dismissGuide(page: Page) {
  const skipBtn = page.getByText("Skip", { exact: true });
  if (await skipBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
    await skipBtn.click();
    await page.waitForTimeout(300);
  }
}

/* ------------------------------------------------------------------ */
/* tests                                                               */
/* ------------------------------------------------------------------ */

test.describe("Query execution", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator(`[data-havn-guide="tabs"]`)
    ).toBeVisible({ timeout: 15000 });
    await dismissGuide(page);
    await goTab(page, "Query");
  });

  test("execute a SELECT and see results", async ({ page }) => {
    // Type SQL into the Monaco editor
    const editor = page.locator(".monaco-editor").first();
    await editor.click();
    await page.keyboard.press("Control+a");
    await page.keyboard.type(
      "SELECT 42 AS answer, 'hello' AS greeting"
    );

    // Click Run
    await page.getByRole("button", { name: /Run/i }).first().click();

    // Wait for results
    await expect(
      page.getByText("42", { exact: false })
    ).toBeVisible({ timeout: 10000 });
    await expect(
      page.getByText("hello", { exact: false })
    ).toBeVisible();
  });

  test("execute a query against pipeline data", async ({ page }) => {
    const editor = page.locator(".monaco-editor").first();
    await editor.click();
    await page.keyboard.press("Control+a");
    await page.keyboard.type(
      "SELECT COUNT(*) AS cnt FROM gold.earthquake_summary"
    );
    await page.getByRole("button", { name: /Run/i }).first().click();

    // Should get a numeric result
    await expect(
      page.locator("td").first()
    ).toBeVisible({ timeout: 10000 });
  });

  test("CSV export produces a download", async ({ page }) => {
    const editor = page.locator(".monaco-editor").first();
    await editor.click();
    await page.keyboard.press("Control+a");
    await page.keyboard.type("SELECT 1 AS x, 2 AS y");
    await page.getByRole("button", { name: /Run/i }).first().click();

    // Wait for results to appear
    await expect(
      page.getByText("1", { exact: false })
    ).toBeVisible({ timeout: 10000 });

    // Click CSV/export button
    const [download] = await Promise.all([
      page.waitForEvent("download", { timeout: 10000 }),
      page.getByRole("button", { name: /CSV|Export|Download/i }).first().click(),
    ]);

    // Verify download started
    expect(download.suggestedFilename()).toMatch(/\.csv$/);
  });

  test("invalid SQL shows error", async ({ page }) => {
    const editor = page.locator(".monaco-editor").first();
    await editor.click();
    await page.keyboard.press("Control+a");
    await page.keyboard.type("SELECTT BROKEN SQL");
    await page.getByRole("button", { name: /Run/i }).first().click();

    // Should show error, not crash
    await expect(
      page.getByText(/error|failed|invalid/i)
    ).toBeVisible({ timeout: 10000 });
  });
});

test.describe("Table browsing", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator(`[data-havn-guide="tabs"]`)
    ).toBeVisible({ timeout: 15000 });
    await dismissGuide(page);
    await goTab(page, "Tables");
  });

  test("tables list shows pipeline schemas", async ({ page }) => {
    // Should see schemas from the earthquake pipeline
    await expect(
      page.getByText("gold", { exact: false })
    ).toBeVisible({ timeout: 10000 });
    await expect(
      page.getByText("bronze", { exact: false })
    ).toBeVisible();
  });

  test("clicking a table shows sample data", async ({ page }) => {
    // Click on a gold table
    await page.getByText("earthquake_summary", { exact: false }).first().click();
    await page.waitForTimeout(1000);

    // Should see column headers and data rows
    await expect(
      page.locator("table, [role='grid'], [data-havn-table]").first()
    ).toBeVisible({ timeout: 10000 });
  });
});

test.describe("Editor file operations", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator(`[data-havn-guide="tabs"]`)
    ).toBeVisible({ timeout: 15000 });
    await dismissGuide(page);
    await goTab(page, "Editor");
  });

  test("open a SQL file and see content in Monaco", async ({ page }) => {
    const sidebar = page.locator(`[data-havn-guide="sidebar"]`);
    await sidebar.getByText("earthquakes.sql").first().click({ timeout: 10000 });
    await page.waitForTimeout(1000);

    // Monaco editor should be visible with SQL content
    const editor = page.locator(".monaco-editor");
    await expect(editor).toBeVisible({ timeout: 10000 });
    // Should contain SQL keywords
    await expect(
      page.locator(".monaco-editor").getByText("SELECT", { exact: false })
    ).toBeVisible({ timeout: 5000 });
  });

  test("file tree shows project structure", async ({ page }) => {
    const sidebar = page.locator(`[data-havn-guide="sidebar"]`);
    await expect(sidebar.getByText("transform")).toBeVisible({ timeout: 5000 });
    await expect(sidebar.getByText("ingest")).toBeVisible();
    await expect(sidebar.getByText("export")).toBeVisible();
  });
});

test.describe("DAG visualization", () => {
  test("DAG canvas renders with model nodes", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator(`[data-havn-guide="tabs"]`)
    ).toBeVisible({ timeout: 15000 });
    await dismissGuide(page);
    await goTab(page, "DAG");

    // Canvas or SVG element should be visible
    await expect(
      page.locator("canvas, svg, [data-havn-dag]").first()
    ).toBeVisible({ timeout: 10000 });
  });
});

test.describe("History", () => {
  test("history tab shows pipeline runs", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.locator(`[data-havn-guide="tabs"]`)
    ).toBeVisible({ timeout: 15000 });
    await dismissGuide(page);
    await goTab(page, "History");

    // Should show run entries
    await expect(
      page.getByText(/transform|ingest|seed/i)
    ).toBeVisible({ timeout: 10000 });
  });
});

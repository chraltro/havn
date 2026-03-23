/**
 * Playwright dashboard test — creates dashboard via API, then tests UI.
 */
import { chromium } from "playwright";
import { writeFileSync, mkdirSync, rmSync } from "fs";

const BASE = "http://localhost:3000";
const SHOTS = "./test_screenshots";
const issues = [];

function log(msg) { console.log(`[TEST] ${msg}`); }
function issue(msg) { issues.push(msg); console.log(`  !! ${msg}`); }
async function shot(page, name) {
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: false });
}

(async () => {
  rmSync(SHOTS, { recursive: true, force: true });
  mkdirSync(SHOTS, { recursive: true });

  // ── Create test dashboard via API ──
  log("Creating dashboard via API...");
  const res = await fetch(`${BASE}/api/dashboards`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "Playwright Chart Test" }),
  });
  const dash = await res.json();
  log(`  Dashboard: ${dash.id}`);

  // Add a widget with a query that returns varied data types
  const wRes = await fetch(`${BASE}/api/dashboards/${dash.id}/widgets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      widget_type: "chart",
      chart_type: "bar",
      title: "Test Chart",
      sql_query: "SELECT * FROM landing.earthquakes LIMIT 100",
      config: {},
      position: { x: 1, y: 1, w: 18, h: 6 },
    }),
  });
  const widget = await wRes.json();
  log(`  Widget: ${widget.id}`);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  // Skip onboarding via localStorage
  await page.goto(BASE);
  await page.evaluate(() => {
    localStorage.setItem("dp_guide_completed", "true");
    localStorage.setItem("dp_dismissed_hints", "[]");
  });

  // Navigate to dashboard by clicking through UI
  await page.goto(BASE);
  await page.waitForTimeout(2000);

  // Dismiss any remaining overlays
  for (let i = 0; i < 3; i++) {
    for (const t of ["Skip tour", "Skip", "Got it"]) {
      try {
        const b = page.locator(`button:has-text("${t}")`).first();
        if (await b.isVisible({ timeout: 300 })) await b.click({ force: true });
      } catch {}
    }
    await page.waitForTimeout(200);
  }

  // Navigate: click all buttons with these exact texts
  log("Navigating to dashboard...");
  // First click Explore in the top section nav
  await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll('button'));
    const explore = buttons.find(b => b.textContent.trim() === 'Explore' && b.hasAttribute('data-havn-tab'));
    if (explore) explore.click();
  });
  await page.waitForTimeout(600);
  // Then click Dashboards in the sub-tab bar
  await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll('button'));
    const dash = buttons.find(b => b.textContent.trim() === 'Dashboards' && b.hasAttribute('data-havn-tab'));
    if (dash) { dash.click(); console.log('clicked dashboards tab'); }
    else console.log('dashboards tab NOT found, buttons:', buttons.map(b => b.textContent.trim()).join(', '));
  });
  await page.waitForTimeout(1500);
  await shot(page, "01_dashboard_list");

  // Click our test dashboard
  const dashCard = page.locator("text=Playwright Chart Test").first();
  if (await dashCard.isVisible({ timeout: 3000 }).catch(() => false)) {
    await dashCard.click({ force: true });
    await page.waitForTimeout(2000);
    log("  Opened dashboard");
  } else {
    issue("Could not find test dashboard in list");
    await shot(page, "01_debug_list");
  }
  await shot(page, "02_dashboard_view");

  // Enter edit mode
  const editBtn = page.locator("button:has-text('Edit')").first();
  if (await editBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
    await editBtn.click({ force: true });
    await page.waitForTimeout(500);
  }

  // Click the widget's edit menu
  log("Opening widget editor...");
  const menuBtn = page.locator("button:has-text('⋮')").first();
  if (await menuBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
    await menuBtn.click({ force: true });
    await page.waitForTimeout(300);
    const editItem = page.locator("button:has-text('Edit')").nth(1); // Second "Edit" is the menu item
    if (await editItem.isVisible({ timeout: 1000 }).catch(() => false)) {
      await editItem.click({ force: true });
      await page.waitForTimeout(1500);
    }
  }
  await shot(page, "03_editor_open");

  // ── Run the preview query first (we're in SQL mode) ──
  log("Running preview query...");
  const runBtn = page.locator("button:has-text('Run Preview')").first();
  if (await runBtn.isVisible({ timeout: 2000 }).catch(() => false)) {
    await runBtn.click({ force: true });
    await page.waitForTimeout(3000); // Wait for query + render
    log("  Preview query executed");
  } else {
    // Try Ctrl+Enter on the textarea
    const textarea = page.locator("textarea").first();
    if (await textarea.isVisible({ timeout: 1000 }).catch(() => false)) {
      await textarea.click();
      await page.keyboard.press("Control+Enter");
      await page.waitForTimeout(3000);
      log("  Preview triggered via Ctrl+Enter");
    }
  }
  await shot(page, "03b_preview_rendered");

  // ── Test every chart type ──
  log("\n====== CHART TYPE TESTING ======");
  const chartTypes = [
    "Bar", "Line", "Area", "Scatter", "Pie", "Donut", "H-Bar", "Stacked",
    "Treemap", "Heatmap", "Funnel", "Waterfall", "Histogram", "Radar", "Bubble",
    "Gauge", "Progress", "Sparkline", "Bullet", "Sankey",
  ];

  for (const ct of chartTypes) {
    const btn = page.locator(`button:has-text("${ct}")`).first();
    if (await btn.isVisible({ timeout: 800 }).catch(() => false)) {
      await btn.click({ force: true });
      await page.waitForTimeout(1500);

      // Take screenshot of the PREVIEW area only (right side)
      const previewEl = page.locator('[style*="flex: 1"][style*="flex-direction: column"]').last();
      try {
        await previewEl.screenshot({ path: `${SHOTS}/chart_${ct.toLowerCase().replace("-","")}.png` });
      } catch {
        await shot(page, `chart_${ct.toLowerCase().replace("-","")}`);
      }

      // Check for NaN
      const pageContent = await page.content();
      const previewSection = pageContent.slice(pageContent.indexOf("Preview"));
      if (previewSection.includes(">NaN<")) issue(`${ct}: shows NaN in preview`);

      // Check for focus outline (the CSS bug)
      const outline = await btn.evaluate(el => window.getComputedStyle(el).outlineStyle);
      if (outline !== "none" && outline !== "") issue(`${ct}: button has focus outline (${outline})`);

      log(`  ${ct}: OK`);
    } else {
      issue(`${ct}: button not found`);
    }
  }

  // ── Check color palette ──
  log("\n====== COLOR & VISUAL CHECKS ======");
  await page.locator("button:has-text('Pie')").first().click({ force: true });
  await page.waitForTimeout(1500);

  const colorInfo = await page.evaluate(() => {
    const fills = new Set();
    document.querySelectorAll("svg path[fill], svg rect[fill]").forEach(el => {
      const f = el.getAttribute("fill");
      if (f && f !== "none" && f !== "transparent" && !f.startsWith("var(") && !f.startsWith("url(")) fills.add(f);
    });
    return { count: fills.size, colors: [...fills].slice(0, 15) };
  });
  log(`  Pie colors: ${colorInfo.count} distinct (${colorInfo.colors.slice(0,5).join(", ")}...)`);
  if (colorInfo.count > 15) issue(`Pie uses ${colorInfo.count} hardcoded colors — palette recycling issue`);

  // Check x-axis overlap on stacked chart
  await page.locator("button:has-text('Stacked')").first().click({ force: true });
  await page.waitForTimeout(1500);
  await shot(page, "check_stacked_xaxis");

  const overlapCount = await page.evaluate(() => {
    const texts = [];
    document.querySelectorAll("svg text").forEach(t => {
      const r = t.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) texts.push({ x: r.x, w: r.width, y: r.y, t: t.textContent });
    });
    // Check bottom row (x-axis): texts at similar y position
    const yGroups = {};
    texts.forEach(t => {
      const key = Math.round(t.y / 10) * 10;
      if (!yGroups[key]) yGroups[key] = [];
      yGroups[key].push(t);
    });
    let overlaps = 0;
    Object.values(yGroups).forEach(group => {
      group.sort((a, b) => a.x - b.x);
      for (let i = 1; i < group.length; i++) {
        if (group[i].x < group[i-1].x + group[i-1].w - 2) overlaps++;
      }
    });
    return overlaps;
  });
  if (overlapCount > 0) issue(`Stacked chart has ${overlapCount} overlapping axis labels`);
  else log("  Stacked chart: no label overlaps");

  // ── Progress chart: check max value logic ──
  await page.locator("button:has-text('Progress')").first().click({ force: true });
  await page.waitForTimeout(1500);
  await shot(page, "check_progress");
  const progressHTML = await page.evaluate(() => {
    const svgs = document.querySelectorAll("svg");
    for (const svg of svgs) {
      const rects = svg.querySelectorAll("rect");
      if (rects.length >= 2) {
        const bg = rects[0].getAttribute("width");
        const fill = rects[1].getAttribute("width");
        return { bgWidth: bg, fillWidth: fill, text: svg.textContent };
      }
    }
    return null;
  });
  if (progressHTML) {
    log(`  Progress: bg=${progressHTML.bgWidth}, fill=${progressHTML.fillWidth}`);
    if (parseFloat(progressHTML.fillWidth) < 1) issue("Progress bar fill is essentially invisible (< 1px)");
  }

  // ── Gauge: check value display ──
  await page.locator("button:has-text('Gauge')").first().click({ force: true });
  await page.waitForTimeout(1500);
  await shot(page, "check_gauge");

  // ── SUMMARY ──
  log("\n" + "=".repeat(50));
  log(`TOTAL ISSUES: ${issues.length}`);
  log("=".repeat(50));
  issues.forEach((iss, i) => log(`  ${i + 1}. ${iss}`));

  writeFileSync(`${SHOTS}/issues.txt`,
    `Dashboard UI Test — ${new Date().toISOString()}\n${"=".repeat(50)}\n\nIssues: ${issues.length}\n\n${issues.map((s,i) => `${i+1}. ${s}`).join("\n")}\n`
  );

  // Cleanup test dashboard
  await fetch(`${BASE}/api/dashboards/${dash.id}`, { method: "DELETE" });

  await browser.close();
  log("\nScreenshots in " + SHOTS);
})();

import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { SECTIONS, tabToPath, pathToTab } from "./navigation";
import NavRail from "./NavRail";
import { isProductionEnv } from "./EnvironmentSwitcher";

describe("navigation routes", () => {
  it("maps every tab to a URL and back", () => {
    for (const section of SECTIONS) {
      const tabs = section.tabs.length ? section.tabs : [section.id];
      for (const tab of tabs) expect(pathToTab(tabToPath(tab))).toBe(tab);
    }
  });

  it("puts Home at / and a section's default tab at the section path", () => {
    expect(tabToPath("Overview")).toBe("/");
    expect(tabToPath("Ship")).toBe("/ship");
    expect(tabToPath("Editor")).toBe("/build");
    expect(tabToPath("Data Sources")).toBe("/data/data-sources");
    expect(tabToPath("Settings")).toBe("/settings");
  });

  it("keeps the pre-rail URLs working", () => {
    expect(pathToTab("/develop")).toBe("Editor");
    expect(pathToTab("/develop/git")).toBe("Git");
    // Data Sources moved from Develop to Data; its old URL still finds it.
    expect(pathToTab("/develop/data-sources")).toBe("Data Sources");
    expect(pathToTab("/explore/dag")).toBe("DAG");
    expect(pathToTab("/configure/masking")).toBe("Masking");
    expect(pathToTab("/nowhere")).toBe("Overview");
  });
});

describe("NavRail", () => {
  it("marks the active destination and shows the attention badge", () => {
    const onNavigate = vi.fn();
    render(<NavRail sections={SECTIONS} activeSection="Observe" onNavigate={onNavigate}
                    agentOpen={false} onToggleAgent={vi.fn()} badges={{ Observe: 3 }} />);
    const observe = screen.getByRole("button", { name: /Observe/ });
    expect(observe.getAttribute("aria-current")).toBe("page");
    expect(screen.getByLabelText("3 need attention")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Ship/ }));
    expect(onNavigate).toHaveBeenCalledWith("Ship");
  });

  it("keeps settings with the secondary items", () => {
    render(<NavRail sections={SECTIONS} activeSection="Overview" onNavigate={vi.fn()}
                    agentOpen onToggleAgent={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Settings/ }).dataset.secondary).toBe("true");
    expect(screen.getByRole("button", { name: /Agent/ }).getAttribute("aria-pressed")).toBe("true");
  });
});

describe("isProductionEnv", () => {
  it("recognises production names only", () => {
    for (const n of ["prod", "PROD", "production", "prd", "live", "prod-eu"]) expect(isProductionEnv(n)).toBe(true);
    for (const n of ["dev", "staging", "preprod", "product", "", null]) expect(isProductionEnv(n)).toBe(false);
  });
});

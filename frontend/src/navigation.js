/*
 * Section-based navigation: the destinations in the nav rail, their sub-tabs,
 * and the URL for each tab. Kept apart from App.jsx so it can be tested
 * without mounting the app.
 */

// Destinations in the nav rail. `slug` is the URL segment; `aliases` keep the
// pre-rail URLs (/develop, /explore, /configure) working. A section with no
// tabs is a single page whose tab id is the section id.
export const SECTIONS = [
  { id: "Overview", label: "Home", slug: "", icon: "home", tabs: [] },
  { id: "Build", label: "Build", slug: "build", aliases: ["develop"], icon: "code", tabs: ["Editor", "Orchestration", "Git"] },
  { id: "Data", label: "Data", slug: "data", aliases: ["explore"], icon: "data", tabs: ["Query", "Tables", "DAG", "Dashboards", "Data Sources"] },
  { id: "Observe", label: "Observe", slug: "observe", icon: "pulse", tabs: ["Quality", "Unit Tests", "Sentinel", "Diff", "Runs"] },
  { id: "Ship", label: "Ship", slug: "ship", icon: "ship", tabs: [] },
  { id: "Configure", label: "Settings", slug: "settings", aliases: ["configure"], icon: "gear", secondary: true, tabs: ["Settings", "Masking", "Wiki", "Docs"] },
];

// Quick lookup: tab name -> section id
export const TAB_TO_SECTION = {};
for (const s of SECTIONS) {
  if (s.tabs.length === 0) TAB_TO_SECTION[s.id] = s.id;
  for (const t of s.tabs) TAB_TO_SECTION[t] = s.id;
}

// Default tab for each section (first sub-tab or the section itself)
export const SECTION_DEFAULT = {};
for (const s of SECTIONS) {
  SECTION_DEFAULT[s.id] = s.tabs.length > 0 ? s.tabs[0] : s.id;
}

const tabSlug = (tab) => tab.toLowerCase().replace(/\s+/g, "-");

// URL routing helpers
export function tabToPath(tab) {
  const section = SECTIONS.find((s) => s.id === TAB_TO_SECTION[tab]);
  if (!section || !section.slug) return "/";
  // A section's default tab lives at the section's own path.
  if (SECTION_DEFAULT[section.id] === tab) return `/${section.slug}`;
  return `/${section.slug}/${tabSlug(tab)}`;
}

export function pathToTab(pathname) {
  const parts = pathname.replace(/^\/+|\/+$/g, "").toLowerCase().split("/").filter(Boolean);
  if (parts.length === 0) return "Overview";
  const section = SECTIONS.find((s) => s.slug === parts[0] || (s.aliases || []).includes(parts[0]));
  if (!section) return "Overview";
  if (parts.length === 1) return SECTION_DEFAULT[section.id];
  // A tab that moved sections (e.g. /develop/data-sources) is still found.
  const tab = section.tabs.find((t) => tabSlug(t) === parts[1])
    || SECTIONS.flatMap((s) => s.tabs).find((t) => tabSlug(t) === parts[1]);
  return tab || SECTION_DEFAULT[section.id];
}

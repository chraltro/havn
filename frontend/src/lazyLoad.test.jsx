import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React, { Suspense } from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

const listWikiPages = vi.fn();
const getWikiPage = vi.fn();

vi.mock("./api", () => ({
  api: {
    listWikiPages: (...args) => listWikiPages(...args),
    getWikiPage: (...args) => getWikiPage(...args),
  },
}));

const { lazyWithPreload, preloadAll, isChunkLoadError, PanelFallback } = await import("./lazyLoad");
const { default: ErrorBoundary, reloadOnceForChunkError } = await import("./ErrorBoundary");

// What Chromium throws when a hashed chunk is gone after a deploy.
function chunkError() {
  return new TypeError("Failed to fetch dynamically imported module: http://localhost/assets/QueryPanel-abc123.js");
}

beforeEach(() => {
  localStorage.clear();
  listWikiPages.mockReset();
  getWikiPage.mockReset();
  listWikiPages.mockResolvedValue([{ slug: "index", title: "Welcome", category: "Getting Started" }]);
  getWikiPage.mockImplementation(async (slug) => ({ slug, content: `# ${slug}` }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("a lazily loaded panel", () => {
  it("shows the loading state, then renders the real panel", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    const Wiki = lazyWithPreload(() => gate.then(() => import("./WikiPanel")));

    render(
      <ErrorBoundary name="Wiki">
        <Suspense fallback={<PanelFallback delay={0} />}>
          <Wiki />
        </Suspense>
      </ErrorBoundary>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    await act(async () => { release(); });
    expect(await screen.findByRole("button", { name: "Welcome" })).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders without a loading state once preloaded", async () => {
    const Wiki = lazyWithPreload(() => import("./WikiPanel"));
    await Wiki.preload();

    render(
      <Suspense fallback={<PanelFallback delay={0} />}>
        <Wiki />
      </Suspense>,
    );
    expect(await screen.findByRole("button", { name: "Welcome" })).toBeInTheDocument();
  });

  it("keeps the loading text hidden for a fast load", () => {
    vi.useFakeTimers();
    try {
      render(<PanelFallback />);
      expect(screen.getByRole("status")).toHaveTextContent("");
      act(() => { vi.advanceTimersByTime(250); });
      expect(screen.getByRole("status")).toHaveTextContent("Loading");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("lazyWithPreload", () => {
  it("fetches a chunk once, however often it is preloaded and rendered", async () => {
    const factory = vi.fn(() => Promise.resolve({ default: () => <p>panel body</p> }));
    const Panel = lazyWithPreload(factory);
    await preloadAll([Panel, Panel]);
    render(<Suspense fallback={null}><Panel /></Suspense>);
    expect(await screen.findByText("panel body")).toBeInTheDocument();
    expect(factory).toHaveBeenCalledTimes(1);
  });

  it("tries again after a failed preload, and a failed preload does not throw", async () => {
    const factory = vi.fn()
      .mockRejectedValueOnce(chunkError())
      .mockResolvedValueOnce({ default: () => <p>second try</p> });
    const Panel = lazyWithPreload(factory);
    await expect(Panel.preload()).resolves.toBeUndefined();
    render(<Suspense fallback={null}><Panel /></Suspense>);
    expect(await screen.findByText("second try")).toBeInTheDocument();
    expect(factory).toHaveBeenCalledTimes(2);
  });
});

describe("isChunkLoadError", () => {
  it("recognises each browser's message for a missing module", () => {
    expect(isChunkLoadError(chunkError())).toBe(true);
    expect(isChunkLoadError(new TypeError("error loading dynamically imported module: /assets/x.js"))).toBe(true);
    expect(isChunkLoadError(new TypeError("Importing a module script failed."))).toBe(true);
    expect(isChunkLoadError(new Error("Unable to preload CSS for /assets/x.css"))).toBe(true);
  });

  it("leaves ordinary errors alone", () => {
    expect(isChunkLoadError(new TypeError("Cannot read properties of undefined (reading 'map')"))).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
  });
});

describe("ErrorBoundary and a chunk that cannot be fetched", () => {
  function renderMissingChunk(reload) {
    const Missing = lazyWithPreload(() => Promise.reject(chunkError()));
    return render(
      <ErrorBoundary name="Query" reload={reload}>
        <Suspense fallback={<PanelFallback delay={0} />}>
          <Missing />
        </Suspense>
      </ErrorBoundary>,
    );
  }

  it("says the app was updated, reloads once by itself, and offers a Reload button", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    renderMissingChunk(reload);

    expect(await screen.findByText("havn has been updated")).toBeInTheDocument();
    expect(screen.queryByText(/crashed/)).not.toBeInTheDocument();
    expect(reload).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("does not reload in a loop when the chunk is still missing after a reload", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    // The automatic reload already happened a moment ago.
    localStorage.setItem("havn_chunk_reload_at", String(Date.now() - 5_000));
    renderMissingChunk(reload);

    expect(await screen.findByText("havn has been updated")).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });

  it("still shows the crash message for an ordinary error", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    function Broken() { throw new Error("boom"); }
    render(<ErrorBoundary name="Docs" reload={reload}><Broken /></ErrorBoundary>);

    expect(screen.getByText("Docs crashed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try Again" })).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("reloadOnceForChunkError", () => {
  it("reloads again once the guard window has passed", () => {
    const reload = vi.fn();
    const t0 = 1_000_000;
    expect(reloadOnceForChunkError(reload, t0)).toBe(true);
    expect(reloadOnceForChunkError(reload, t0 + 30_000)).toBe(false);
    expect(reloadOnceForChunkError(reload, t0 + 61_000)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });
});

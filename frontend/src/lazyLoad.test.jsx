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

const { lazyWithPreload, preloadAll, isChunkLoadError, PanelFallback, isNewDeploy, entryScriptFromHtml } = await import("./lazyLoad");
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
    // The panel itself is there on the first render; it never suspended.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
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
    const Panel = lazyWithPreload(factory, { retryDelays: [] });
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
  function renderMissingChunk(reload, checkDeploy = async () => true, factory = () => Promise.reject(chunkError())) {
    const Missing = lazyWithPreload(factory, { retryDelays: [] });
    return render(
      <ErrorBoundary name="Query" reload={reload} checkDeploy={checkDeploy}>
        <Suspense fallback={<PanelFallback delay={0} />}>
          <Missing />
        </Suspense>
      </ErrorBoundary>,
    );
  }

  it("after a new deploy, says the app was updated, reloads once by itself, and offers a Reload button", async () => {
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

  it("does not reload when the server is unreachable, and Retry loads the part in place", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.spyOn(console, "error").mockImplementation(() => {});
    const reload = vi.fn();
    let up = false;
    const factory = vi.fn(() => (up
      ? Promise.resolve({ default: () => <p>panel is back</p> })
      : Promise.reject(chunkError())));
    renderMissingChunk(reload, async () => false, factory);

    expect(await screen.findByText("Could not load this part of havn")).toBeInTheDocument();
    expect(reload).not.toHaveBeenCalled();

    up = true;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("panel is back")).toBeInTheDocument();
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

  it("never reloads when the guard cannot be stored", () => {
    // A cross-site embed or a private window can block storage; without a
    // stored guard every load would reload again.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const reload = vi.fn();
    expect(reloadOnceForChunkError(reload, 1_000_000)).toBe(false);
    expect(reloadOnceForChunkError(reload, 9_000_000)).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});

describe("lazyWithPreload retries", () => {
  it("tries a chunk that could not be fetched again before giving up", async () => {
    const factory = vi.fn()
      .mockRejectedValueOnce(chunkError())
      .mockRejectedValueOnce(chunkError())
      .mockResolvedValueOnce({ default: () => <p>third time</p> });
    const Panel = lazyWithPreload(factory, { retryDelays: [1, 1] });
    render(<ErrorBoundary name="P"><Suspense fallback={null}><Panel /></Suspense></ErrorBoundary>);
    expect(await screen.findByText("third time")).toBeInTheDocument();
    expect(factory).toHaveBeenCalledTimes(3);
  });

  it("does not retry an error that is not a missing chunk", async () => {
    const factory = vi.fn().mockRejectedValue(new SyntaxError("Unexpected token"));
    const Panel = lazyWithPreload(factory, { retryDelays: [1, 1] });
    await Panel.preload();
    expect(factory).toHaveBeenCalledTimes(1);
  });
});

describe("isNewDeploy", () => {
  const html = (src) => `<!doctype html><html><head><script type="module" crossorigin src="${src}"></script></head></html>`;
  const answer = (body, ok = true) => vi.fn(async () => ({ ok, text: async () => body }));

  it("is true only when the served index.html starts from another entry script", async () => {
    const running = "/assets/index-aaa.js";
    expect(await isNewDeploy({ fetchImpl: answer(html("/assets/index-bbb.js")), running })).toBe(true);
    expect(await isNewDeploy({ fetchImpl: answer(html("/assets/index-aaa.js")), running })).toBe(false);
  });

  it("is false when the server is down, erroring or not serving the app", async () => {
    const running = "/assets/index-aaa.js";
    const down = vi.fn(async () => { throw new TypeError("Failed to fetch"); });
    expect(await isNewDeploy({ fetchImpl: down, running })).toBe(false);
    expect(await isNewDeploy({ fetchImpl: answer("Bad gateway", false), running })).toBe(false);
    expect(await isNewDeploy({ fetchImpl: answer("<html>proxy page</html>"), running })).toBe(false);
  });

  it("asks for a fresh copy of index.html", async () => {
    const fetchImpl = answer(html("/assets/index-bbb.js"));
    await isNewDeploy({ fetchImpl, running: "/assets/index-aaa.js" });
    expect(fetchImpl).toHaveBeenCalledWith("/", expect.objectContaining({ cache: "no-store" }));
  });

  it("reads the entry script from index.html markup", () => {
    expect(entryScriptFromHtml(html("/assets/index-x.js"))).toBe("/assets/index-x.js");
    expect(entryScriptFromHtml('<script src="/a.js"></script>')).toBe(null);
  });
});

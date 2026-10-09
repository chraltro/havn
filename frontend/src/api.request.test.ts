import { describe, it, expect, vi, afterEach } from "vitest";
import { api } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request() and a caller's abort", () => {
  it("rethrows a caller abort at once instead of retrying it as a timeout", async () => {
    // A retryable GET: before the fix the abort was read as a timeout and the
    // request fired again after the backoff, undoing the caller's cancel.
    const fetchMock = vi.fn((_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal!.addEventListener("abort", () =>
          reject(new DOMException("The operation was aborted.", "AbortError")),
        );
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const controller = new AbortController();
    const pending = api.columnReferences("silver.customers", "id", controller.signal);
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not leave a listener on the caller's signal after the request settles", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ sites: [] }), { status: 200 })));
    const controller = new AbortController();
    const add = vi.spyOn(controller.signal, "addEventListener");
    const remove = vi.spyOn(controller.signal, "removeEventListener");

    await api.columnReferences("silver.customers", "id", controller.signal);

    expect(add).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith("abort", add.mock.calls[0][1]);
  });
});

describe("connectToStreamEvents", () => {
  it("keeps an event's name when its frame is split across reads", async () => {
    const encoder = new TextEncoder();
    // The "event:" line arrives in one chunk, its "data:" line in the next.
    const chunks = ["event: model_end\n", 'data: {"model": "gold.orders"}\n\n'];
    const body = new ReadableStream({
      start(controller) {
        for (const c of chunks) controller.enqueue(encoder.encode(c));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(body, { status: 200 })));

    const events: Array<[string, Record<string, unknown>]> = [];
    const conn = api.connectToStreamEvents(0, (event, data) => events.push([event, data]));
    await conn.done;

    expect(events).toEqual([["model_end", { model: "gold.orders" }]]);
  });
});

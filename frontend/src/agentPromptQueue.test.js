import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useQueuedAgentPrompts } from "./agentPromptQueue";

function send(prompt) {
  window.dispatchEvent(new CustomEvent("havn-agent-send", { detail: { prompt } }));
}

describe("useQueuedAgentPrompts", () => {
  it("queues a prompt and opens the sidebar when nothing is listening", () => {
    const open = vi.fn();
    const { result } = renderHook(() => useQueuedAgentPrompts(open));
    act(() => send("review this"));
    expect(open).toHaveBeenCalledTimes(1);
    expect(result.current.pendingPrompt).toEqual({ prompt: "review this" });
    act(() => result.current.onPromptConsumed());
    expect(result.current.pendingPrompt).toBeNull();
  });

  it("keeps a prompt sent while the sidebar is open but its chunk is still downloading", () => {
    // The sidebar was opened already (e.g. by an earlier click), but has not
    // mounted: an open-only check dropped this prompt.
    const open = vi.fn();
    const { result } = renderHook(() => useQueuedAgentPrompts(open));
    act(() => send("first"));
    act(() => send("second"));
    expect(result.current.pendingPrompt).toEqual({ prompt: "second" });
  });

  it("leaves the prompt to the sidebar once it listens itself", () => {
    const open = vi.fn();
    const { result } = renderHook(() => useQueuedAgentPrompts(open));
    act(() => result.current.onListeningChange(true));
    act(() => send("handled by the sidebar"));
    expect(open).not.toHaveBeenCalled();
    expect(result.current.pendingPrompt).toBeNull();
    act(() => result.current.onListeningChange(false));
    act(() => send("sidebar closed again"));
    expect(result.current.pendingPrompt).toEqual({ prompt: "sidebar closed again" });
  });
});

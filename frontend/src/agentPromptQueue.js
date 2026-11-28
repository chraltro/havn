import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Queue for 'havn-agent-send' prompts (e.g. ReviewsPanel's AI Review button)
 * that arrive while the agent sidebar is not listening for them itself.
 *
 * "Not listening" is not the same as "closed": the sidebar is its own chunk,
 * and while it downloads after being opened nothing has subscribed yet, so a
 * prompt sent then was lost. The sidebar reports when its own listener is in
 * place through `onListeningChange`; until then a prompt is queued, the
 * sidebar is opened (`openSidebar`), and it takes the prompt on mount from
 * `pendingPrompt` and calls `onPromptConsumed`.
 */
export function useQueuedAgentPrompts(openSidebar) {
  const [pendingPrompt, setPendingPrompt] = useState(null);
  const listeningRef = useRef(false);
  const openRef = useRef(openSidebar);
  openRef.current = openSidebar;

  useEffect(() => {
    const handler = (e) => {
      if (!e?.detail?.prompt) return;
      if (listeningRef.current) return; // the sidebar handles it
      setPendingPrompt(e.detail);
      openRef.current?.();
    };
    window.addEventListener("havn-agent-send", handler);
    return () => window.removeEventListener("havn-agent-send", handler);
  }, []);

  const onPromptConsumed = useCallback(() => setPendingPrompt(null), []);
  const onListeningChange = useCallback((listening) => {
    listeningRef.current = listening;
  }, []);
  return { pendingPrompt, onPromptConsumed, onListeningChange };
}

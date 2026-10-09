// Keyboard-shortcut labels for the viewer's platform: ⌘ on a Mac, Ctrl elsewhere.
export const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || "");
export const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
export const SAVE_KEYS = IS_MAC ? "⌘S" : "Ctrl+S";
export const RUN_KEYS = IS_MAC ? "⌘↵" : "Ctrl+Enter";

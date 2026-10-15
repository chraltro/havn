// Serve Monaco from havn itself. @monaco-editor/react otherwise fetches it
// from cdn.jsdelivr.net at runtime, so without internet access (an
// air-gapped install, a proxy that blocks the CDN) every code editor sat on
// "Loading..." forever, and each page load asked a third party for it.
//
// The build copies monaco-editor/min/vs, the version installed with
// @monaco-editor/react, to /monaco/vs; the dev server serves the same files
// from node_modules (see vite.config.js).
//
// Import this before anything calls loader.init(): Editor.jsx does at module
// load. The `in` check keeps tests that mock @monaco-editor/react without a
// loader working (reading a missing export from such a mock throws).
import * as monacoReact from "@monaco-editor/react";

export const MONACO_VS_PATH = "/monaco/vs";

const loader = "loader" in monacoReact ? monacoReact.loader : null;
if (typeof loader?.config === "function") {
  loader.config({ paths: { vs: MONACO_VS_PATH } });
}

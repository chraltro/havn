import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import ThemeProvider from "./ThemeProvider";
import { HintProvider } from "./HintSystem";
import { AuthProvider } from "./AuthContext";

// Published dashboards (/p/<key>) are a separate, chrome-less page: load only
// what they need, so a phone opening a shared link never downloads the editor.
const publishedMatch = /^\/p\/[^/]+\/?$/.test(window.location.pathname);
const App = lazy(() => import("./App"));
const PublishedDashboard = lazy(() => import("./PublishedDashboard"));

function Published() {
  const key = decodeURIComponent(window.location.pathname.replace(/^\/p\//, "").replace(/\/$/, ""));
  return <PublishedDashboard shareKey={key} />;
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ThemeProvider>
      <Suspense fallback={null}>
        {publishedMatch ? (
          <Published />
        ) : (
          <HintProvider>
            <AuthProvider>
              <App />
            </AuthProvider>
          </HintProvider>
        )}
      </Suspense>
    </ThemeProvider>
  </React.StrictMode>,
);

import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import ThemeProvider from "./ThemeProvider";
import { HintProvider } from "./HintSystem";
import { AuthProvider } from "./AuthContext";
import ErrorBoundary from "./ErrorBoundary";

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
      {/* Catches a chunk that cannot be fetched (a deploy replaced it) as
          well as a crash outside every panel's own boundary. */}
      <ErrorBoundary name="havn" fullScreen>
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
      </ErrorBoundary>
    </ThemeProvider>
  </React.StrictMode>,
);

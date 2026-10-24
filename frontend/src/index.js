import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@/index.css";
import App from "@/App";
import * as serviceWorkerRegistration from "@/serviceWorkerRegistration";
import { initNativeShell, isNativePlatform } from "@/nativeShell";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
    },
  },
});

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);

// Native shell (Capacitor apps only; a no-op in browsers): back-button
// handling and external-link routing. See src/nativeShell.js.
initNativeShell();

// PWA: install the app shell cache (production only; see
// public/service-worker.js for the caching strategy). Skipped inside the
// native apps: their bundle ships with the app and updates arrive via
// store releases, so the web service-worker cache does not apply there.
// The PWA itself is unchanged and remains the no-store fallback.
if (!isNativePlatform()) {
  serviceWorkerRegistration.register();
}

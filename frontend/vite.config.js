import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.ATLAS_API || "http://localhost:8000";

// The browser must see ONE origin: the httpOnly refresh cookie is scoped to /auth on that origin, and the API then needs no
// CORS. The dev server and `vite preview` (used by the browser tests against the production bundle) forward the same paths
// as the production nginx (frontend/nginx.conf); tests/deploy/test_compose.py keeps the three lists in step.
const proxy = Object.fromEntries(["/auth", "/evaluations", "/repositories", "/debug", "/ready"].map((p) => [p, api]));

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  preview: { proxy },
  test: { environment: "jsdom", setupFiles: ["./src/test-setup.js"], css: false, exclude: ["e2e/**", "node_modules/**"] },
});

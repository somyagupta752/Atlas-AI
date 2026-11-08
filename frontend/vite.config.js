import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API, so the browser sees one origin and the backend needs no CORS.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/evaluations": process.env.ATLAS_API || "http://localhost:8000" } },
});

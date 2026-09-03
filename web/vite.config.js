import { defineConfig } from "vite";

export default defineConfig({
  server: {
    // The backend's CORS list allows exactly http://localhost:5173 and
    // http://127.0.0.1:5173. strictPort makes Vite fail loudly if 5173 is
    // taken, rather than quietly moving to 5174 and causing a confusing
    // "blocked by CORS" error in the browser console.
    port: 5173,
    strictPort: true,
  },
});
